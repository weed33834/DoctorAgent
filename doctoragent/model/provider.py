"""Model provider abstraction with multi-platform support.

Supports Ollama, LM Studio, vLLM, llama.cpp server, LocalAI, OpenAI,
Anthropic, and any custom OpenAI-compatible endpoint.
"""

import asyncio
import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable
from typing import Any, cast

import httpx
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from doctoragent.connections.models import AuthMethod, Connection
from doctoragent.observability.langfuse import observe

logger = logging.getLogger(__name__)

# Platforms that do NOT support the OpenAI /v1/models health endpoint.
_NO_HEALTH_ENDPOINT_PLATFORMS = frozenset({"anthropic"})

# Platform detection: port → platform type mapping
_PLATFORM_PORT_MAP: dict[int, str] = {
    11434: "ollama",
    1234: "lm_studio",
    8000: "llamacpp_server",
    8080: "localai",
}


def _build_headers(connection: Connection) -> dict[str, str]:
    """Build HTTP headers from connection auth config."""
    headers: dict[str, str] = {}
    headers.update(connection.custom_headers)

    api_key = connection.api_key.get_secret_value()
    if connection.auth_method == AuthMethod.BEARER and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif connection.auth_method == AuthMethod.API_KEY and api_key:
        headers["Authorization"] = api_key

    return headers


class UsageStats(BaseModel):
    """Aggregated token / cost usage statistics for a provider instance.

    Populated incrementally by
    :meth:`OpenAICompatibleProvider._record_usage` each time an
    OpenAI-compatible ``usage`` block is observed on a chat completion (or
    streaming) response. ``cost_usd`` is reserved for callers that wish to
    attach per-1k-token pricing; the provider itself leaves it at ``0.0`` as
    it has no price source.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    # Optional cost estimate (USD); populated when price_per_1k set
    cost_usd: float = 0.0


class ChatCompletionResponse(BaseModel):
    """Richer chat-completion response that may carry native tool calls.

    Returned by :meth:`OpenAICompatibleProvider.chat_completion` (and its
    synchronous counterpart) when the caller supplies a non-empty ``tools``
    list. When no tools are requested the provider keeps returning a plain
    ``str`` for full backward compatibility with existing callers
    (classifier, RAG pipeline, tests, ...).
    """

    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] | None = None


class ModelProvider(ABC):
    """Abstract model provider."""

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> str | ChatCompletionResponse:
        """Return assistant message content as a string.

        Implementations that support native function calling should return a
        :class:`ChatCompletionResponse` when *tools* is provided, and a plain
        ``str`` otherwise.
        """

    @abstractmethod
    async def health(self) -> bool:
        """Check if provider is reachable."""

    @abstractmethod
    async def close(self) -> None:
        """Close underlying resources."""


class OpenAICompatibleProvider(ModelProvider):
    """Provider for any OpenAI-compatible endpoint (base class)."""

    def __init__(
        self,
        connection: Connection,
        temperature: float = 0.3,
        fallback_model: str | None = None,
    ) -> None:
        self.connection = connection
        self.temperature = temperature
        self._usage = UsageStats()
        self._usage_lock = threading.Lock()
        self._fallback_model = fallback_model
        if connection.base_url.endswith("/v1"):
            self._chat_path = "chat/completions"
            self._models_path = "models"
            self._messages_path = "messages"
        else:
            self._chat_path = "/v1/chat/completions"
            self._models_path = "/v1/models"
            self._messages_path = "/v1/messages"
        password = connection.password.get_secret_value()
        auth = (
            httpx.BasicAuth(connection.username, password)
            if (connection.auth_method == AuthMethod.BASIC and connection.username and password)
            else None
        )
        self.client = httpx.AsyncClient(
            base_url=connection.base_url,
            timeout=connection.timeout,
            headers=_build_headers(connection),
            auth=auth,
            follow_redirects=True,
        )

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _do_chat_completion(self, payload: dict[str, Any]) -> str:
        response = await self.client.post(self._chat_path, json=payload)
        response.raise_for_status()
        data = response.json()
        self._record_usage(data.get("usage"))
        message = data.get("choices", [{}])[0].get("message", {})
        content = message.get("content")
        if content is None:
            reasoning = message.get("reasoning_content")
            if reasoning:
                logger.debug("Using reasoning_content as fallback (content was null)")
                return cast(str, reasoning)
            raise RuntimeError(
                f"API returned null content in response: {data}. "
                "The model may not support this request format, or max_tokens "
                "may be too low for reasoning models."
            )
        return cast(str, content)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _do_chat_completion_with_tools(
        self, payload: dict[str, Any]
    ) -> ChatCompletionResponse:
        """POST a tools-enabled completion and parse native ``tool_calls``.

        Unlike :meth:`_do_chat_completion`, a null/empty ``content`` is *not*
        an error here: many models emit only ``tool_calls`` and leave
        ``content`` null. We normalise such cases to an empty string.
        """
        response = await self.client.post(self._chat_path, json=payload)
        response.raise_for_status()
        data = response.json()
        self._record_usage(data.get("usage"))
        message = data.get("choices", [{}])[0].get("message", {}) or {}

        content = message.get("content")
        if content is None:
            # Some models return null content together with tool_calls, or
            # stash the text under reasoning_content. Treat both as content.
            content = message.get("reasoning_content") or ""

        raw_tool_calls = message.get("tool_calls") or []
        tool_calls: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            func = tc.get("function", {}) or {}
            args = func.get("arguments", {})
            # OpenAI sends arguments as a JSON string; normalise to a dict.
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    args = {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(
                {
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": args,
                }
            )

        return ChatCompletionResponse(
            content=cast(str, content),
            tool_calls=tool_calls,
            usage=data.get("usage"),
        )

    def chat_completion_sync(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> str | ChatCompletionResponse:
        """Synchronous wrapper for :meth:`chat_completion`.

        .. warning::
            This method MUST NOT reuse the shared ``self.client``
            (:class:`httpx.AsyncClient`) via :func:`async_to_sync`. Doing so
            binds that async client to the throwaway event loop
            ``async_to_sync`` creates (and then closes), which permanently
            breaks every subsequent ``await provider.chat_completion(...)``
            on the real loop with ``RuntimeError: Event loop is closed`` /
            ``<asyncio.locks.Event> is bound to a different event loop``.

            The Agent base calls this sync method from inside async plan /
            ReAct / reflection methods — so the corruption was previously
            guaranteed on the first LLM call of any clinical workflow.

            Instead we run a one-shot **synchronous** ``httpx.Client`` with
            the same base URL / headers / auth as the async client. The async
            ``self.client`` is never touched by sync calls, so the two paths
            coexist safely. Token usage is still recorded.
        """
        payload: dict[str, Any] = {
            "model": self.connection.model_name,
            "messages": messages,
            "temperature": self.temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        payload.update(self.connection.custom_payload)

        auth: Any = None
        if self.connection.auth_method == AuthMethod.BASIC:
            auth = httpx.BasicAuth(
                self.connection.username,
                self.connection.password.get_secret_value(),
            )
        try:
            with httpx.Client(
                base_url=self.connection.base_url,
                timeout=self.connection.timeout,
                headers=_build_headers(self.connection),
                auth=auth,
                follow_redirects=True,
            ) as sync_client:
                response = sync_client.post(self._chat_path, json=payload)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {429, 500, 502, 503, 504} and self._fallback_model:
                payload["model"] = self._fallback_model
                with httpx.Client(
                    base_url=self.connection.base_url,
                    timeout=self.connection.timeout,
                    headers=_build_headers(self.connection),
                    auth=auth,
                    follow_redirects=True,
                ) as sync_client:
                    response = sync_client.post(self._chat_path, json=payload)
            else:
                raise RuntimeError(f"Chat completion failed ({status}): {exc}") from exc
        except httpx.RequestError:
            if self._fallback_model:
                payload["model"] = self._fallback_model
                with httpx.Client(
                    base_url=self.connection.base_url,
                    timeout=self.connection.timeout,
                    headers=_build_headers(self.connection),
                    auth=auth,
                    follow_redirects=True,
                ) as sync_client:
                    response = sync_client.post(self._chat_path, json=payload)
            else:
                raise

        response.raise_for_status()
        data = response.json()
        self._record_usage(data.get("usage"))
        message = data.get("choices", [{}])[0].get("message", {}) or {}

        if tools:
            content = message.get("content")
            if content is None:
                content = message.get("reasoning_content") or ""
            raw_tool_calls = message.get("tool_calls") or []
            tool_calls: list[dict[str, Any]] = []
            for tc in raw_tool_calls:
                func = tc.get("function", {}) or {}
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "arguments": args,
                    }
                )
            return ChatCompletionResponse(
                content=cast(str, content),
                tool_calls=tool_calls,
                usage=data.get("usage"),
            )

        content = message.get("content")
        if content is None:
            reasoning = message.get("reasoning_content")
            if reasoning:
                return cast(str, reasoning)
            raise RuntimeError(
                f"API returned null content in response: {data}. "
                "The model may not support this request format, or max_tokens "
                "may be too low for reasoning models."
            )
        return cast(str, content)

    @observe(
        name="llm.chat_completion",
        # ``messages`` routinely carry clinical prompts with PHI (patient
        # context, lab values, medications). Langfuse is an external
        # service — never capture raw inputs. Token usage / latency /
        # errors are still recorded (Langfuse records those structurally).
        capture_input=False,
        capture_output=False,
    )
    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> str | ChatCompletionResponse:
        """Call /v1/chat/completions with tenacity retry on transient errors.

        When *tools* is a non-empty list the OpenAI ``tools`` (and optional
        ``tool_choice``) parameter is attached to the request and a
        :class:`ChatCompletionResponse` carrying parsed ``tool_calls`` is
        returned. When *tools* is ``None`` (the default) a plain ``str`` is
        returned, preserving backward compatibility.
        """
        payload: dict[str, Any] = {
            "model": self.connection.model_name,
            "messages": messages,
            "temperature": self.temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        payload.update(self.connection.custom_payload)
        try:
            if tools:
                return await self._do_chat_completion_with_tools(payload)
            return await self._do_chat_completion(payload)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # Transient / overload statuses that warrant a fallback-model
            # retry. 529 is the gateway/CDN "site is overloaded" code (Cloudflare,
            # some OpenAI-compatible gateways) — without it an overloaded
            # upstream permanently fails the call instead of failing over.
            if status in {429, 500, 502, 503, 504, 529}:
                if self._fallback_model:
                    return await self._call_fallback(payload, tools, tool_choice, exc)
                raise
            raise RuntimeError(f"Chat completion failed ({status}): {exc}") from exc
        except httpx.RequestError as exc:
            if self._fallback_model:
                return await self._call_fallback(payload, tools, tool_choice, exc)
            raise

    async def _call_fallback(
        self,
        payload: dict[str, Any],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        original_exc: BaseException,
    ) -> str | ChatCompletionResponse:
        """Retry *payload* against the configured fallback model.

        Used by :meth:`chat_completion` when the primary model fails with a
        transient HTTP error (429 / 5xx) or a transport-level
        :class:`httpx.RequestError`. If the fallback attempt also fails, the
        *original* exception is re-raised so callers see the primary fault.
        """
        logger.warning(
            "Primary model %r failed (%s); falling back to %r",
            self.connection.model_name,
            original_exc,
            self._fallback_model,
        )
        fallback_payload = {**payload, "model": self._fallback_model}
        try:
            if tools:
                return await self._do_chat_completion_with_tools(fallback_payload)
            return await self._do_chat_completion(fallback_payload)
        except (httpx.HTTPStatusError, httpx.RequestError):
            # Surface the primary fault; suppress the fallback's exception
            # context so callers see the original cause, not the retry's.
            raise original_exc from None

    def _record_usage(self, usage: dict[str, Any] | None) -> None:
        """Accumulate an OpenAI ``usage`` block into the provider stats.

        Thread-safe via ``self._usage_lock``. Missing or non-numeric fields
        default to ``0`` so a partial ``usage`` block never raises.
        """
        if not usage:
            return
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        total = int(usage.get("total_tokens", 0) or 0)
        with self._usage_lock:
            self._usage.prompt_tokens += prompt
            self._usage.completion_tokens += completion
            self._usage.total_tokens += total
            self._usage.requests += 1
        # Mirror to the Prometheus counter so the /metrics endpoint reflects
        # real token consumption. Safe no-op when prometheus_client is absent
        # (the metric is an in-process stub).
        try:
            from doctoragent.observability.metrics import doctoragent_llm_tokens_total

            doctoragent_llm_tokens_total.labels(model=self.model_name, kind="prompt").inc(prompt)
            doctoragent_llm_tokens_total.labels(model=self.model_name, kind="completion").inc(
                completion
            )
        except Exception:  # noqa: BLE001 - metrics must never break LLM call
            pass

    def get_usage(self) -> UsageStats:
        """Return a snapshot of aggregated usage statistics."""
        with self._usage_lock:
            return self._usage.model_copy()

    def reset_usage(self) -> UsageStats:
        """Reset usage stats and return the pre-reset snapshot."""
        with self._usage_lock:
            snapshot = self._usage.model_copy()
            self._usage = UsageStats()
            return snapshot

    def set_fallback_model(self, name: str | None) -> None:
        """Configure (or clear) the fallback model used on transient errors."""
        self._fallback_model = name

    async def chat_completion_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream assistant content deltas as an async generator.

        Sends the request with ``stream: True`` and
        ``stream_options.include_usage`` so OpenAI-compatible servers that
        support it return a final chunk carrying a ``usage`` block, which is
        fed to :meth:`_record_usage`. Each yielded value is a ``delta.content``
        fragment; the caller is responsible for concatenation.

        Transient connection / setup errors are retried with the same policy
        as :meth:`_do_chat_completion` (3 attempts, exponential backoff). The
        ``tenacity`` decorator cannot wrap an async generator — it only
        observes generator *creation*, not iteration — so the equivalent
        retry loop is implemented inline. Once any content has been yielded
        the stream is committed and mid-stream errors propagate without
        replay (to avoid emitting duplicate content).
        """
        payload: dict[str, Any] = {
            "model": self.connection.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        payload.update(self.connection.custom_payload)

        last_exc: Exception | None = None
        yielded_content = False
        for attempt in range(3):
            try:
                async with self.client.stream("POST", self._chat_path, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[len("data: ") :]
                        if data_str == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        usage = chunk.get("usage")
                        if usage:
                            self._record_usage(usage)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}) or {}
                        content = delta.get("content")
                        if content:
                            yielded_content = True
                            yield cast(str, content)
                return
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                if yielded_content:
                    raise
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(min(2**attempt, 10))
        assert last_exc is not None
        raise last_exc

    async def health(self) -> bool:
        """Check provider health."""
        if self.connection.platform_type.value in _NO_HEALTH_ENDPOINT_PLATFORMS:
            try:
                response = await self.client.post(
                    self._messages_path,
                    json={
                        "model": self.connection.model_name,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                return response.status_code in {200, 400}
            except Exception:  # noqa: BLE001
                return False
        try:
            response = await self.client.get(self._models_path)
            response.raise_for_status()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        """Close underlying HTTP client."""
        await self.client.aclose()


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama-specific provider with native API support."""

    def __init__(self, connection: Connection, temperature: float = 0.3) -> None:
        super().__init__(connection, temperature)
        # Ollama native endpoints (non-OpenAI-compatible)
        self._ollama_base = connection.base_url.rstrip("/v1").rstrip("/")
        self._ollama_client = httpx.AsyncClient(
            base_url=self._ollama_base,
            timeout=connection.timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await super().close()
        await self._ollama_client.aclose()


class VLLMProvider(OpenAICompatibleProvider):
    """vLLM-specific provider with structured output support."""


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: dict[str, Callable[[Connection], ModelProvider]] = {}
_REGISTRY_LOCK = threading.Lock()

BUILT_IN_PROVIDER_NAMES: tuple[str, ...] = (
    "openai_compatible",
    "ollama",
    "lm_studio",
    "llamacpp_server",
    "vllm",
    "localai",
    "openai",
    "anthropic",
    "custom",
)

_PROVIDER_CLASS_MAP: dict[str, type[ModelProvider]] = {
    "openai_compatible": OpenAICompatibleProvider,
    "ollama": OllamaProvider,
    "lm_studio": OpenAICompatibleProvider,
    "llamacpp_server": OpenAICompatibleProvider,
    "vllm": VLLMProvider,
    "localai": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
    "anthropic": OpenAICompatibleProvider,
    "custom": OpenAICompatibleProvider,
}


def register_provider(
    name: str,
    factory: Callable[[Connection], ModelProvider],
    *,
    allow_override: bool = False,
) -> None:
    """Register a model provider factory under *name*."""
    with _REGISTRY_LOCK:
        if name in _PROVIDER_REGISTRY and not allow_override:
            raise ValueError(
                f"Provider {name!r} is already registered. "
                "Use allow_override=True to replace it explicitly."
            )
        _PROVIDER_REGISTRY[name] = factory


def _load_built_in_providers() -> None:
    """Register built-in providers lazily."""
    with _REGISTRY_LOCK:
        if "openai_compatible" in _PROVIDER_REGISTRY:
            return
        for name, cls in _PROVIDER_CLASS_MAP.items():
            _PROVIDER_REGISTRY[name] = cls


def create_provider(connection: Connection) -> ModelProvider:
    """Factory to create a provider from a connection."""
    _load_built_in_providers()

    with _REGISTRY_LOCK:
        factory = _PROVIDER_REGISTRY.get(connection.platform_type.value)
    if factory is None:
        raise ValueError(
            f"Unsupported platform type: {connection.platform_type.value!r}. "
            f"Registered providers: {sorted(_PROVIDER_REGISTRY)}"
        )
    return factory(connection)


async def detect_platform(base_url: str, timeout: float = 5.0) -> str | None:
    """Auto-detect the platform type from a base URL.

    Probes /v1/models and checks response headers/content for platform signatures.
    Returns the platform type string or None if unknown.
    """
    # Fast path: check port
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    port = parsed.port
    if port and port in _PLATFORM_PORT_MAP:
        candidate = _PLATFORM_PORT_MAP[port]
        # Verify with a health check
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                url = base_url.rstrip("/") + "/v1/models"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    data_str = str(data).lower()
                    if candidate == "ollama" and "ollama" in data_str:
                        return "ollama"
                    if candidate == "lm_studio" and (
                        "lmstudio" in data_str or "lm-studio" in data_str
                    ):
                        return "lm_studio"
                    if candidate == "llamacpp_server" and (
                        "llama" in data_str or "gguf" in data_str
                    ):
                        return "llamacpp_server"
                    if candidate == "localai" and "localai" in data_str:
                        return "localai"
                    # Port matches but no content signature — still return it
                    return candidate
        except Exception:  # noqa: BLE001
            pass

    # Slow path: try OpenAI-compatible endpoint
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            url = base_url.rstrip("/") + "/v1/models"
            resp = await client.get(url)
            if resp.status_code == 200:
                return "openai_compatible"
    except Exception:  # noqa: BLE001
        pass

    return None

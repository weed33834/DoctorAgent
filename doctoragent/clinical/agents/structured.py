"""Structured LLM output via the ``instructor`` library.

Wraps the OpenAI SDK with :func:`instructor.from_openai` so the four
clinical specialist agents receive **validated pydantic models** instead
of free-form prose that has to be regex-parsed. This is the structured-
output layer mandated by the clinical-safety contract: every LLM answer
that flows into the orchestrator is a typed object, not a string the
caller has to ``json.loads`` defensively.

Why instructor + openai SDK (not a hand-rolled tool-calling shim)?
------------------------------------------------------------------
The user's standing instruction is "use external libraries when they
exist". :mod:`instructor` is the canonical PyPI library for pydantic-
validated LLM output; rolling our own tool-schema → tool-call →
``model_validate`` pipeline would re-implement exactly what instructor
already handles (retries on validation failure, partial responses,
mode selection across providers).

The existing :class:`doctoragent.model.provider.OpenAICompatibleProvider`
speaks the OpenAI tool-calling protocol via :mod:`httpx`, but it does NOT
expose the ``openai.AsyncOpenAI`` interface that
:func:`instructor.from_openai` requires. Rather than fork the provider,
we build a *separate* ``openai.AsyncOpenAI`` client from the same
:class:`~doctoragent.connections.models.Connection` (base_url, api_key,
headers, timeout) and wrap that with instructor. The two clients share
the same endpoint + auth; the provider keeps its retry/fallback/usage-
tracking path for the unstructured ``Agent.run()`` loop, and instructor
owns the structured-output path.

Graceful degradation
--------------------
When ``instructor`` or ``openai`` are not installed (the ``clinical``
extra is optional), :func:`structured_complete` returns ``None`` and the
caller falls back to the legacy ``from_text()`` / raw-text path. This
keeps the clinical layer importable on a minimal install and lets an
enterprise deployment opt into structured output by installing
``doctoragent[clinical]``.

Lifecycle
---------
The instructor client is cached on the provider instance
(``provider._instructor_client``) so repeated structured calls reuse the
same underlying ``openai.AsyncOpenAI`` connection pool. Call
:func:`close_instructor_client` at shutdown (or rely on the provider's
own ``close()``) to release it.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel

from doctoragent.connections.models import AuthMethod, Connection
from doctoragent.model.provider import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

__all__ = [
    "STRUCTURED_AVAILABLE",
    "close_instructor_client",
    "structured_complete",
]

# Whether the instructor + openai optional deps are importable. Evaluated
# once at import time so callers can short-circuit without try/except on
# every call (the clinical extra is either installed for the whole process
# or it isn't).
try:
    import instructor as _instructor  # noqa: F401
    import openai as _openai  # noqa: F401

    STRUCTURED_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only without clinical extra
    _instructor = None  # type: ignore[assignment]
    _openai = None  # type: ignore[assignment]
    STRUCTURED_AVAILABLE = False


T = TypeVar("T", bound=BaseModel)

# Attribute name on the provider where the cached instructor client lives.
_INSTRUCTOR_ATTR = "_instructor_client"


def _normalise_base_url(connection: Connection) -> str:
    """Return a base_url suitable for ``openai.AsyncOpenAI``.

    The openai SDK appends ``/chat/completions`` to ``base_url``, so the
    latter must point at the OpenAI-compatible root (``.../v1``). The
    doctoragent provider accepts both ``http://host`` and ``http://host/v1``
    and adjusts its internal path accordingly; here we normalise to the
    ``.../v1`` form the SDK expects.
    """
    url = connection.base_url.rstrip("/")
    if url.endswith("/v1"):
        return url
    return f"{url}/v1"


def _build_openai_client(connection: Connection) -> Any:
    """Construct an ``openai.AsyncOpenAI`` from a doctoragent :class:`Connection`.

    Mirrors the auth-header logic in
    :func:`doctoragent.model.provider._build_headers` so the same endpoint +
    credentials work through both the httpx-based provider and the
    openai-SDK-based instructor path.
    """
    if not STRUCTURED_AVAILABLE:  # pragma: no cover
        raise RuntimeError("instructor/openai not installed — install doctoragent[clinical]")
    api_key = connection.api_key.get_secret_value() or "sk-not-required"
    headers: dict[str, str] = dict(connection.custom_headers)
    # The provider supports BEARER / API_KEY / BASIC; the openai SDK only
    # exposes ``api_key`` (sent as ``Authorization: Bearer <key>``). For
    # BASIC auth we fall back to a custom header since the SDK has no
    # native BASIC support — the operator is expected to front the BASIC-
    # authed endpoint with a bearer-issuing proxy in that case.
    if connection.auth_method in (AuthMethod.BEARER, AuthMethod.API_KEY) and api_key:
        pass  # api_key passed below; openai SDK sets Authorization: Bearer
    client_kwargs: dict[str, Any] = {
        "base_url": _normalise_base_url(connection),
        "api_key": api_key,
        "timeout": connection.timeout,
        "default_headers": headers or None,
        "max_retries": 0,  # instructor + tenacity own the retry policy
    }
    return _openai.AsyncOpenAI(**client_kwargs)


def _get_instructor_client(provider: OpenAICompatibleProvider) -> Any | None:
    """Return the cached instructor-wrapped client for *provider*.

    Builds + caches on first call so subsequent structured calls reuse the
    underlying connection pool. Returns ``None`` when the optional deps
    are missing (graceful degradation — caller falls back to text).
    """
    if not STRUCTURED_AVAILABLE:
        return None
    cached = getattr(provider, _INSTRUCTOR_ATTR, None)
    if cached is not None:
        return cached
    try:
        raw_client = _build_openai_client(provider.connection)
        client = _instructor.from_openai(raw_client)
    except Exception:  # noqa: BLE001 — defensive; never block the workflow
        # ``provider.connection`` may itself be the cause of the exception
        # (e.g. a non-conforming mock provider in tests); access it
        # defensively so the warning never re-raises.
        base_url = getattr(getattr(provider, "connection", None), "base_url", "<unknown>")
        logger.warning(
            "Failed to build instructor client for %s; "
            "structured output disabled for this provider",
            base_url,
            exc_info=True,
        )
        return None
    setattr(provider, _INSTRUCTOR_ATTR, client)
    return client


async def structured_complete(
    provider: OpenAICompatibleProvider,
    messages: list[dict[str, Any]],
    response_model: type[T],
    *,
    max_retries: int = 2,
    temperature: float | None = None,
) -> T | None:
    """Validate an LLM completion into *response_model* via instructor.

    Parameters
    ----------
    provider:
        The doctoragent LLM provider (must be an
        :class:`OpenAICompatibleProvider`). Its ``connection`` is used to
        build the instructor-wrapped ``openai.AsyncOpenAI`` client.
    messages:
        OpenAI chat messages (same shape the provider accepts).
    response_model:
        The pydantic model to validate the completion into.
    max_retries:
        Instructor's own validation-retry count (it re-prompts the model
        with the validation error on failure). Defaults to 2 — clinical
        workflows prefer a fast fallback to the text path over many
        expensive retries.
    temperature:
        Override the provider's default temperature for this call. When
        ``None`` (default) the provider's ``self.temperature`` is used.

    Returns
    -------
    An instance of *response_model*, or ``None`` when:
    * instructor/openai are not installed,
    * the instructor client could not be built,
    * the LLM call fails or the output fails validation after *max_retries*.

    Never raises — callers should fall back to the legacy text path on
    ``None``.
    """
    client = _get_instructor_client(provider)
    if client is None:
        return None
    model_name = provider.connection.model_name
    call_kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "response_model": response_model,
        "max_retries": max_retries,
        "temperature": temperature if temperature is not None else provider.temperature,
    }
    # Forward any provider-level custom payload keys the operator set
    # (e.g. ``top_p``, ``stop``) so structured calls respect the same
    # tuning as unstructured ones.
    custom_payload = getattr(provider.connection, "custom_payload", None)
    if isinstance(custom_payload, dict):
        for k, v in custom_payload.items():
            # Don't clobber the keys we already set.
            if k not in call_kwargs:
                call_kwargs[k] = v
    try:
        result = await client.chat.completions.create(**call_kwargs)
    except Exception:  # noqa: BLE001 — never block the clinical workflow
        logger.warning(
            "instructor structured_complete failed for model %s; falling back to text path",
            model_name,
            exc_info=True,
        )
        return None
    return result  # type: ignore[return-value]


async def close_instructor_client(provider: OpenAICompatibleProvider) -> None:
    """Release the cached instructor client's connection pool.

    Safe to call when no client was built (no-op). Called by the
    provider's ``close()`` lifecycle hook at shutdown.
    """
    client = getattr(provider, _INSTRUCTOR_ATTR, None)
    if client is None:
        return
    # The instructor client wraps an openai.AsyncOpenAI; the underlying
    # client is accessible via ``client.client`` (instructor >=1.0).
    raw = getattr(client, "client", None)
    if raw is not None and hasattr(raw, "close"):
        try:
            close = raw.close
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
        except Exception:  # noqa: BLE001 — shutdown path, never raise
            logger.debug("instructor client close failed", exc_info=True)
    setattr(provider, _INSTRUCTOR_ATTR, None)

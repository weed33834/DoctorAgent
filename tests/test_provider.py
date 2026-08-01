"""Tests for OpenAI-compatible model provider."""

from http import HTTPStatus
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import SecretStr

from doctoragent.connections.models import AuthMethod, Connection, PlatformType
from doctoragent.model.provider import (
    OpenAICompatibleProvider,
    _build_headers,
    _load_built_in_providers,
    create_provider,
    register_provider,
)


@pytest.fixture
def connection() -> Connection:
    """Default local connection fixture."""
    return Connection(
        name="test",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:1234",
        model_name="test-model",
    )


def test_create_provider_loads_built_ins(connection: Connection) -> None:
    """create_provider auto-registers built-in providers."""
    provider = create_provider(connection)
    assert isinstance(provider, OpenAICompatibleProvider)


def test_create_provider_lists_registered_on_unknown(
    connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_provider error includes the list of registered providers."""
    monkeypatch.setattr(
        "doctoragent.model.provider._PROVIDER_REGISTRY", {"other": OpenAICompatibleProvider}
    )
    monkeypatch.setattr("doctoragent.model.provider._load_built_in_providers", lambda: None)
    with pytest.raises(ValueError) as exc_info:
        create_provider(connection)
    assert connection.platform_type.value in str(exc_info.value)
    assert "Registered providers" in str(exc_info.value)


def test_register_provider_rejects_duplicate_by_default() -> None:
    """Duplicate provider registration is rejected unless explicitly allowed."""
    with pytest.raises(ValueError):
        register_provider("openai_compatible", OpenAICompatibleProvider)


def test_register_provider_allows_override() -> None:
    """Override is allowed when explicitly requested."""
    register_provider("openai_compatible", OpenAICompatibleProvider, allow_override=True)


async def test_chat_completion_success(connection: Connection) -> None:
    """chat_completion returns the assistant message content."""
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.OK
    mock_response.json.return_value = {"choices": [{"message": {"content": "hello back"}}]}
    mock_response.raise_for_status.return_value = None

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat_completion([{"role": "user", "content": "hi"}])

    assert result == "hello back"
    await provider.close()


async def test_chat_completion_merges_custom_payload(connection: Connection) -> None:
    """chat_completion merges connection.custom_payload into the request body."""
    connection.custom_payload = {"top_p": 0.9, "max_tokens": 42}

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.OK
    mock_response.json.return_value = {"choices": [{"message": {"content": ""}}]}
    mock_response.raise_for_status.return_value = None

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)) as post:
        await provider.chat_completion([{"role": "user", "content": "hi"}])

    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "test-model"
    assert payload["top_p"] == 0.9
    assert payload["max_tokens"] == 42
    await provider.close()


async def test_chat_completion_raises_on_http_error(connection: Connection) -> None:
    """chat_completion propagates HTTP errors."""
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.UNAUTHORIZED
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Unauthorized",
        request=Mock(spec=httpx.Request),
        response=mock_response,
    )

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(RuntimeError, match="Chat completion failed"):
            await provider.chat_completion([{"role": "user", "content": "hi"}])

    await provider.close()


async def test_health_success(connection: Connection) -> None:
    """health returns True when /v1/models responds."""
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.OK
    mock_response.raise_for_status.return_value = None

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "get", new=AsyncMock(return_value=mock_response)):
        assert await provider.health() is True

    await provider.close()


async def test_health_failure(connection: Connection) -> None:
    """health returns False when the request fails."""
    provider = OpenAICompatibleProvider(connection)
    with patch.object(
        provider.client, "get", new=AsyncMock(side_effect=httpx.ConnectError("boom"))
    ):
        assert await provider.health() is False

    await provider.close()


def test_build_headers_bearer() -> None:
    """Bearer auth sets Authorization header."""
    conn = Connection(
        name="test",
        platform_type=PlatformType.OPENAI,
        base_url="http://localhost",
        auth_method=AuthMethod.BEARER,
        api_key="secret-token",
    )
    headers = _build_headers(conn)
    assert headers["Authorization"] == "Bearer secret-token"


def test_build_headers_api_key() -> None:
    """API key auth passes the key verbatim."""
    conn = Connection(
        name="test",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url="http://localhost",
        auth_method=AuthMethod.API_KEY,
        api_key="ApiKey secret-token",
    )
    headers = _build_headers(conn)
    assert headers["Authorization"] == "ApiKey secret-token"


def test_build_headers_basic() -> None:
    """Basic auth is handled by httpx.BasicAuth, not a custom header."""
    conn = Connection(
        name="test",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url="http://localhost",
        auth_method=AuthMethod.BASIC,
        username="alice",
        password="wonderland",
    )
    headers = _build_headers(conn)
    assert "Authorization" not in headers


async def test_openai_compatible_provider_uses_basic_auth() -> None:
    """Basic auth configures httpx.BasicAuth on the HTTP client."""
    conn = Connection(
        name="test",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url="http://localhost",
        auth_method=AuthMethod.BASIC,
        username="alice",
        password="wonderland",
    )
    provider = OpenAICompatibleProvider(conn)
    assert isinstance(provider.client.auth, httpx.BasicAuth)
    await provider.close()


def test_build_headers_custom_headers(connection: Connection) -> None:
    """Custom headers are preserved alongside auth headers."""
    connection.custom_headers = {"X-Custom": "value"}
    connection.auth_method = AuthMethod.BEARER
    connection.api_key = SecretStr("token")
    headers = _build_headers(connection)
    assert headers["X-Custom"] == "value"
    assert headers["Authorization"] == "Bearer token"


# ---------------------------------------------------------------------------
# Provider registry coverage
# ---------------------------------------------------------------------------


def test_load_built_in_providers_registers_all_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_load_built_in_providers registers every built-in when registry is empty."""
    monkeypatch.setattr("doctoragent.model.provider._PROVIDER_REGISTRY", {})
    _load_built_in_providers()
    from doctoragent.model.provider import _PROVIDER_REGISTRY

    expected = {
        "openai_compatible",
        "ollama",
        "lm_studio",
        "llamacpp_server",
        "openai",
        "custom",
    }
    assert expected <= set(_PROVIDER_REGISTRY)


# --------------------------------------------------------------------------- #
# Regression: chat_completion_sync must not poison the shared async client
# --------------------------------------------------------------------------- #
# Bug class (fixed): chat_completion_sync previously called
# async_to_sync(self.chat_completion(...)) → asyncio.run() in a worker
# thread, which created+closed a throwaway event loop. The shared
# httpx.AsyncClient (self.client) bound to that loop on first await, then
# died — breaking every subsequent await provider.chat_completion(...) on
# the real loop with "Event loop is closed" /
# "<asyncio.locks.Event> is bound to a different event loop".
#
# The Agent base calls chat_completion_sync from inside async plan/ReAct/
# reflection methods, so the corruption was guaranteed on the first LLM
# call of any clinical workflow. The fix routes sync calls through a
# one-shot synchronous httpx.Client so the async client is never touched.


def _ok_response() -> httpx.Response:
    """A 200 chat-completion response carrying content "ok"."""
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
    )


def test_chat_completion_sync_does_not_touch_async_client(
    connection: Connection,
) -> None:
    """The sync path must NOT call the shared async ``self.client``.

    If it did, the async client would bind to a throwaway loop and every
    later ``await chat_completion`` would raise "Event loop is closed".
    We assert the async client's ``post`` is never awaited during the sync
    call by leaving it as a real (unmocked) client pointed at a dead URL —
    a sync call that mistakenly used it would hang / connect-error.
    """
    provider = OpenAICompatibleProvider(connection)
    # Point the ASYNC client at a port nothing listens on, then patch its
    # post to raise if it's ever called (proving the sync path doesn't use it).
    async def _explode(*a, **kw):  # noqa: ANN001
        raise AssertionError(
            "chat_completion_sync must not use the shared async client"
        )

    with patch.object(provider.client, "post", new=_explode):
        # Patch the SYNCHRONOUS httpx.Client used by chat_completion_sync so
        # it returns our canned response without hitting the network.
        mock_resp = Mock(spec=httpx.Response)
        mock_resp.status_code = HTTPStatus.OK
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "sync-ok"}}],
            "usage": {},
        }
        mock_resp.raise_for_status.return_value = None

        class _FakeSyncClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return mock_resp

        with patch("doctoragent.model.provider.httpx.Client", _FakeSyncClient):
            result = provider.chat_completion_sync(
                [{"role": "user", "content": "hi"}]
            )

    assert result == "sync-ok"


async def test_async_chat_still_works_after_sync_call(
    connection: Connection,
) -> None:
    """End-to-end regression: after a sync call, the async path must work.

    This is the exact failure mode the bug caused — the sync call poisoned
    ``self.client`` so the next ``await chat_completion(...)`` raised
    "Event loop is closed". With the fix the sync path uses an isolated
    sync client and the async client stays bound to the running loop.
    """
    provider = OpenAICompatibleProvider(connection)

    # Sync path: isolate via a fake sync client (no network).
    sync_resp = Mock(spec=httpx.Response)
    sync_resp.status_code = HTTPStatus.OK
    sync_resp.json.return_value = {
        "choices": [{"message": {"content": "sync"}}],
        "usage": {},
    }
    sync_resp.raise_for_status.return_value = None

    class _FakeSyncClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return sync_resp

    with patch("doctoragent.model.provider.httpx.Client", _FakeSyncClient):
        assert provider.chat_completion_sync([{"role": "user", "content": "x"}]) == "sync"

    # Async path: must STILL work after the sync call (the bug broke this).
    async_resp = Mock(spec=httpx.Response)
    async_resp.status_code = HTTPStatus.OK
    async_resp.json.return_value = {
        "choices": [{"message": {"content": "async"}}],
        "usage": {},
    }
    async_resp.raise_for_status.return_value = None
    with patch.object(
        provider.client, "post", new=AsyncMock(return_value=async_resp)
    ):
        result = await provider.chat_completion([{"role": "user", "content": "y"}])
    assert result == "async"
    await provider.close()


def test_chat_completion_sync_returns_tools_response(connection: Connection) -> None:
    """The sync path parses native tool_calls just like the async path."""
    provider = OpenAICompatibleProvider(connection)
    sync_resp = Mock(spec=httpx.Response)
    sync_resp.status_code = HTTPStatus.OK
    sync_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "search",
                                "arguments": '{"q": "warfarin"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }
    sync_resp.raise_for_status.return_value = None

    class _FakeSyncClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return sync_resp

    with patch("doctoragent.model.provider.httpx.Client", _FakeSyncClient):
        resp = provider.chat_completion_sync(
            [{"role": "user", "content": "search"}],
            tools=[{"type": "function", "function": {"name": "search"}}],
        )
    assert hasattr(resp, "tool_calls")
    assert resp.tool_calls[0]["name"] == "search"
    assert resp.tool_calls[0]["arguments"] == {"q": "warfarin"}


def test_chat_completion_sync_reasoning_content_fallback(connection: Connection) -> None:
    """Sync path falls back to reasoning_content when content is null
    (DeepSeek / Qwen reasoning models emit content under reasoning_content)."""
    provider = OpenAICompatibleProvider(connection)
    sync_resp = Mock(spec=httpx.Response)
    sync_resp.status_code = HTTPStatus.OK
    sync_resp.json.return_value = {
        "choices": [{"message": {"content": None, "reasoning_content": "thought"}}],
        "usage": {},
    }
    sync_resp.raise_for_status.return_value = None

    class _FakeSyncClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return sync_resp

    with patch("doctoragent.model.provider.httpx.Client", _FakeSyncClient):
        result = provider.chat_completion_sync([{"role": "user", "content": "hi"}])
    assert result == "thought"




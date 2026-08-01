"""Tests for token/cost tracking, fallback chain, and streaming.

These cover the :class:`OpenAICompatibleProvider` enhancements added alongside
the original ``test_provider.py``: ``usage`` parsing, ``reset_usage`` /
``get_usage`` snapshots, the 429/5xx + ``RequestError`` fallback chain, and
``chat_completion_stream`` SSE assembly.
"""

import asyncio
import logging
from http import HTTPStatus
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from doctoragent.connections.models import Connection, PlatformType
from doctoragent.model.provider import (
    ChatCompletionResponse,
    OpenAICompatibleProvider,
    UsageStats,
)


@pytest.fixture
def connection() -> Connection:
    """Default local connection fixture (mirrors test_provider.py)."""
    return Connection(
        name="test",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:1234",
        model_name="test-model",
    )


def _ok_response(payload: dict) -> Mock:
    """Build a mocked httpx.Response that decodes to *payload*."""
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.OK
    mock_response.json.return_value = payload
    mock_response.raise_for_status.return_value = None
    return mock_response


def _status_error_response(status: int) -> Mock:
    """Build a mocked httpx.Response whose raise_for_status raises *status*."""
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = status
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"{status}", request=Mock(spec=httpx.Request), response=mock_response
    )
    return mock_response


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


async def test_usage_recorded_on_chat_completion(connection: Connection) -> None:
    """response.usage is accumulated into get_usage()."""
    mock_response = _ok_response(
        {
            "choices": [{"message": {"content": "hi back"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    )

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat_completion([{"role": "user", "content": "hi"}])

    assert result == "hi back"
    usage = provider.get_usage()
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.total_tokens == 30
    assert usage.requests == 1
    assert usage.cost_usd == 0.0
    await provider.close()


async def test_usage_not_recorded_without_usage_block(connection: Connection) -> None:
    """A response without a usage block leaves stats at zero."""
    mock_response = _ok_response({"choices": [{"message": {"content": "hi"}}]})

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)):
        await provider.chat_completion([{"role": "user", "content": "hi"}])

    usage = provider.get_usage()
    assert usage.total_tokens == 0
    assert usage.requests == 0
    await provider.close()


async def test_reset_usage_returns_snapshot_and_clears(connection: Connection) -> None:
    """reset_usage() returns the pre-reset snapshot and zeroes the stats."""
    mock_response = _ok_response(
        {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    )

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)):
        await provider.chat_completion([{"role": "user", "content": "hi"}])

    snapshot = provider.reset_usage()
    assert isinstance(snapshot, UsageStats)
    assert snapshot.total_tokens == 30
    assert snapshot.requests == 1

    cleared = provider.get_usage()
    assert cleared.total_tokens == 0
    assert cleared.requests == 0
    # Snapshot is independent of the (now reset) internal stats.
    assert snapshot.total_tokens == 30
    await provider.close()


async def test_chat_completion_with_tools_carries_usage(connection: Connection) -> None:
    """ChatCompletionResponse exposes the raw usage block and records it."""
    mock_response = _ok_response(
        {
            "choices": [{"message": {"content": "ok", "tool_calls": []}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
    )

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "post", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat_completion(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        )

    assert isinstance(result, ChatCompletionResponse)
    assert result.usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert provider.get_usage().total_tokens == 3
    await provider.close()


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


async def test_fallback_on_503(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Primary 503 (after tenacity retry) triggers a successful fallback."""
    # Speed up tenacity's exponential backoff (3 primary attempts => ~3s).
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    success = _ok_response(
        {
            "choices": [{"message": {"content": "from-fallback"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }
    )
    error = _status_error_response(HTTPStatus.SERVICE_UNAVAILABLE)

    calls = {"n": 0}

    async def fake_post(*_args: object, **_kwargs: object) -> Mock:
        calls["n"] += 1
        # First 3 calls = primary (retried 3x by tenacity), then fallback.
        if calls["n"] <= 3:
            return error
        return success

    provider = OpenAICompatibleProvider(connection)
    provider.set_fallback_model("fallback-model")

    with caplog.at_level(logging.WARNING, logger="doctoragent.model.provider"):
        with patch.object(provider.client, "post", new=AsyncMock(side_effect=fake_post)):
            result = await provider.chat_completion([{"role": "user", "content": "hi"}])

    assert result == "from-fallback"
    # 3 primary retries (all 503) + 1 successful fallback attempt.
    assert calls["n"] == 4
    usage = provider.get_usage()
    assert usage.total_tokens == 12
    assert usage.requests == 1
    assert "falling back" in caplog.text
    assert "fallback-model" in caplog.text
    await provider.close()


async def test_fallback_reraises_original_when_fallback_also_fails(
    connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the fallback also fails, the original (primary) exception is raised."""
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    error_503 = _status_error_response(HTTPStatus.SERVICE_UNAVAILABLE)

    provider = OpenAICompatibleProvider(connection)
    provider.set_fallback_model("fallback-model")

    # Every attempt fails with 503.
    with patch.object(provider.client, "post", new=AsyncMock(return_value=error_503)):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await provider.chat_completion([{"role": "user", "content": "hi"}])

    # Original primary exception (503) is surfaced, not the fallback's.
    assert exc_info.value.response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    await provider.close()


async def test_no_fallback_when_not_configured(
    connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a fallback model, 503 propagates after tenacity gives up."""
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    error_503 = _status_error_response(HTTPStatus.SERVICE_UNAVAILABLE)
    provider = OpenAICompatibleProvider(connection)
    assert provider._fallback_model is None

    with patch.object(provider.client, "post", new=AsyncMock(return_value=error_503)):
        with pytest.raises(httpx.HTTPStatusError):
            await provider.chat_completion([{"role": "user", "content": "hi"}])

    await provider.close()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal stand-in for an httpx streaming Response."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def raise_for_status(self) -> None:  # noqa: D401 - mirrors httpx API
        return None

    async def aiter_lines(self):  # type: ignore[no-untyped-def]
        for line in self._lines:
            yield line


class _FakeStreamCM:
    """Async context manager yielding a :class:`_FakeStreamResponse`."""

    def __init__(self, lines: list[str]) -> None:
        self._response = _FakeStreamResponse(lines)

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False


async def test_stream_assembles_content(connection: Connection) -> None:
    """SSE deltas are yielded in order and the final usage chunk is recorded."""
    lines = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
        "data: [DONE]",
    ]

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "stream", return_value=_FakeStreamCM(lines)):
        chunks = [
            c
            async for c in provider.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            )
        ]

    assert "".join(chunks) == "Hello world"
    usage = provider.get_usage()
    assert usage.total_tokens == 7
    assert usage.requests == 1
    await provider.close()


async def test_stream_skips_non_data_lines(connection: Connection) -> None:
    """SSE comments / keep-alive lines are ignored."""
    lines = [
        ": keep-alive",
        "",
        'data: {"choices":[{"delta":{"content":"A"}}]}',
        "data: [DONE]",
    ]

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "stream", return_value=_FakeStreamCM(lines)):
        chunks = [
            c async for c in provider.chat_completion_stream([{"role": "user", "content": "x"}])
        ]

    assert chunks == ["A"]
    await provider.close()


async def test_stream_records_usage_without_done_marker(connection: Connection) -> None:
    """A usage chunk is recorded even when the server omits ``[DONE]``."""
    lines = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}',
    ]

    provider = OpenAICompatibleProvider(connection)
    with patch.object(provider.client, "stream", return_value=_FakeStreamCM(lines)):
        chunks = [
            c async for c in provider.chat_completion_stream([{"role": "user", "content": "x"}])
        ]

    assert chunks == ["hi"]
    assert provider.get_usage().total_tokens == 3
    await provider.close()

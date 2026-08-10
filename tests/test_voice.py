# mypy: ignore-errors
"""Tests for the voice conversation chain (ASR + TTS)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from doctoragent.config import VoiceConfig
from doctoragent.voice.service import (
    VoiceService,
    VoiceUnavailable,
    _audio_endpoint,
)


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "患者用药史记录完整"})
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(
                200, content=b"FAKE-MP3-DATA", headers={"content-type": "audio/mpeg"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _mock_service() -> VoiceService:
    config = VoiceConfig(
        transcribe_base_url="http://llm/v1",
        transcribe_model="whisper",
        tts_base_url="http://llm/v1",
        tts_model="tts-1",
        tts_voice="nova",
    )
    return VoiceService(config, http_client=httpx.AsyncClient(transport=_mock_transport()))


@pytest.fixture
async def mock_http() -> Any:
    async with httpx.AsyncClient(transport=_mock_transport()) as client:
        yield client
        await client.aclose()


def test_audio_endpoint_v1_normalization() -> None:
    assert _audio_endpoint("http://host", "audio/transcriptions") == (
        "http://host/v1/audio/transcriptions"
    )
    assert _audio_endpoint("http://host/v1", "audio/speech") == "http://host/v1/audio/speech"


def test_capabilities_when_unconfigured() -> None:
    svc = VoiceService(VoiceConfig())
    assert svc.transcribe_available is False
    assert svc.tts_available is False
    assert svc.enabled is False
    assert svc.availability()["enabled"] is False


def test_capabilities_when_configured() -> None:
    svc = VoiceService(VoiceConfig(
        transcribe_base_url="http://x", transcribe_model="w",
        tts_base_url="http://x", tts_model="t",
    ))
    assert svc.transcribe_available is True
    assert svc.tts_available is True
    assert svc.enabled is True


def test_voice_unavailable_message() -> None:
    svc = VoiceService(VoiceConfig())
    import asyncio

    with pytest.raises(VoiceUnavailable, match="transcribe"):
        asyncio.run(svc.transcribe(b"x"))


@pytest.mark.asyncio
async def test_transcribe(mock_http: httpx.AsyncClient) -> None:
    svc = VoiceService(
        VoiceConfig(
            transcribe_base_url="http://llm/v1",
            transcribe_model="whisper",
        ),
        http_client=mock_http,
    )
    text = await svc.transcribe(b"\x00audio", filename="a.webm")
    assert text == "患者用药史记录完整"


@pytest.mark.asyncio
async def test_synthesize(mock_http: httpx.AsyncClient) -> None:
    svc = VoiceService(
        VoiceConfig(
            tts_base_url="http://llm/v1",
            tts_model="tts-1",
            tts_voice="nova",
        ),
        http_client=mock_http,
    )
    audio = await svc.synthesize("您好，用药方案安全")
    assert audio == b"FAKE-MP3-DATA"

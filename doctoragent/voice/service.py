"""Voice conversation chain: ASR (speech → text) + TTS (text → speech).

Implements the **08-voice** capability. Both directions plug into any
OpenAI-compatible audio endpoint, so a single local gateway (e.g. Ollama with a
speech model, or a cloud provider) can serve transcription and synthesis. When
no endpoint is configured the service reports itself disabled and callers
return HTTP 501.

* :meth:`VoiceService.transcribe` — POST ``/v1/audio/transcriptions``
  (multipart ``file`` + ``model``), returns the transcript.
* :meth:`VoiceService.synthesize` — POST ``/v1/audio/speech``
  (JSON ``{model, input, voice}``), returns raw audio bytes.

See ``doctoragent/config.py`` → :class:`VoiceConfig` for wiring.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from doctoragent._utils import NoCloseClient

logger = logging.getLogger(__name__)


def _audio_endpoint(base_url: str, path: str) -> str:
    """Join an OpenAI-style base URL to an audio endpoint path.

    Appends ``/v1/`` when the base URL does not already carry an API version
    segment, so ``https://host`` and ``https://host/v1`` both work.
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return urljoin(base + "/", path.lstrip("/"))


class VoiceService:
    """ASR + TTS over OpenAI-compatible audio endpoints."""

    def __init__(self, config: Any, http_client: httpx.AsyncClient | None = None) -> None:
        # ``config`` is a doctoragent.config.VoiceConfig (duck-typed so this
        # module stays importable without importing the whole config module).
        self.config = config
        self._http_client = http_client

    def _make_client(self) -> Any:
        """Return the injected client (no-op ctx) or a fresh short-lived one."""
        if self._http_client is not None:
            return NoCloseClient(self._http_client)
        return httpx.AsyncClient(timeout=60.0)

    # ── capability ──────────────────────────────────────────────────

    @property
    def transcribe_available(self) -> bool:
        return bool(self.config.transcribe_base_url and self.config.transcribe_model)

    @property
    def tts_available(self) -> bool:
        return bool(self.config.tts_base_url and self.config.tts_model)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True)) and (
            self.transcribe_available or self.tts_available
        )

    def availability(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transcribe": self.transcribe_available,
            "tts": self.tts_available,
            "tts_voice": self.config.tts_voice,
        }

    # ── ASR ─────────────────────────────────────────────────────────

    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "audio.webm",
        *,
        mime: str = "audio/webm",
        language: str | None = None,
    ) -> str:
        """Transcribe an audio clip to text (speech-to-text)."""
        if not self.transcribe_available:
            raise VoiceUnavailableError("transcribe")
        url = _audio_endpoint(self.config.transcribe_base_url, "audio/transcriptions")
        headers: dict[str, str] = {}
        if self.config.transcribe_api_key:
            headers["Authorization"] = f"Bearer {self.config.transcribe_api_key}"
        data: dict[str, Any] = {"model": self.config.transcribe_model}
        if language:
            data["language"] = language
        async with self._make_client() as client:
            resp = await client.post(
                url,
                headers=headers,
                data=data,
                files={"file": (filename, audio_bytes, mime)},
            )
            resp.raise_for_status()
            body = resp.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VoiceError(f"Transcription returned empty result: {body}")
        return text.strip()

    # ── TTS ─────────────────────────────────────────────────────────

    async def synthesize(self, text: str, *, voice: str | None = None) -> bytes:
        """Synthesize speech audio for *text* (text-to-speech)."""
        if not self.tts_available:
            raise VoiceUnavailableError("tts")
        url = _audio_endpoint(self.config.tts_base_url, "audio/speech")
        headers = {"Content-Type": "application/json"}
        if self.config.tts_api_key:
            headers["Authorization"] = f"Bearer {self.config.tts_api_key}"
        payload = {
            "model": self.config.tts_model,
            "input": text,
            "voice": voice or self.config.tts_voice,
        }
        async with self._make_client() as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
        if not resp.content:
            raise VoiceError("Synthesis returned empty audio")
        return resp.content


class VoiceError(Exception):
    """Voice service failure."""


class VoiceUnavailableError(VoiceError):
    """A voice capability is disabled (no endpoint configured)."""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(
            f"Voice '{capability}' is not configured — set "
            f"DOCTORAGENT_VOICE__*_BASE_URL / *_MODEL environment variables."
        )

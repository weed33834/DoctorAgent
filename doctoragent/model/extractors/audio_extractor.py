"""Audio content extractor (ASR-based).

Prefers ``openai-whisper`` and falls back to ``faster-whisper``. The whisper
model is large and is therefore not part of the ``[multimodal]`` extras;
users can manually ``pip install openai-whisper`` or
``pip install faster-whisper`` to enable it.

When a dependency is missing or transcription fails it degrades to returning
empty text + ``method="none"``. The model is loaded lazily on the first
``extract`` call and reused from cache afterwards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from doctoragent._utils import truncate_text
from doctoragent.model.extensions import AUDIO_EXTENSIONS, mime_type
from doctoragent.model.extractors.base import (
    MAX_EXTRACTION_CHARS,
    ContentExtractor,
    ExtractionResult,
)

logger = logging.getLogger(__name__)


def _import_whisper() -> tuple[str | None, Any]:
    """Lazily import a whisper implementation.

    Returns:
        A tuple of ``("openai"|"faster", module)``; returns ``(None, None)``
        when missing.
    """
    try:
        import whisper  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        return "openai", whisper
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except ImportError:
        return None, None
    return "faster", faster_whisper


class AudioContentExtractor(ContentExtractor):
    """Audio ASR content extractor. The model is loaded on demand and cached."""

    def __init__(self) -> None:
        # Cache the loaded whisper model to avoid reloading it on every extraction.
        self._model: Any = None
        self._model_kind: str | None = None

    def supports(self, path: Path) -> bool:
        ext = path.suffix.lower().lstrip(".")
        return ext in AUDIO_EXTENSIONS

    def extract(self, path: Path, max_chars: int = MAX_EXTRACTION_CHARS) -> ExtractionResult:
        ext = path.suffix.lower().lstrip(".")
        mime = mime_type(ext)
        kind, module = _import_whisper()
        if kind is None or module is None:
            logger.debug("whisper 未安装，跳过 ASR: %s", path)
            return ExtractionResult(text="", method="none", mime_type=mime)
        try:
            text = self._transcribe(path, kind, module)
            text = text or ""
            text, truncated = truncate_text(text, max_chars)
            return ExtractionResult(
                text=text,
                method="asr",
                mime_type=mime,
                char_count=len(text),
                truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("ASR 提取失败 %s: %s", path, exc)
            return ExtractionResult(text="", method="none", mime_type=mime)

    def _transcribe(self, path: Path, kind: str, module: Any) -> str:
        """Run transcription according to the whisper implementation; loads the model lazily."""
        if self._model is None or self._model_kind != kind:
            if kind == "openai":
                self._model = module.load_model("base")
            else:  # faster-whisper
                self._model = module.WhisperModel("base", device="cpu", compute_type="int8")
            self._model_kind = kind
        if kind == "openai":
            result = self._model.transcribe(str(path))
            # openai-whisper may return a dict (older) or a Transcription
            # NamedTuple / object with a ``.text`` attribute (newer).
            text = getattr(result, "text", None)
            if text is None and isinstance(result, dict):
                text = result.get("text", "")
            return str(text or "")
        # faster-whisper returns (segments, info).
        segments, _info = self._model.transcribe(str(path))
        return " ".join(str(seg.text) for seg in segments)

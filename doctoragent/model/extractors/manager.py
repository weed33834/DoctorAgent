"""Multimodal extractor manager: routes files to the appropriate extractor by type."""

from __future__ import annotations

from pathlib import Path

from doctoragent.model.extractors.audio_extractor import AudioContentExtractor
from doctoragent.model.extractors.base import (
    MAX_EXTRACTION_CHARS,
    ContentExtractor,
    ExtractionResult,
)
from doctoragent.model.extractors.image_extractor import ImageContentExtractor
from doctoragent.model.extractors.text_extractor import TextContentExtractor


class ExtractionManager:
    """Dispatches to different extractors by file type.

    The default order is ``[text, image, audio]``: try text/document
    extraction first, then image OCR, then audio ASR. The first extractor
    whose ``supports`` returns ``True`` performs the actual extraction. When
    no extractor matches, an empty ``method="none"`` result is returned.
    """

    def __init__(self, extractors: list[ContentExtractor] | None = None) -> None:
        # Default order: try [text, image, audio].
        self._extractors: list[ContentExtractor] = (
            extractors
            if extractors is not None
            else [
                TextContentExtractor(),
                ImageContentExtractor(),
                AudioContentExtractor(),
            ]
        )

    def extract(self, path: Path, max_chars: int = MAX_EXTRACTION_CHARS) -> ExtractionResult:
        """Try extractors in order; the first one whose ``supports`` returns True wins."""
        for extractor in self._extractors:
            if extractor.supports(path):
                return extractor.extract(path, max_chars=max_chars)
        return ExtractionResult(text="", method="none", mime_type=None)

    def supports(self, path: Path) -> bool:
        """Return whether any extractor supports the file."""
        return any(extractor.supports(path) for extractor in self._extractors)

"""Multimodal content extractors.

The core vault functionality does not depend on any third-party multimodal
library; installing the ``[multimodal]`` extras enables PDF/DOCX text
extraction and image OCR. The whisper model is large, so users must manually
install ``openai-whisper`` or ``faster-whisper`` to enable audio
transcription.
"""

from doctoragent.model.extractors.audio_extractor import AudioContentExtractor
from doctoragent.model.extractors.base import (
    MAX_EXTRACTION_CHARS,
    ContentExtractor,
    DocumentMetadata,
    DocumentStructure,
    ExtractionResult,
)
from doctoragent.model.extractors.image_extractor import ImageContentExtractor
from doctoragent.model.extractors.manager import ExtractionManager
from doctoragent.model.extractors.text_extractor import TextContentExtractor

__all__ = [
    "AudioContentExtractor",
    "ContentExtractor",
    "DocumentMetadata",
    "DocumentStructure",
    "ExtractionManager",
    "ExtractionResult",
    "ImageContentExtractor",
    "MAX_EXTRACTION_CHARS",
    "TextContentExtractor",
]

"""Base types for multimodal content extractors.

The core vault functionality does not depend on any optional third-party
library; multimodal extraction dependencies (pypdf, python-docx, Pillow,
pytesseract, whisper, etc.) are provided via the ``[multimodal]`` extras.
When a dependency is missing the extractor degrades to returning empty text
rather than raising.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default maximum number of characters to extract (keeps the prompt
# from blowing up the context window).
MAX_EXTRACTION_CHARS = 8000


@dataclass
class DocumentStructure:
    """Structured representation of a document's layout.

    Captures the logical structure of a document so downstream consumers
    (RAG chunking, classification, search) can operate on semantic units
    rather than a flat text blob.

    Attributes:
        headings: List of ``(level, text)`` tuples representing the
            document's heading hierarchy (level 1 = top-level ``#``/H1).
        paragraphs: List of paragraph text strings (non-heading body text).
        tables: List of tables rendered as markdown strings.
        list_items: List of list-item text strings (bullet/numbered items).
    """

    headings: list[tuple[int, str]] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON storage."""
        return {
            "headings": [list(h) for h in self.headings],
            "paragraphs": list(self.paragraphs),
            "tables": list(self.tables),
            "list_items": list(self.list_items),
        }


@dataclass
class DocumentMetadata:
    """Metadata extracted from a document.

    All fields default to ``None``/empty so extractors only populate what
    they can reliably determine for a given format.

    Attributes:
        author: Document author, if present in file metadata.
        title: Document title, if present.
        created_at: Creation timestamp (ISO-8601 string when available).
        modified_at: Last-modified timestamp (ISO-8601 string when available).
        page_count: Number of pages (for paginated formats like PDF).
        word_count: Approximate word count of the extracted text.
        language: Detected/declared language code (e.g. ``"en"``, ``"zh"``).
        producer: Producing application (e.g. ``"Microsoft Office Word"``).
    """

    author: str | None = None
    title: str | None = None
    created_at: str | None = None
    modified_at: str | None = None
    page_count: int | None = None
    word_count: int | None = None
    language: str | None = None
    producer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, omitting ``None`` values."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ExtractionResult:
    """Content extraction result.

    Attributes:
        text: The extracted text content (may be an empty string).
        method: The extraction method, one of ``"text" | "ocr" | "asr" | "none"``.
            ``"none"`` means nothing was extracted (dependency missing / failed / unsupported).
        mime_type: The inferred file MIME type, or ``None`` when unknown.
        char_count: The actual number of characters returned (after truncation).
        truncated: Whether the text was truncated because it exceeded ``max_chars``.
        structure: Optional :class:`DocumentStructure` with headings/paragraphs/tables.
        metadata: Optional :class:`DocumentMetadata` with author/dates/page count.
        ocr_confidence: For OCR results, the average word-level confidence in [0, 100].
        ocr_language: For OCR results, the Tesseract language code used (e.g. ``"eng"``).
    """

    text: str
    method: str
    mime_type: str | None = None
    char_count: int = 0
    truncated: bool = False
    structure: DocumentStructure | None = None
    metadata: DocumentMetadata | None = None
    ocr_confidence: float | None = None
    ocr_language: str | None = None


class ContentExtractor(ABC):
    """Abstract base class for content extractors.

    Implementers must declare ``supports`` (a quick check by extension, etc.)
    and ``extract`` (the actual text extraction). On failure an empty result
    with ``method="none"`` should be returned so the caller can degrade
    gracefully; in principle no exception is raised.
    """

    @abstractmethod
    def supports(self, path: Path) -> bool:
        """Determine whether this extractor supports the file at the given path."""

    @abstractmethod
    def extract(self, path: Path, max_chars: int = MAX_EXTRACTION_CHARS) -> ExtractionResult:
        """Extract the file content as text.

        Parameters:
            path: The file path.
            max_chars: The maximum number of characters to return; excess text is truncated.
        """

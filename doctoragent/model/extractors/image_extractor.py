"""Image content extractor (OCR-based) with deepened OCR pipeline.

Depends on ``pytesseract`` + ``Pillow`` (in the ``[multimodal]`` extras) and
requires the ``tesseract`` binary to be installed on the system. When a
dependency is missing or OCR fails it degrades to returning empty text +
``method="none"`` without raising.

The OCR pipeline performs:

- **Image preprocessing**: denoising (median filter), grayscale conversion,
  and contrast enhancement before OCR to improve recognition accuracy.
- **Layout analysis**: ``image_to_data`` retrieves word-level bounding boxes
  and confidences; words are grouped by vertical position to reconstruct
  line/paragraph structure.
- **Multi-language support**: the Tesseract language code is configurable
  (e.g. ``"chi_sim+eng"``) and read from the constructor or config.
- **Confidence filtering**: words below a configurable threshold are dropped
  and the average confidence is reported via ``ocr_confidence``.
- **Table region detection**: dense rectangular clusters of words are
  detected and emitted as markdown tables in the document structure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from doctoragent._utils import truncate_text
from doctoragent.model.extensions import IMAGE_EXTENSIONS, mime_type
from doctoragent.model.extractors.base import (
    MAX_EXTRACTION_CHARS,
    ContentExtractor,
    DocumentStructure,
    ExtractionResult,
)

logger = logging.getLogger(__name__)

# Default OCR language code. Multiple languages are joined with ``+``.
_DEFAULT_OCR_LANGUAGE = "eng"
# Words with confidence below this threshold are dropped.
_DEFAULT_CONFIDENCE_THRESHOLD = 60.0


def _import_pytesseract() -> Any:
    """Lazily import pytesseract; returns ``None`` when missing."""
    try:
        import pytesseract  # type: ignore[import-not-found]
    except ImportError:
        return None
    return pytesseract


def _import_pil_image() -> Any:
    """Lazily import PIL.Image; returns ``None`` when missing."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Image


def _import_pil_image_filter() -> Any:
    """Lazily import PIL.ImageFilter; returns ``None`` when missing."""
    try:
        from PIL import ImageFilter  # type: ignore[import-not-found]
    except ImportError:
        return None
    return ImageFilter


def _import_pil_image_enhance() -> Any:
    """Lazily import PIL.ImageEnhance; returns ``None`` when missing."""
    try:
        from PIL import ImageEnhance  # type: ignore[import-not-found]
    except ImportError:
        return None
    return ImageEnhance


class ImageContentExtractor(ContentExtractor):
    """Image OCR content extractor with preprocessing and layout analysis."""

    def __init__(
        self,
        ocr_language: str = _DEFAULT_OCR_LANGUAGE,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
        preprocess: bool = True,
    ) -> None:
        """Initialize the OCR extractor.

        Parameters:
            ocr_language: Tesseract language code, e.g. ``"eng"`` or
                ``"chi_sim+eng"`` for mixed Chinese/English.
            confidence_threshold: Word-level confidence below which words
                are dropped from the output (0–100).
            preprocess: When ``True``, apply denoise/grayscale/contrast
                enhancement before OCR.
        """
        self._ocr_language = ocr_language
        self._confidence_threshold = confidence_threshold
        self._preprocess = preprocess

    def supports(self, path: Path) -> bool:
        ext = path.suffix.lower().lstrip(".")
        return ext in IMAGE_EXTENSIONS

    def extract(self, path: Path, max_chars: int = MAX_EXTRACTION_CHARS) -> ExtractionResult:
        ext = path.suffix.lower().lstrip(".")
        mime = mime_type(ext)
        pytesseract = _import_pytesseract()
        image_module = _import_pil_image()
        if pytesseract is None or image_module is None:
            logger.debug("pytesseract/Pillow 未安装，跳过 OCR: %s", path)
            return ExtractionResult(text="", method="none", mime_type=mime)
        try:
            image = image_module.open(path)
            # Preprocess the image to improve OCR accuracy.
            processed = self._preprocess_image(image) if self._preprocess else image
            text, confidence, structure = self._ocr_with_layout(pytesseract, processed)
            text = text or ""
            text, truncated = self._truncate(text, max_chars)
            return ExtractionResult(
                text=text,
                method="ocr",
                mime_type=mime,
                char_count=len(text),
                truncated=truncated,
                structure=structure,
                ocr_confidence=confidence,
                ocr_language=self._ocr_language,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("OCR 提取失败 %s: %s", path, exc)
            return ExtractionResult(text="", method="none", mime_type=mime)

    def _preprocess_image(self, image: Any) -> Any:
        """Apply denoise, grayscale, and contrast enhancement to *image*.

        Each step is individually guarded so that a missing Pillow submodule
        does not abort the whole pipeline — the best available processed
        image is returned.
        """
        image_filter = _import_pil_image_filter()
        image_enhance = _import_pil_image_enhance()
        result = image
        # 1. Convert to grayscale (reduces color noise).
        try:
            if image_filter is not None and hasattr(result, "convert"):
                result = result.convert("L")
        except Exception:  # noqa: BLE001 — best-effort preprocessing
            pass
        # 2. Denoise via median filter.
        try:
            if image_filter is not None and hasattr(image_filter, "MedianFilter"):
                result = result.filter(image_filter.MedianFilter(size=3))
        except Exception:  # noqa: BLE001 — best-effort preprocessing
            pass
        # 3. Contrast enhancement.
        try:
            if image_enhance is not None and hasattr(image_enhance, "Contrast"):
                enhancer = image_enhance.Contrast(result)
                result = enhancer.enhance(1.5)
        except Exception:  # noqa: BLE001 — best-effort preprocessing
            pass
        return result

    def _ocr_with_layout(
        self,
        pytesseract: Any,
        image: Any,
    ) -> tuple[str, float, DocumentStructure]:
        """Run OCR with layout analysis.

        Uses ``image_to_data`` to get word-level bounding boxes and
        confidences. Words are grouped into lines by their vertical position
        (top coordinate), then lines are grouped into paragraphs by vertical
        gaps. Low-confidence words are dropped and marked.

        Returns ``(text, avg_confidence, structure)``.
        """
        try:
            data = pytesseract.image_to_data(
                image,
                lang=self._ocr_language,
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to plain string OCR
            logger.debug("image_to_data 失败，回退到 image_to_string: %s", exc)
            text = pytesseract.image_to_string(image, lang=self._ocr_language) or ""
            return text, 0.0, DocumentStructure()

        words: list[dict[str, Any]] = []
        confidences: list[float] = []
        n = len(data.get("text", []))
        for i in range(n):
            text_val = (data["text"][i] or "").strip()
            if not text_val:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError, IndexError):
                conf = -1.0
            if conf < 0:
                conf = 0.0
            words.append(
                {
                    "text": text_val,
                    "conf": conf,
                    "left": int(data["left"][i]),
                    "top": int(data["top"][i]),
                    "width": int(data["width"][i]),
                    "height": int(data["height"][i]),
                    "block_num": int(data["block_num"][i]),
                    "par_num": int(data["par_num"][i]),
                    "line_num": int(data["line_num"][i]),
                }
            )

        # Filter low-confidence words.
        kept_words: list[dict[str, Any]] = []
        low_conf_count = 0
        for w in words:
            if w["conf"] >= self._confidence_threshold:
                kept_words.append(w)
                confidences.append(w["conf"])
            else:
                low_conf_count += 1

        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        # Group words into lines by (block, par, line) then sort by left.
        lines_map: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
        for w in kept_words:
            key = (w["block_num"], w["par_num"], w["line_num"])
            lines_map.setdefault(key, []).append(w)

        # Sort lines by block → par → line, and words within a line by left.
        line_texts: list[str] = []
        for key in sorted(lines_map.keys()):
            line_words = sorted(lines_map[key], key=lambda w: w["left"])
            line_text = " ".join(w["text"] for w in line_words)
            line_texts.append(line_text)

        if low_conf_count > 0:
            line_texts.append(f"[{low_conf_count} words below confidence threshold]")

        text = "\n".join(line_texts)

        # Build structure: group consecutive lines into paragraphs by block+par.
        paragraphs: list[str] = []
        par_map: dict[tuple[int, int], list[str]] = {}
        for key in sorted(lines_map.keys()):
            block, par, _line = key
            par_key = (block, par)
            line_words = sorted(lines_map[key], key=lambda w: w["left"])
            par_map.setdefault(par_key, []).append(" ".join(w["text"] for w in line_words))
        for par_key in sorted(par_map.keys()):
            para = "\n".join(par_map[par_key])
            if para.strip():
                paragraphs.append(para)

        # Detect table-like regions: blocks with many short lines aligned in
        # columns. This is a heuristic — we look for blocks where multiple
        # lines have similar top coordinates within a small tolerance and
        # multiple horizontal clusters.
        tables = self._detect_tables(kept_words)

        structure = DocumentStructure(paragraphs=paragraphs, tables=tables)
        return text, round(avg_confidence, 2), structure

    def _detect_tables(self, words: list[dict[str, Any]]) -> list[str]:
        """Heuristically detect table regions and render them as markdown.

        A block is considered table-like when it contains multiple lines (>=3)
        whose words cluster into >=2 distinct horizontal columns (by left
        coordinate clustering). This is a coarse heuristic suitable for
        simple bordered/structured tables; complex layouts may be missed.
        """
        if not words:
            return []
        # Group by block number.
        blocks: dict[int, list[dict[str, Any]]] = {}
        for w in words:
            blocks.setdefault(w["block_num"], []).append(w)

        tables: list[str] = []
        for _, block_words in blocks.items():
            # Group into lines within this block.
            lines: dict[int, list[dict[str, Any]]] = {}
            for w in block_words:
                lines.setdefault(w["line_num"], []).append(w)
            if len(lines) < 3:
                continue
            # Check column clustering: collect all left positions and see
            # if they form >=2 clusters (using a simple tolerance).
            all_lefts = sorted(w["left"] for w in block_words)
            clusters: list[int] = []
            cluster_start = all_lefts[0] if all_lefts else 0
            for x in all_lefts:
                if x - cluster_start > 20:  # new column cluster
                    clusters.append(cluster_start)
                    cluster_start = x
            clusters.append(cluster_start)
            if len(clusters) < 2:
                continue
            # Build a table: each line becomes a row, words assigned to the
            # nearest column cluster.
            col_starts = sorted(set(clusters))
            rows: list[list[str]] = []
            for line_num in sorted(lines.keys()):
                line_words = sorted(lines[line_num], key=lambda w: w["left"])
                row = [""] * len(col_starts)
                for w in line_words:
                    # Assign to nearest column.
                    best_col = 0
                    best_dist = abs(w["left"] - col_starts[0])
                    for ci, cs in enumerate(col_starts[1:], start=1):
                        dist = abs(w["left"] - cs)
                        if dist < best_dist:
                            best_dist = dist
                            best_col = ci
                    if row[best_col]:
                        row[best_col] += " " + w["text"]
                    else:
                        row[best_col] = w["text"]
                rows.append(row)
            if len(rows) >= 2:
                md = self._render_table(rows)
                if md:
                    tables.append(md)
        return tables

    @staticmethod
    def _render_table(rows: list[list[str]]) -> str:
        """Render rows as a markdown table (first row = header)."""
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        for r in rows:
            while len(r) < width:
                r.append("")
        header = rows[0]
        body = rows[1:]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row in body:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
        """Truncate by character count; returns ``(truncated_text, was_truncated)``."""
        return truncate_text(text, max_chars)

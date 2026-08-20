"""Shared utility helpers used across doctoragent modules.

This module exists to eliminate repeated boilerplate that previously lived in
several unrelated modules:

* ``async_to_sync`` — a synchronous wrapper around a coroutine that mirrors
  the ``concurrent.futures.ThreadPoolExecutor().submit(asyncio.run, ...)``
  pattern that was duplicated in :mod:`doctoragent.model.agent`,
  :mod:`doctoragent.model.provider`, :mod:`doctoragent.model.tools` (twice) and
  :mod:`doctoragent.model.skills`.
* ``open_sqlite`` — a SQLite connection factory that always enables WAL mode,
  ``busy_timeout=5000``, ``timeout=30`` and ``check_same_thread=False``. This
  was duplicated by every SQLite-backed class in
  :mod:`doctoragent.model.rag` (``MemorySystem``, ``BM25Search``,
  ``HybridRetriever``, ``ChunkStorage``) and by
  :class:`doctoragent.orchestration.task_store.TaskStore`.
* ``cosine_similarity`` — a numpy-backed cosine similarity implementation,
  previously duplicated by ``HybridRetriever`` and ``TaskStore``.
* ``truncate_text`` — a small text-truncation helper, previously duplicated
  by the text and image extractors.

Keeping these here lets the rest of the codebase depend on a single, tested
implementation instead of subtly drifting copies.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json as _json
import re as _re
import sqlite3
from pathlib import Path
from typing import Any

__all__ = [
    "async_to_sync",
    "open_sqlite",
    "cosine_similarity",
    "cosine_similarity_matrix",
    "truncate_text",
    "tokenize_for_fts",
    "tokenize_words",
    "extract_doc_text",
    "extract_doc_id",
    "extract_json",
]


def async_to_sync(coro: Any, *, timeout: float | None = None) -> Any:
    """Run *coro* to completion from synchronous code.

    Mirrors the ``concurrent.futures.ThreadPoolExecutor().submit(asyncio.run,
    ...)`` pattern that was previously inlined in several modules.  The
    coroutine runs on a worker thread so that an already-running event loop in
    the calling thread does not collide.

    Parameters
    ----------
    coro:
        An awaitable (typically the result of calling an ``async def``
        function). It is consumed by this call.
    timeout:
        Optional wall-clock timeout in seconds for the thread-pool wait. When
        ``None`` (default) the wait is unbounded.

    Returns
    -------
    The value produced by *coro*.

    Raises
    ------
    Any exception raised inside *coro* propagates directly to the caller.
    """
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result(timeout=timeout)


def open_sqlite(
    db_path: str | Path,
    *,
    row_factory: Any = None,
) -> sqlite3.Connection:
    """Open a SQLite connection configured for concurrent multi-thread use.

    The connection is configured with:

    * ``timeout=30`` — wait up to 30 s for a locked DB before raising.
    * ``check_same_thread=False`` — allow use from any thread (callers are
      responsible for serialising writes if they share a connection).
    * ``PRAGMA journal_mode=WAL`` — write-ahead logging for concurrent
      readers + a single writer.
    * ``PRAGMA busy_timeout=5000`` — SQLite-level busy timeout (ms).

    The caller owns the returned connection and is responsible for closing
    it (typically via a ``with`` block or ``try/finally``).
    """
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute the cosine similarity between two vectors using numpy.

    Returns ``0.0`` when the vectors differ in length or either has zero
    magnitude. The two duplicated implementations this replaces (in
    :class:`doctoragent.model.rag.HybridRetriever` and
    :class:`doctoragent.orchestration.task_store.TaskStore`) had identical
    semantics.
    """
    import numpy as np

    if len(a) != len(b):
        return 0.0
    a_arr = np.asarray(a, dtype=np.float32)
    b_arr = np.asarray(b, dtype=np.float32)
    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / (norm_a * norm_b))


def cosine_similarity_matrix(
    query: list[float],
    matrix: list[list[float]] | Any,
) -> list[float]:
    """Compute cosine similarity between *query* and every row of *matrix*.

    This is the vectorized batch counterpart of :func:`cosine_similarity`.
    It builds a single ``(n, d)`` numpy array and performs one matrix-vector
    product, which is dramatically faster than looping over rows in Python
    when the index holds thousands of vectors (the basis of the
    numpy-backed ANN index in :mod:`doctoragent.model.rag`).

    Parameters
    ----------
    query:
        A single ``d``-dimensional query vector.
    matrix:
        Either a list of ``d``-dimensional vectors or any object exposing a
        NumPy buffer (e.g. a pre-built ``np.ndarray``). Rows of differing
        length than *query* are skipped (scored ``0.0``) rather than raising.

    Returns
    -------
    A list of cosine-similarity floats, one per row of *matrix*, in the same
    order. Rows with zero magnitude or mismatched dimensionality are scored
    ``0.0``. Returns ``[]`` when *matrix* is empty.
    """
    import numpy as np

    if matrix is None:
        return []
    arr = np.asarray(matrix, dtype=np.float32)
    if arr.size == 0:
        return []
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    q = np.asarray(query, dtype=np.float32)
    if q.ndim != 1:
        q = q.reshape(-1)
    # Guard against dimension mismatch between query and matrix rows.
    if arr.shape[1] != q.shape[0]:
        # Fall back to a safe per-row computation preserving row order.
        out: list[float] = []
        for row in arr:
            row_list = list(row)
            if len(row_list) == len(query):
                out.append(cosine_similarity(query, row_list))
            else:
                out.append(0.0)
        return out

    norms = np.linalg.norm(arr, axis=1)
    q_norm = np.linalg.norm(q)
    denom = norms * q_norm
    # Avoid division by zero; rows with zero magnitude get score 0.
    safe = denom > 0
    scores = np.zeros(arr.shape[0], dtype=np.float32)
    if q_norm > 0:
        dots = arr @ q
        np.divide(dots, denom, out=scores, where=safe)
    return [float(s) for s in scores]


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate *text* to at most *max_chars* characters.

    Returns ``(truncated_text, was_truncated)``. ``was_truncated`` is ``True``
    only when the original text exceeded *max_chars*.
    """
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# ---------------------------------------------------------------------------
# CJK-aware tokenisation (jieba) — shared by FTS indexing, FTS query building,
# and keyword-overlap scorers. Previously every caller hand-rolled its own
# tokeniser (``re.findall(r"\w+")`` in self_evolution, per-character CJK split
# in skills_advanced, whitespace ``split()`` in the FTS search paths) — none of
# which segment Chinese into words, so BM25/FTS search on Chinese content was
# effectively broken. jieba is the standard Chinese segmentation library; when
# it is unavailable the helpers degrade gracefully to whitespace splitting so
# minimal installs and the test suite keep working.
# ---------------------------------------------------------------------------


_JIEBA_AVAILABLE: bool | None = None
_JIEBA_RE = _re.compile(r"[\x00-\x1f\x7f]")


def _get_jieba():
    """Lazily import jieba, caching the availability flag.

    Returns the jieba module or ``None`` when it is not installed.
    """
    global _JIEBA_AVAILABLE
    if _JIEBA_AVAILABLE is False:
        return None
    try:
        import jieba  # type: ignore[import-not-found]
    except ImportError:
        _JIEBA_AVAILABLE = False
        return None
    _JIEBA_AVAILABLE = True
    return jieba


def _segment(text: str) -> list[str]:
    """Segment *text* into word tokens using jieba when available.

    Strips control characters (which break FTS5 syntax) and empty tokens.
    Falls back to whitespace splitting when jieba is absent so behaviour
    matches the previous implementation for ASCII-only content.
    """
    if not text:
        return []
    cleaned = _JIEBA_RE.sub("", text)
    jieba = _get_jieba()
    if jieba is None:
        return [t for t in cleaned.split() if t]
    # cut_for_search yields a finer-grained superset of tokens, improving
    # recall on short queries that are a substring of an indexed word.
    return [t for t in jieba.cut_for_search(cleaned) if t.strip()]


def tokenize_for_fts(text: str) -> str:
    """Return *text* segmented into space-separated tokens for FTS5 indexing.

    FTS5's default ``unicode61`` tokenizer splits on whitespace, so feeding it
    jieba-segmented text (words separated by single spaces) makes Chinese
    content searchable word-by-word. Use this both when **inserting** indexed
    text and when **building** a MATCH query expression, so the query tokens
    align with the stored tokens.
    """
    return " ".join(_segment(text))


def tokenize_words(text: str) -> list[str]:
    """Return *text* segmented into a list of word tokens.

    For keyword-overlap / Jaccard-style scorers that previously hand-rolled
    tokenisation. Lowercases the result so matching is case-insensitive.
    """
    return [t.lower() for t in _segment(text)]


# ---------------------------------------------------------------------------
# Document field extraction — shared by agentic_rag, corrective_rag,
# knowledge_graph, and query_router. Previously duplicated as _doc_text /
# _doc_id / _doc_ref in 4 files with subtly different implementations.
# ---------------------------------------------------------------------------

# Keys checked in order for text extraction.
_DOC_TEXT_KEYS = ("text", "content", "chunk", "summary")
# Keys checked in order for identifier extraction.
_DOC_ID_KEYS = ("chunk_id", "doc_id", "task_id", "id", "title", "vault_path")


def extract_doc_text(doc: Any, *, max_chars: int | None = None) -> str:
    """Best-effort extraction of textual content from a document.

    Accepts plain strings, dicts with ``text``/``content``/``chunk``/
    ``summary`` keys, or objects with ``.text``/``.content`` attributes.
    Returns ``""`` when nothing usable is found.

    When *max_chars* is given, the result is truncated to that length with
    an ellipsis appended.
    """
    if doc is None:
        return ""
    if isinstance(doc, str):
        text = doc
    elif isinstance(doc, dict):
        text = ""
        for key in _DOC_TEXT_KEYS:
            value = doc.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
    else:
        text = getattr(doc, "text", "") or getattr(doc, "content", "") or ""
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


def extract_doc_id(doc: Any) -> str:
    """Best-effort extraction of a document identifier.

    Checks ``chunk_id``, ``doc_id``, ``task_id``, ``id``, ``title``,
    ``vault_path`` keys (for dicts) or the corresponding attributes.
    Returns ``""`` when no identifier is found.
    """
    if isinstance(doc, str):
        return ""
    if isinstance(doc, dict):
        for key in _DOC_ID_KEYS:
            value = doc.get(key)
            if value:
                return str(value)
    return (
        getattr(doc, "chunk_id", "")
        or getattr(doc, "doc_id", "")
        or getattr(doc, "task_id", "")
        or getattr(doc, "id", "")
        or ""
    )


# ---------------------------------------------------------------------------
# JSON extraction from LLM output — shared by agent, query_router,
# knowledge_graph, agentic_rag, corrective_rag, tree_of_thought,
# self_evolution, clinical_tools, dynamic_tools. Previously duplicated as
# _extract_json in agent.py and imported from there by 8 files.
# ---------------------------------------------------------------------------

# Pre-compiled regexes for JSON extraction.
_JSON_FENCE_RE = _re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", _re.DOTALL)
_JSON_BARE_RE = _re.compile(r"(\{.*\}|\[.*\])", _re.DOTALL)


def extract_json(text: str) -> Any:
    """Extract a JSON object/array from an LLM response.

    Tolerates ```` ```json ... ``` ```` fenced blocks and leading/trailing
    prose. Returns the parsed value or ``None``.

    Previously duplicated as ``_extract_json`` in
    :mod:`doctoragent.model.agent` — now centralised here.
    """
    if not text:
        return None
    match = _JSON_FENCE_RE.search(text)
    candidate = match.group(1) if match else text
    try:
        return _json.loads(candidate)
    except (_json.JSONDecodeError, ValueError):
        # Last resort: scan for the first {...} or [...] span.
        match = _JSON_BARE_RE.search(text)
        if match:
            try:
                return _json.loads(match.group(1))
            except (_json.JSONDecodeError, ValueError):
                return None
        return None

"""SQLite-backed vector store.

This is the default backend. It mirrors the cosine-similarity logic used by
the legacy ``vault_vectors`` table (numpy-backed exact similarity, no
external vector library required) but stores records in its own
``doctoragent_vectors`` table using the generic :class:`VectorRecord` schema so
it can be exercised standalone.

Behaviour matches the existing RAG default: exact numpy cosine similarity,
in-memory scoring over the full corpus. Suitable for small-to-medium
datasets; switch to the Chroma backend for enterprise scale.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

from doctoragent._utils import cosine_similarity_matrix
from doctoragent.model.vectorstore.base import (
    VectorRecord,
    VectorSearchResult,
    VectorStoreBackend,
)

logger = logging.getLogger(__name__)

# Vectors are stored as a struct-packed little-endian double array (same
# on-disk format as ``vault_vectors.vector_blob``) so reads are O(1) and
# do not require a JSON parse per row.
_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS doctoragent_vectors (
        id TEXT PRIMARY KEY,
        vector_blob BLOB NOT NULL,
        metadata TEXT,
        document TEXT,
        dim INTEGER NOT NULL
    )
"""


def _pack_vector(vector: list[float]) -> bytes:
    """Serialise a float list into a packed little-endian double array."""
    return struct.pack(f"<{len(vector)}d", *vector)


def _unpack_vector(blob: bytes, dim: int) -> list[float]:
    """Inverse of :func:`_pack_vector`."""
    return list(struct.unpack(f"<{dim}d", blob))


class SQLiteVectorStore(VectorStoreBackend):
    """Default vector store backed by a local SQLite file.

    Parameters
    ----------
    path:
        Filesystem path for the SQLite database. Parent directories are
        created on demand.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_TABLE_SQL)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce(record: VectorRecord | dict[str, Any]) -> VectorRecord:
        if isinstance(record, VectorRecord):
            return record
        return VectorRecord(**record)

    def _load_all(self) -> list[tuple[str, list[float], dict[str, Any], str]]:
        rows = self._conn.execute(
            "SELECT id, vector_blob, metadata, document, dim FROM doctoragent_vectors"
        ).fetchall()
        out: list[tuple[str, list[float], dict[str, Any], str]] = []
        for rid, blob, meta_json, document, dim in rows:
            try:
                vec = _unpack_vector(blob, dim)
            except struct.error:
                logger.warning("Corrupt vector blob for id=%r; skipping", rid)
                continue
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except json.JSONDecodeError:
                meta = {}
            out.append((rid, vec, meta, document or ""))
        return out

    # ------------------------------------------------------------------
    # VectorStoreBackend
    # ------------------------------------------------------------------
    def add(self, records: list[VectorRecord | dict[str, Any]]) -> None:
        coerced = [self._coerce(r) for r in records]
        with self._lock:
            for rec in coerced:
                blob = _pack_vector(rec.vector)
                self._conn.execute(
                    "INSERT OR REPLACE INTO doctoragent_vectors "
                    "(id, vector_blob, metadata, document, dim) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        rec.id,
                        blob,
                        json.dumps(rec.metadata, ensure_ascii=False),
                        rec.document,
                        len(rec.vector),
                    ),
                )

    def search(self, query_vector: list[float], top_k: int = 10) -> list[VectorSearchResult]:
        if top_k <= 0:
            return []
        with self._lock:
            rows = self._load_all()
        if not rows:
            return []
        # Batch cosine similarity via the shared numpy helper — it handles
        # dimension mismatch and zero-magnitude rows (scored 0.0) and returns
        # one score per row in input order.
        matrix = [vec for _, vec, _, _ in rows]
        scores = cosine_similarity_matrix(query_vector, matrix)
        scored: list[VectorSearchResult] = []
        for (rid, vec, meta, document), score in zip(rows, scores, strict=True):
            scored.append(
                VectorSearchResult(
                    record=VectorRecord(id=rid, vector=vec, metadata=meta, document=document),
                    score=score,
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "DELETE FROM doctoragent_vectors WHERE id = ?", [(i,) for i in ids]
            )

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM doctoragent_vectors").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

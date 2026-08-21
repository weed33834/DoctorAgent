"""Semantic response cache (M23 performance / M18 foundation).

A real LLM response cache keyed by the **semantic similarity** of the input
query rather than an exact string match. When a query is close enough to a
previously answered one, the cached response is returned — cutting latency
(TTFT) and cost for repeated / near-duplicate questions, which is very common
in clinical Q&A.

Design:
* An optional embedding provider turns each query into a vector. Without one,
  the cache degrades to a normalized-text exact match.
* Responses are stored in memory with an LRU/TTL eviction policy, plus an
  optional SQLite persistence layer so the cache survives restarts.
* A cosine-similarity threshold decides a hit; a configurable prefix/suffix
  blacklist avoids caching sensitive or patient-identifying content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cachetools import TTLCache as _CTTLCache

from doctoragent._utils import open_sqlite

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    """Delegate to the shared :func:`cosine_similarity` in :mod:`doctoragent._utils`.

    The shared helper uses numpy for vectorised computation and is also used
    by HybridRetriever, AnnIndex, and TaskStore.
    """
    from doctoragent._utils import cosine_similarity

    return cosine_similarity(a, b)


def _norm_text(text: str) -> str:
    return " ".join((text or "").lower().split())


class SemanticCache:
    """Embedding-similarity keyed LLM response cache (thread-safe)."""

    def __init__(
        self,
        *,
        threshold: float = 0.92,
        ttl_seconds: int = 3600,
        max_entries: int = 2000,
        embedding_provider: Any | None = None,
        persist_path: Path | None = None,
        sensitive_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.threshold = threshold
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.embedding_provider = embedding_provider
        self._lock = threading.Lock()
        # TTL expiry + LRU eviction are handled by cachetools (already a core
        # dependency). Entry value: {embedding, response, ts, hits}. Replaces
        # the former hand-rolled dict + manual TTL sweep + manual LRU eviction.
        self._cache: _CTTLCache = _CTTLCache(maxsize=max_entries, ttl=ttl_seconds)
        self._sensitive_prefixes = sensitive_prefixes
        self._persist = persist_path
        if self._persist:
            self._persist.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._load()

    # ── persistence ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self._persist, row_factory=sqlite3.Row)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    key TEXT PRIMARY KEY,
                    embedding TEXT,
                    response TEXT,
                    created_at REAL,
                    hits INTEGER DEFAULT 0
                )
                """
            )
            conn.commit()

    def _load(self) -> None:
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM semantic_cache").fetchall()
            with self._lock:
                for r in rows:
                    emb = json.loads(r["embedding"]) if r["embedding"] else None
                    self._cache[r["key"]] = {
                        "embedding": emb,
                        "response": r["response"],
                        "ts": r["created_at"],
                        "hits": r["hits"],
                    }
        except Exception:  # noqa: BLE001
            logger.warning("semantic cache load failed", exc_info=True)

    def _flush(self) -> None:
        if not self._persist:
            return
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM semantic_cache")
                with self._lock:
                    items = list(self._cache.items())
                for key, e in items:
                    emb = None
                    try:
                        if e["embedding"]:
                            emb = json.dumps(e["embedding"])
                    except (TypeError, ValueError):  # noqa: BLE001 — non-serializable embedding
                        emb = None
                    conn.execute(
                        "INSERT OR REPLACE INTO semantic_cache "
                        "(key, embedding, response, created_at, hits) VALUES (?,?,?,?,?)",
                        (key, emb, e["response"], e["ts"], e["hits"]),
                    )
                conn.commit()
        except Exception:  # noqa: BLE001
            logger.warning("semantic cache flush failed", exc_info=True)

    # ── core API ────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float] | None:
        if self.embedding_provider is None:
            return None
        try:
            return self.embedding_provider.embed([text])[0]
        except Exception:  # noqa: BLE001
            return None

    def get(self, query: str) -> str | None:
        """Return a cached response if a semantically-similar query was cached."""
        if self._sensitive(query):
            return None
        q_emb = self._embed(query)
        # TTL expiry is enforced by cachetools, so no manual sweep is needed.
        best: tuple[float, str, str] = (self.threshold, "", "")
        with self._lock:
            for key, e in list(self._cache.items()):
                sim = (
                    _cosine(q_emb, e["embedding"])
                    if q_emb
                    else (1.0 if _norm_text(query) == key else 0.0)
                )
                if sim >= best[0]:
                    best = (sim, key, e["response"])
        if not best[2]:
            return None
        with self._lock:
            entry = self._cache.get(best[1])
            if entry is not None:
                entry["hits"] += 1
        logger.debug("semantic cache hit (sim=%.3f)", best[0])
        return best[2]

    def put(self, query: str, response: str) -> None:
        """Store a query→response pair for future semantic hits."""
        if self._sensitive(query) or not response:
            return
        q_emb = self._embed(query)
        key = _norm_text(query) or hashlib.sha256(query.encode()).hexdigest()[:16]
        with self._lock:
            self._cache[key] = {
                "embedding": q_emb,
                "response": response,
                "ts": time.time(),
                "hits": 0,
            }
        # LRU eviction over capacity is handled by cachetools.
        self._flush()

    def _sensitive(self, query: str) -> bool:
        for p in self._sensitive_prefixes:
            if p and p in query:
                return True
        return False

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._cache),
                "threshold": self.threshold,
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
                "persist": bool(self._persist),
            }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
        self._flush()

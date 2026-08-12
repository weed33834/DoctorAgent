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

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


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
        self._entries: dict[str, dict[str, Any]] = {}  # key -> {embed, resp, ts, hits}
        self._sensitive_prefixes = sensitive_prefixes
        self._persist = persist_path
        if self._persist:
            self._persist.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._load()

    # ── persistence ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._persist))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

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
            for r in rows:
                emb = json.loads(r["embedding"]) if r["embedding"] else None
                self._entries[r["key"]] = {
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
                for key, e in self._entries.items():
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
        now = time.time()
        best: tuple[float, str] = (self.threshold, "")
        with self._lock:
            for key, e in list(self._entries.items()):
                if now - e["ts"] > self.ttl_seconds:
                    del self._entries[key]
                    continue
                sim = (
                    _cosine(q_emb, e["embedding"])
                    if q_emb
                    else (1.0 if _norm_text(query) == key else 0.0)
                )
                if sim >= best[0]:
                    best = (sim, e["response"])
        if not best[1]:
            return None
        with self._lock:
            hit_key = self._find_hit_key(best[1])
            if hit_key and hit_key in self._entries:
                self._entries[hit_key]["hits"] += 1
        logger.debug("semantic cache hit (sim=%.3f)", best[0])
        return best[1]

    def _find_hit_key(self, response: str) -> str | None:
        for key, e in self._entries.items():
            if e["response"] == response:
                return key
        return None

    def put(self, query: str, response: str) -> None:
        """Store a query→response pair for future semantic hits."""
        if self._sensitive(query) or not response:
            return
        q_emb = self._embed(query)
        key = _norm_text(query) or hashlib.sha256(query.encode()).hexdigest()[:16]
        with self._lock:
            self._entries[key] = {
                "embedding": q_emb,
                "response": response,
                "ts": time.time(),
                "hits": 0,
            }
            # LRU-style eviction when over capacity.
            if len(self._entries) > self.max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k]["ts"])
                del self._entries[oldest]
        self._flush()

    def _sensitive(self, query: str) -> bool:
        for p in self._sensitive_prefixes:
            if p and p in query:
                return True
        return False

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "threshold": self.threshold,
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
                "persist": bool(self._persist),
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
        self._flush()

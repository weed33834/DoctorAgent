"""Caching layer for RAG.

Three cooperating caches that reduce redundant computation across RAG
requests:

* :class:`TTLCache` — generic, thread-safe TTL + LRU cache used as the
  backing store for the specialised caches below.
* :class:`EmbeddingCache` — caches embedding vectors keyed by
  ``sha256(text)`` so repeated embedding of the same text is skipped.
* :class:`QueryResultCache` — caches full RAG query results with
  document-level invalidation so that editing a vault document drops only
  the affected cached answers.

All classes use ``hashlib.sha256`` for cache keys and ``time.monotonic()``
for TTL expiry (immune to wall-clock adjustments).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Generic TTL + LRU cache
# ---------------------------------------------------------------------------


class TTLCache:
    """Thread-safe cache with per-entry TTL and LRU eviction.

    Entries expire *lazily*: an expired entry is dropped on the next
    :meth:`get`. When ``max_size`` is exceeded the least-recently-used
    entry is evicted on :meth:`set`. Access (get/set of an existing key)
    updates the recency order.

    Args:
        max_size: Maximum number of live entries.
        ttl_seconds: Time-to-live for each entry, measured with
            :func:`time.monotonic`.
    """

    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: float = 3600.0,
    ) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        # key -> (value, expiry_monotonic)
        self._store: OrderedDict[Any, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        # Observability counters.
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: Any) -> Any:
        """Return the cached value for *key*, or ``None`` if absent/expired.

        Refreshes LRU recency on a hit and evicts the entry lazily when
        its TTL has elapsed.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expiry = entry
            if now >= expiry:
                # Expired — drop and count as an eviction + miss.
                self._store.pop(key, None)
                self._evictions += 1
                self._misses += 1
                return None
            # Hit: refresh recency.
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: Any, value: Any) -> None:
        """Insert or update *key* with *value* and a fresh TTL.

        Evicts the least-recently-used entry when ``max_size`` is exceeded.
        """
        expiry = time.monotonic() + self.ttl_seconds
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expiry)
            # LRU eviction of the oldest entry.
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)
                self._evictions += 1

    def delete(self, key: Any) -> bool:
        """Remove *key* from the cache. Returns ``True`` if it was present."""
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """Remove every entry and reset the hit/miss/eviction counters."""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Keys: ``hit_rate`` (``0.0``-``1.0``), ``size``, ``hits``, ``misses``,
        ``evictions``, ``max_size``, ``ttl_seconds``.
        """
        with self._lock:
            hits = self._hits
            misses = self._misses
            evictions = self._evictions
            size = len(self._store)
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else 0.0
        return {
            "hit_rate": round(hit_rate, 6),
            "size": size,
            "hits": hits,
            "misses": misses,
            "evictions": evictions,
            "max_size": self.max_size,
            "ttl_seconds": self.ttl_seconds,
        }


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------


class EmbeddingCache:
    """Cache for embedding vectors, keyed by ``sha256(text)``.

    Wraps a :class:`TTLCache` so embedding computations (which are
    expensive) are not repeated for identical text.
    """

    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._cache = TTLCache(max_size=max_size, ttl_seconds=ttl_seconds)

    @staticmethod
    def _key(text: str) -> str:
        """Compute the deterministic cache key for *text*."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get_embedding(self, text: str) -> list[float] | None:
        """Return the cached embedding for *text*, or ``None`` on a miss."""
        return self._cache.get(self._key(text))

    def set_embedding(self, text: str, embedding: list[float]) -> None:
        """Store *embedding* under the key derived from *text*."""
        self._cache.set(self._key(text), embedding)

    def delete(self, text: str) -> bool:
        """Drop the embedding for *text*. Returns ``True`` if it was cached."""
        return self._cache.delete(self._key(text))

    def clear(self) -> None:
        """Remove every cached embedding."""
        self._cache.clear()

    def stats(self) -> dict[str, Any]:
        """Return the underlying :class:`TTLCache` statistics."""
        return self._cache.stats()


# ---------------------------------------------------------------------------
# Query result cache
# ---------------------------------------------------------------------------


def _extract_doc_ids(result: Any) -> list[str]:
    """Extract document identifiers from a cached RAG result.

    Supports both an explicit ``doc_ids`` list and a ``sources`` list of
    dicts carrying ``doc_id`` / ``task_id`` / ``vault_path`` / ``chunk_id``
    fields. Returns a de-duplicated list of string IDs.
    """
    if not isinstance(result, dict):
        return []
    doc_ids: list[str] = []
    for value in result.get("doc_ids", []) or []:
        if value:
            doc_ids.append(str(value))
    for source in result.get("sources", []) or []:
        if not isinstance(source, dict):
            continue
        for key in ("doc_id", "task_id", "vault_path", "chunk_id"):
            value = source.get(key)
            if value:
                doc_ids.append(str(value))
                break
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for did in doc_ids:
        if did not in seen:
            seen.add(did)
            unique.append(did)
    return unique


class QueryResultCache:
    """Cache for full RAG query results with document-level invalidation.

    Results are keyed by a caller-supplied ``query_hash`` (typically
    ``sha256`` of the normalised query). Whenever a document is added,
    updated or removed, :meth:`invalidate_for_doc` drops only the cached
    results that referenced that document.

    The reverse index (``doc_id -> {query_hash, ...}``) is maintained
    lazily on :meth:`set_result` by inspecting the result's ``doc_ids`` /
    ``sources`` fields.
    """

    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._cache = TTLCache(max_size=max_size, ttl_seconds=ttl_seconds)
        # doc_id -> set of query_hashes that referenced it.
        self._doc_index: dict[str, set[str]] = {}
        self._index_lock = threading.Lock()

    def get_result(self, query_hash: str) -> dict[str, Any] | None:
        """Return the cached result for *query_hash*, or ``None`` on a miss."""
        return self._cache.get(query_hash)

    def set_result(self, query_hash: str, result: dict[str, Any]) -> None:
        """Cache *result* under *query_hash* and index its document IDs.

        If a result already exists for *query_hash*, its previous document
        associations are cleaned up before re-indexing so the reverse index
        never holds stale mappings.
        """
        # Refresh the reverse index for this query hash.
        doc_ids = _extract_doc_ids(result)
        with self._index_lock:
            # Remove any previous associations for this query hash.
            for doc_id, hashes in list(self._doc_index.items()):
                hashes.discard(query_hash)
                if not hashes:
                    self._doc_index.pop(doc_id, None)
            # Add the new associations.
            for doc_id in doc_ids:
                self._doc_index.setdefault(doc_id, set()).add(query_hash)
        self._cache.set(query_hash, result)

    def invalidate_for_doc(self, doc_id: str) -> int:
        """Invalidate every cached result that referenced *doc_id*.

        Returns the number of invalidated entries.
        """
        if not doc_id:
            return 0
        doc_id = str(doc_id)
        with self._index_lock:
            query_hashes = self._doc_index.pop(doc_id, set())
        invalidated = 0
        for query_hash in query_hashes:
            if self._cache.delete(query_hash):
                invalidated += 1
        if invalidated:
            logger.debug(
                "QueryResultCache invalidated %d result(s) for doc %s",
                invalidated,
                doc_id,
            )
        return invalidated

    def invalidate_all(self) -> None:
        """Drop the entire cache and the reverse document index."""
        self._cache.clear()
        with self._index_lock:
            self._doc_index.clear()

    def stats(self) -> dict[str, Any]:
        """Return the underlying :class:`TTLCache` statistics plus the
        number of indexed documents."""
        stats = self._cache.stats()
        with self._index_lock:
            stats["indexed_docs"] = len(self._doc_index)
        return stats

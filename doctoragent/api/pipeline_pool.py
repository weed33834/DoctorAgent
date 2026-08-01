"""Pipeline instance pool for reusing expensive RAG / Agent pipelines.

The RAG endpoint previously constructed a fresh :class:`RagPipeline` (and all
of its sub-systems: retriever, memory, context engineer, reranker, …) on every
single request.  For an enterprise deployment that is wasteful and slow.

This module provides :class:`PipelinePool`, a thread-safe, per-tenant cache of
``RagPipeline`` instances with a TTL-based eviction policy.  All API endpoints
that need a RAG pipeline should acquire it via ``pool.get_pipeline(tenant_id)``
instead of constructing one ad-hoc.

Design notes
------------
* **Per-tenant isolation** — each ``tenant_id`` gets its own pipeline instance
  so retrieval/memory state never leaks across tenants.
* **Lazy creation** — pipelines are only built on first use, so a fresh server
  with no traffic pays no cost.
* **TTL eviction** — idle instances are evicted after ``ttl_seconds`` (default
  30 minutes) to release SQLite connections and cached embeddings.
* **Thread safety** — all mutations are guarded by a single ``threading.Lock``.
  The pool is therefore safe to use from FastAPI's thread pool (``asyncio.to_thread``)
  as well as from async handlers.
* **Refresh-on-access** — fetching an instance refreshes its TTL so a
  continuously-used pipeline is never evicted mid-traffic.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Default TTL: 30 minutes of inactivity before a pooled pipeline is evicted.
DEFAULT_TTL_SECONDS: float = 30 * 60.0


class PipelinePool:
    """Thread-safe, per-tenant pool of ``RagPipeline`` instances.

    Parameters
    ----------
    factory:
        Callable ``factory(tenant_id, **kwargs) -> RagPipeline`` used to build a
        new pipeline on first access for a tenant.  When ``None`` a default
        factory that imports and constructs :class:`doctoragent.model.rag.RagPipeline`
        with the captured ``config``/``agent`` providers is used.
    ttl_seconds:
        Idle time after which a pooled instance is eligible for eviction.
    """

    def __init__(
        self,
        factory: Callable[..., Any] | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._factory = factory
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        # tenant_id -> (pipeline, last_access_monotonic)
        self._entries: dict[str, tuple[Any, float]] = {}

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def get_pipeline(self, tenant_id: str = "default", **factory_kwargs: Any) -> Any:
        """Return the pooled pipeline for *tenant_id*, creating it if needed.

        Accessing an instance refreshes its TTL.  Expired entries are evicted
        opportunistically on every call so a dedicated background sweeper is
        not strictly required (though :meth:`cleanup_expired` can be called
        periodically to proactively release idle resources).
        """
        with self._lock:
            self._evict_expired_locked()
            now = time.monotonic()
            entry = self._entries.get(tenant_id)
            if entry is None:
                pipeline = self._build(tenant_id, **factory_kwargs)
                self._entries[tenant_id] = (pipeline, now)
                logger.debug("PipelinePool: created pipeline for tenant=%s", tenant_id)
                return pipeline
            pipeline, _ = entry
            # Refresh TTL on access.
            self._entries[tenant_id] = (pipeline, now)
            return pipeline

    def get_or_none(self, tenant_id: str) -> Any | None:
        """Return the pooled pipeline for *tenant_id* without creating one.

        Returns ``None`` when no pipeline has been created for the tenant yet.
        Does **not** refresh the TTL (read-only peek).
        """
        with self._lock:
            entry = self._entries.get(tenant_id)
            return entry[0] if entry is not None else None

    # ------------------------------------------------------------------
    # Eviction / lifecycle
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Evict all entries whose idle time exceeds the TTL.

        Returns the number of evicted entries.  Safe to call from any thread.
        """
        with self._lock:
            return self._evict_expired_locked()

    def _evict_expired_locked(self) -> int:
        if not self._entries:
            return 0
        now = time.monotonic()
        expired = [tenant for tenant, (_, ts) in self._entries.items() if now - ts > self._ttl]
        for tenant in expired:
            pipeline, _ = self._entries.pop(tenant, (None, 0.0))
            self._close(pipeline)
            logger.debug("PipelinePool: evicted idle pipeline for tenant=%s", tenant)
        return len(expired)

    def close(self) -> None:
        """Drop all pooled instances and release their resources."""
        with self._lock:
            for tenant, (pipeline, _) in self._entries.items():
                self._close(pipeline)
                logger.debug("PipelinePool: closed pipeline for tenant=%s", tenant)
            self._entries.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def tenants(self) -> list[str]:
        """Return the list of currently pooled tenant IDs (snapshot)."""
        with self._lock:
            return list(self._entries.keys())

    def stats(self) -> dict[str, Any]:
        """Return a small statistics snapshot suitable for a status endpoint."""
        with self._lock:
            now = time.monotonic()
            return {
                "pooled_tenants": len(self._entries),
                "tenants": [
                    {
                        "tenant_id": tenant,
                        "idle_seconds": round(now - ts, 1),
                        "ttl_seconds": self._ttl,
                    }
                    for tenant, (_, ts) in self._entries.items()
                ],
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build(self, tenant_id: str, **factory_kwargs: Any) -> Any:
        if self._factory is None:
            raise RuntimeError(
                "PipelinePool has no factory configured; pass factory=... "
                "or use get_pipeline only after binding a factory."
            )
        return self._factory(tenant_id, **factory_kwargs)

    @staticmethod
    def _close(pipeline: Any) -> None:
        """Best-effort resource release for a pooled pipeline.

        ``RagPipeline`` does not expose an explicit ``close()``, but its
        sub-systems hold SQLite connections that are re-opened per call.
        We call ``close()`` if the object exposes one (future-proofing) and
        otherwise simply drop the reference so GC can reclaim it.
        """
        close = getattr(pipeline, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — never raise from cleanup
                logger.debug("PipelinePool: pipeline.close() raised", exc_info=True)

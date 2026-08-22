"""Factory for selecting a vector store backend by name.

Keeps the rest of the codebase decoupled from the concrete backend classes:
callers ask for ``"sqlite"`` or ``"chroma"`` and get back a
:class:`VectorStoreBackend`. Unknown names raise :class:`ValueError`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from doctoragent.model.vectorstore.base import VectorStoreBackend

logger = logging.getLogger(__name__)

# Shared-instance cache so every consumer (TaskStore ingestion, HybridRetriever
# queries) talks to ONE backend object per (backend, path) pair. Chroma's
# PersistentClient is process-safe but opening several instances on the same
# path wastes handles; sharing also makes dual-write/query see a consistent
# view.
_SHARED_LOCK = threading.Lock()
_SHARED: dict[tuple[str, str], VectorStoreBackend] = {}
_WARNED_ONCE: set[tuple[str, str]] = set()


def get_shared_vector_store(backend: str, path: str) -> VectorStoreBackend | None:
    """Return a process-wide cached backend instance, or ``None`` on failure.

    Failure modes (unknown name, native dependency missing such as chromadb,
    unwritable path) are logged once per (backend, path) and yield ``None``
    so callers can degrade to the inline SQLite dense path instead of
    crashing startup.
    """
    key = (backend.lower(), str(path))
    with _SHARED_LOCK:
        if key in _SHARED:
            return _SHARED[key]
        try:
            instance = create_vector_store(backend, path=path)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash startup
            if key not in _WARNED_ONCE:
                _WARNED_ONCE.add(key)
                logger.warning(
                    "Vector backend %r at %r unavailable (%s); "
                    "falling back to inline SQLite dense search",
                    backend,
                    path,
                    exc,
                )
            return None
        _SHARED[key] = instance
        return instance


def create_vector_store(backend: str = "sqlite", **kwargs: Any) -> VectorStoreBackend:
    """Construct a vector store backend by name.

    Parameters
    ----------
    backend:
        ``"sqlite"`` (default) or ``"chroma"``.
    **kwargs:
        Backend-specific options:

        * ``sqlite`` → ``path`` (SQLite file path).
        * ``chroma`` → ``path`` (persistence directory); requires
          ``chromadb`` to be installed.

    Returns
    -------
    VectorStoreBackend
        A ready-to-use store instance.

    Raises
    ------
    ValueError
        If *backend* is not one of the supported names.
    ImportError
        If a backend's native dependency is not installed (e.g. chromadb).
    """
    name = (backend or "").lower()
    if name == "sqlite":
        from doctoragent.model.vectorstore.sqlite_store import SQLiteVectorStore

        path = kwargs.get("path")
        if not path:
            raise ValueError("sqlite backend requires a 'path' argument")
        return SQLiteVectorStore(path=path)

    if name == "chroma":
        from doctoragent.model.vectorstore.chroma_store import ChromaVectorStore

        path = kwargs.get("path")
        if not path:
            raise ValueError("chroma backend requires a 'path' argument")
        return ChromaVectorStore(path=path)

    if name == "pgvector":
        from doctoragent.model.vectorstore.pgvector_store import PgVectorStore

        # *path* carries the Postgres DSN for symmetry with other backends.
        path = kwargs.get("path")
        if not path:
            raise ValueError("pgvector backend requires a 'path' argument (DSN)")
        return PgVectorStore(dsn=path)

    raise ValueError(
        f"Unknown vector store backend: {backend!r}. "
        "Supported backends: 'sqlite', 'chroma', 'pgvector'."
    )

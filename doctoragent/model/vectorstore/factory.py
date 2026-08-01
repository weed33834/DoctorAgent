"""Factory for selecting a vector store backend by name.

Keeps the rest of the codebase decoupled from the concrete backend classes:
callers ask for ``"sqlite"`` or ``"chroma"`` and get back a
:class:`VectorStoreBackend`. Unknown names raise :class:`ValueError`.
"""

from __future__ import annotations

from typing import Any

from doctoragent.model.vectorstore.base import VectorStoreBackend


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

    raise ValueError(
        f"Unknown vector store backend: {backend!r}. Supported backends: 'sqlite', 'chroma'."
    )

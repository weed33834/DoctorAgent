"""Pluggable vector store backends.

The default RAG pipeline keeps using its inline SQLite implementation; this
package provides the abstraction layer that lets operators swap in
horizontally-scalable backends (Chroma, and future ones) without touching
the retriever core.

Public API::

    from doctoragent.model.vectorstore import create_vector_store, VectorStoreBackend

    store = create_vector_store("sqlite", path="/var/lib/doctoragent/vectors.db")
    store.add([VectorRecord(id="1", vector=[...], document="hi")])
    hits = store.search(query_vector=[...], top_k=5)
"""

from __future__ import annotations

from doctoragent.model.vectorstore.base import (
    VectorRecord,
    VectorSearchResult,
    VectorStoreBackend,
)
from doctoragent.model.vectorstore.factory import create_vector_store

__all__ = [
    "VectorRecord",
    "VectorSearchResult",
    "VectorStoreBackend",
    "create_vector_store",
]

"""Abstract vector store backend interface.

Defines the common contract implemented by every concrete vector store
backend (SQLite, Chroma, ...). The default RAG pipeline keeps using its
inline SQLite implementation; this module exists so that alternative
backends can be plugged in without touching the retriever core.

The abstractions are intentionally minimal: ``add`` / ``search`` /
``delete`` / ``count`` / ``close``. Backends are responsible for their own
serialisation, persistence format and similarity metric, but should report
cosine-style scores (higher = more similar) so callers can compare results
across backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class VectorRecord(BaseModel):
    """A single vector plus its payload.

    Attributes
    ----------
    id:
        Stable unique identifier for the record. Upserts by id.
    vector:
        Dense embedding (floats). Dimensionality is backend-defined but must
        be consistent within a single store.
    metadata:
        Free-form filterable metadata (category, source path, tags, ...).
    document:
        Optional human-readable text associated with the vector. Backends
        that support full-text payloads store this verbatim; others may
        leave it empty.
    """

    id: str
    vector: list[float] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    document: str = ""


class VectorSearchResult(BaseModel):
    """A search hit: the matched record plus its similarity score.

    Scores follow the cosine convention (``-1.0``..``1.0`` for normalised
    vectors, ``0.0``..``1.0`` in practice). Higher is more similar. Backends
    using a different metric internally convert before returning.
    """

    record: VectorRecord
    score: float


class VectorStoreBackend(ABC):
    """Abstract vector store backend.

    Implementations must be safe to construct without their optional native
    dependency installed (lazy import inside ``__init__``); the factory
    surfaces a clear :class:`ImportError` in that case.
    """

    @abstractmethod
    def add(self, records: list[VectorRecord | dict[str, Any]]) -> None:
        """Upsert one or more records (dict input is coerced to VectorRecord)."""

    @abstractmethod
    def search(self, query_vector: list[float], top_k: int = 10) -> list[VectorSearchResult]:
        """Return the ``top_k`` most similar records (cosine, descending)."""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """Delete records by id. Unknown ids are silently ignored."""

    @abstractmethod
    def count(self) -> int:
        """Number of records currently stored."""

    @abstractmethod
    def close(self) -> None:
        """Release any underlying resources (connection, client, ...)."""

    def __enter__(self) -> VectorStoreBackend:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

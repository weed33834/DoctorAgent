"""Chroma-backed vector store.

Chroma (https://www.trychroma.com/) is an open-source embedding database
that scales beyond what the in-process SQLite backend can handle. The
dependency is imported lazily so the rest of the package keeps working
without ``chromadb`` installed; constructing this backend without it raises
a clear :class:`ImportError`.
"""

from __future__ import annotations

import logging
from typing import Any

from doctoragent.model.vectorstore.base import (
    VectorRecord,
    VectorSearchResult,
    VectorStoreBackend,
)

logger = logging.getLogger(__name__)

# Single shared collection name. Multi-tenant isolation is the caller's
# responsibility (use one path per tenant) for now.
_COLLECTION_NAME = "doctoragent_vectors"
# Chroma reports cosine *distance* (= 1 - cosine_similarity); convert back
# to the similarity convention used by every other backend.


class ChromaVectorStore(VectorStoreBackend):
    """Persistent Chroma vector store.

    Parameters
    ----------
    path:
        Directory Chroma persists to. Created on demand by the client.

    Raises
    ------
    ImportError
        If ``chromadb`` is not installed.
    """

    def __init__(self, path: str) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(
                "chromadb is not installed. Install it with: pip install chromadb"
            ) from exc

        self.path = path
        self._client = chromadb.PersistentClient(path=path)
        # ``cosine`` space makes Chroma normalise vectors and report cosine
        # distance, matching the SQLite backend's similarity semantics.
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce(record: VectorRecord | dict[str, Any]) -> VectorRecord:
        if isinstance(record, VectorRecord):
            return record
        return VectorRecord(**record)

    # ------------------------------------------------------------------
    # VectorStoreBackend
    # ------------------------------------------------------------------
    def add(self, records: list[VectorRecord | dict[str, Any]]) -> None:
        coerced = [self._coerce(r) for r in records]
        if not coerced:
            return
        ids = [r.id for r in coerced]
        embeddings = [r.vector for r in coerced]
        # Chroma metadata values must be primitives; nested dicts are
        # stringified to keep round-tripping lossless for our purposes.
        metadatas = [
            {
                k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                for k, v in r.metadata.items()
            }
            for r in coerced
        ]
        documents = [r.document for r in coerced]
        self._collection.add(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    def search(self, query_vector: list[float], top_k: int = 10) -> list[VectorSearchResult]:
        if top_k <= 0:
            return []
        stored = self._collection.count()
        if stored == 0:
            return []
        n_results = min(top_k, stored)
        result = self._collection.query(
            query_embeddings=[query_vector],
            n_results=n_results,
            include=["metadatas", "documents", "distances"],
        )
        ids_batch = result.get("ids", [[]])
        metas_batch = result.get("metadatas", [[]])
        docs_batch = result.get("documents", [[]])
        dists_batch = result.get("distances", [[]])
        if not ids_batch:
            return []
        ids = ids_batch[0]
        metas = metas_batch[0] if metas_batch else [{} for _ in ids]
        docs = docs_batch[0] if docs_batch else ["" for _ in ids]
        dists = dists_batch[0] if dists_batch else [0.0 for _ in ids]

        out: list[VectorSearchResult] = []
        for rid, meta, doc, dist in zip(ids, metas, docs, dists, strict=False):
            # Chroma cosine distance == 1 - similarity; clamp for safety.
            score = 1.0 - float(dist)
            out.append(
                VectorSearchResult(
                    record=VectorRecord(
                        id=str(rid),
                        vector=[],  # Chroma does not return embeddings by default
                        metadata=dict(meta) if meta else {},
                        document=doc or "",
                    ),
                    score=score,
                )
            )
        return out

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        self._collection.delete(ids=ids)

    def count(self) -> int:
        return int(self._collection.count())

    def close(self) -> None:
        # Chroma's PersistentClient has no explicit close hook; release the
        # references so the underlying SQLite/HNSW files can be reclaimed.
        try:
            self._collection = None  # type: ignore[assignment]
            self._client = None  # type: ignore[assignment]
        except Exception:  # pragma: no cover - defensive
            logger.debug("Error while closing ChromaVectorStore", exc_info=True)

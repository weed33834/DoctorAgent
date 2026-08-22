"""PostgreSQL + pgvector-backed vector store.

Implements the sync :class:`VectorStoreBackend` interface on top of the
``vector`` extension (https://github.com/pgvector/pgvector) using psycopg 3
in autocommit mode — a natural fit for deployments that already run
Postgres (see ``docs/POSTGRES_MIGRATION.md``).

Design notes:

* Single table ``doctoragent_vectors`` shared by all tenants; **tenant
  filtering is the caller's job** (mirrors ChromaVectorStore). The
  repository/retriever layers filter by row-lookup after ANN hits, and P4
  adds an RLS policy on this table for database-level enforcement.
* Embedding column has no typmod (mixed dimensions allowed per row);
  queries cast the literal to ``vector`` so any model dimension works
  without migrations. ANN indexes (hnsw/ivfflat) require a fixed dim and
  are intentionally deferred until the dimension is deployment-pinned.
* Similarity convention: pgvector's ``<=>`` returns cosine *distance*;
  scores are reported as ``1 - distance`` like every other backend.

The driver (psycopg 3, binary extra) is imported lazily.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from doctoragent.model.vectorstore.base import (
    VectorRecord,
    VectorSearchResult,
    VectorStoreBackend,
)

logger = logging.getLogger(__name__)

_TABLE = "doctoragent_vectors"


class PgVectorStore(VectorStoreBackend):
    """Persistent Postgres+pgvector store (sync psycopg 3 driver)."""

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(
                "psycopg is not installed. Install it with: "
                "pip install 'doctoragent[database]' 'psycopg[binary]'"
            ) from exc

        self.dsn = dsn
        self._conn: Any = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id        TEXT PRIMARY KEY,
                    embedding vector,
                    metadata_ JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    document  TEXT NOT NULL DEFAULT '',
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _coerce(record: VectorRecord | dict[str, Any]) -> VectorRecord:
        if isinstance(record, VectorRecord):
            return record
        return VectorRecord(**record)

    @staticmethod
    def _vector_literal(vector: list[float]) -> str:
        return "[" + ",".join(repr(float(v)) for v in vector) + "]"

    # ── VectorStoreBackend ───────────────────────────────────────────

    def add(self, records: list[VectorRecord | dict[str, Any]]) -> None:
        coerced = [self._coerce(r) for r in records]
        if not coerced:
            return
        with self._conn.cursor() as cur:
            for rec in coerced:
                meta = dict(rec.metadata)
                tenant = str(meta.pop("tenant_id", "default"))
                cur.execute(
                    f"""
                    INSERT INTO {_TABLE} (id, embedding, metadata_, document, tenant_id)
                    VALUES (%s, %s::vector, %s::jsonb, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        metadata_ = EXCLUDED.metadata_,
                        document  = EXCLUDED.document,
                        tenant_id = EXCLUDED.tenant_id
                    """,
                    (
                        rec.id,
                        self._vector_literal(rec.vector),
                        json.dumps(meta, ensure_ascii=False, default=str),
                        rec.document,
                        tenant,
                    ),
                )

    def search(
        self, query_vector: list[float], top_k: int = 10, tenant_id: str | None = None
    ) -> list[VectorSearchResult]:
        if top_k <= 0:
            return []
        lit = self._vector_literal(query_vector)
        sql = (
            f"SELECT id, metadata_, document, 1 - (embedding <=> %s::vector) AS score "
            f"FROM {_TABLE}"
        )
        params: list[Any] = [lit]
        if tenant_id:
            sql += " WHERE tenant_id = %s"
            params.append(tenant_id)
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params += [lit, top_k]
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: list[VectorSearchResult] = []
        for rid, meta, doc, score in rows:
            out.append(
                VectorSearchResult(
                    record=VectorRecord(
                        id=str(rid),
                        vector=[],
                        metadata=dict(meta) if meta else {},
                        document=doc or "",
                    ),
                    score=float(score),
                )
            )
        return out

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE id = ANY(%s)", (list(ids),))

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {_TABLE}")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug("Error while closing PgVectorStore", exc_info=True)

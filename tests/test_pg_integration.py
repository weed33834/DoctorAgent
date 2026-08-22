"""REAL PostgreSQL integration tests (P3 driver-level verification).

Gated by ``DOCTORAGENT_TEST_PG_URL`` (e.g.
``postgresql+asyncpg://doctoragent:doctoragent@localhost:55432/doctoragent_test``).
Skipped entirely when unset, so default CI/dev runs stay green without a DB.

Local reproducibility::

    docker run -d --name doctoragent-pg \
      -e POSTGRES_PASSWORD=doctoragent -e POSTGRES_USER=doctoragent \
      -e POSTGRES_DB=doctoragent_test -p 55432:5432 pgvector/pgvector:pg16

Covered here — things mocks can never prove:

* asyncpg driver round-trip through our engine factory;
* ``tenant_scope`` really binds ``app.tenant_id`` inside a transaction
  (verified via ``current_setting``) and auto-reverts afterwards;
* **RLS end-to-end**: with policies installed, tenant A physically cannot
  see/update tenant B's rows, and a session without any GUC sees nothing;
* :class:`AsyncConversationRepository` full parity against real Postgres;
* pgvector extension availability smoke (vector column + ``<=>`` operator).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("asyncpg")

PG_URL = os.environ.get(
    "DOCTORAGENT_TEST_PG_URL",
    "postgresql+asyncpg://doctoragent:doctoragent@localhost:55432/doctoragent_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DOCTORAGENT_TEST_PG_URL"),
    reason="DOCTORAGENT_TEST_PG_URL not set (real Postgres integration)",
)

from sqlalchemy import text  # noqa: E402

from doctoragent.db.bootstrap import (  # noqa: E402
    _enable_rls_statements,
    rls_policy_statements,
)
from doctoragent.db.engine import (  # noqa: E402
    create_async_engine_from_url,
    dialect_of,
)
from doctoragent.db.repositories import AsyncConversationRepository  # noqa: E402
from doctoragent.model.rag import HybridRetriever, RagConfig  # noqa: E402
from doctoragent.orchestration.task_store import TaskStore  # noqa: E402


@pytest.fixture
def engine() -> Any:
    return create_async_engine_from_url(PG_URL)


@pytest.fixture
async def rls_probe(engine: Any):
    """A dedicated probe table with RLS installed + a NON-superuser prober.

    Critical: the container's default role (``doctoragent``) is SUPERUSER,
    which silently bypasses RLS — probing through it proves nothing. All
    read/write probes therefore run as ``rls_probe_user`` (LOGIN, no
    BYPASSRLS), granted only on the probe table.
    """
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS _rls_probe"))
        await conn.execute(
            text(
                "CREATE TABLE _rls_probe (id SERIAL PRIMARY KEY, "
                "tenant_id TEXT NOT NULL, secret TEXT NOT NULL)"
            )
        )
        for stmt in _enable_rls_statements("_rls_probe") + rls_policy_statements(
            "_rls_probe"
        ):
            await conn.execute(text(stmt))
        # Cluster-wide probe role: idempotent-ish (ignore if pre-existing
        # with dependencies we cannot drop).
        await conn.execute(
            text("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rls_probe_user') THEN CREATE ROLE rls_probe_user LOGIN PASSWORD 'probe-pass' NOBYPASSRLS; END IF; END $$;")
        )
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO rls_probe_user"))
        await conn.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON _rls_probe TO rls_probe_user")
        )
        await conn.execute(
            text(
                "GRANT USAGE, SELECT ON SEQUENCE _rls_probe_id_seq TO rls_probe_user"
            )
        )
    yield "_rls_probe"
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS _rls_probe"))


def _probe_url() -> str:
    """Same host/db as PG_URL but authenticated as the non-superuser prober."""
    tail = PG_URL.split("@", 1)[1]
    return f"postgresql+asyncpg://rls_probe_user:probe-pass@{tail}"


@pytest.fixture
async def probe_engine(engine: Any):
    return create_async_engine_from_url(_probe_url())


@pytest.fixture
async def clean_repo_tables(engine: Any) -> AsyncIterator[AsyncConversationRepository]:
    """Fresh conversations schema for repository parity tests."""
    async with engine.begin() as conn:
        for stmt in (
            "DROP TABLE IF EXISTS conv_shares",
            "DROP TABLE IF EXISTS conv_messages",
            "DROP TABLE IF EXISTS conv_conversations",
        ):
            await conn.execute(text(stmt))
    yield AsyncConversationRepository(PG_URL)


# ---------------------------------------------------------------------------
# driver + tenant context
# ---------------------------------------------------------------------------


class TestDriverAndTenantScope:
    @pytest.mark.asyncio
    async def test_dialect_and_connectivity(self, engine: Any) -> None:
        assert dialect_of(PG_URL) == "postgres"
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version()"))).scalar_one()
        assert version.startswith("PostgreSQL")

    @pytest.mark.asyncio
    async def test_tenant_guc_binds_inside_transaction(self, engine: Any) -> None:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = 'hospital_a'"))
                val = (
                    await conn.execute(
                        text(f"SELECT current_setting('{TENANT_GUC_}', true)")
                    )
                ).scalar_one()
            assert val == "hospital_a"

    @pytest.mark.asyncio
    async def test_tenant_guc_reverts_after_commit(self, engine: Any) -> None:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = 'hospital_a'"))
            val = (
                await conn.execute(
                    text(f"SELECT current_setting('{TENANT_GUC_}', true)")
                )
            ).scalar_one()
        assert val in ("", None)  # SET LOCAL auto-reverted


TENANT_GUC_ = "app.tenant_id"


# ---------------------------------------------------------------------------
# Row-Level Security end-to-end
# ---------------------------------------------------------------------------


class TestRlsEndToEnd:
    @pytest.mark.asyncio
    async def test_cross_tenant_rows_invisible(
        self, probe_engine: Any, rls_probe: str
    ) -> None:
        """All probes run as the NON-superuser rls_probe_user role.

        The container's default ``doctoragent`` role is SUPERUSER and
        silently bypasses RLS — probing through it proves nothing (this
        exact gap is why the first version of this test passed while RLS
        was broken).
        """
        table = rls_probe

        async def write_as(tenant: str, secret: str) -> int:
            async with probe_engine.begin() as conn:
                await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = '{tenant}'"))
                rid = (
                    await conn.execute(
                        text(
                            f"INSERT INTO {table} (tenant_id, secret) "
                            f"VALUES ('{tenant}', '{secret}') RETURNING id"
                        )
                    )
                ).scalar_one()
            return int(rid)

        async def read_as(tenant: str | None) -> list[str]:
            async with probe_engine.connect() as conn:
                trans = await conn.begin()
                if tenant is not None:
                    await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = '{tenant}'"))
                rows = (
                    await conn.execute(text(f"SELECT secret FROM {table}"))
                ).fetchall()
                await trans.rollback()
                return [r[0] for r in rows]

        await write_as("hospital_a", "a-secret")
        await write_as("hospital_b", "b-secret")

        seen_a = await read_as("hospital_a")
        seen_b = await read_as("hospital_b")
        seen_none = await read_as(None)
        assert sorted(seen_a) == ["a-secret"], f"as A saw {seen_a}"
        assert sorted(seen_b) == ["b-secret"], f"as B saw {seen_b}"
        # No GUC bound → policy yields false → zero rows (fail-closed).
        assert seen_none == [], f"unauthenticated saw {seen_none}"

    @pytest.mark.asyncio
    async def test_with_check_blocks_mismatched_insert(
        self, probe_engine: Any, rls_probe: str
    ) -> None:
        """WITH CHECK rejects inserting a row tagged with another tenant."""
        import sqlalchemy

        async with probe_engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = 'hospital_a'"))
            with pytest.raises(sqlalchemy.exc.DBAPIError):
                await conn.execute(
                    text(
                        "INSERT INTO _rls_probe (tenant_id, secret) "
                        "VALUES ('hospital_b', 'smuggled')"
                    )
                )

    @pytest.mark.asyncio
    async def test_update_blocked_across_tenants(
        self, probe_engine: Any, rls_probe: str
    ) -> None:
        table = rls_probe

        async def write_as(tenant: str, secret: str) -> int:
            async with probe_engine.begin() as conn:
                await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = '{tenant}'"))
                rid = (
                    await conn.execute(
                        text(
                            f"INSERT INTO {table} (tenant_id, secret) "
                            f"VALUES ('{tenant}', '{secret}') RETURNING id"
                        )
                    )
                ).scalar_one()
            return int(rid)

        victim_id = await write_as("hospital_a", "a-secret")
        attacker_id = await write_as("hospital_b", "b-secret")

        # Tenant B tries to update tenant A's row by id.
        async with probe_engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = 'hospital_b'"))
            cur = await conn.execute(
                text(f"UPDATE {table} SET secret='hacked' WHERE id={victim_id}")
            )
            assert cur.rowcount == 0
        # And cannot even see it.
        async with probe_engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = 'hospital_b'"))
            n = (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE id={victim_id}")
                )
            ).scalar_one()
            assert n == 0
        # Sanity: own-row update works.
        async with probe_engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL {TENANT_GUC_} = 'hospital_a'"))
            cur = await conn.execute(
                text(f"UPDATE {table} SET secret='rotated' WHERE id={victim_id}")
            )
            assert cur.rowcount == 1
        del attacker_id


# ---------------------------------------------------------------------------
# Repository parity on real Postgres
# ---------------------------------------------------------------------------


class TestRepositoryOnPostgres:
    @pytest.mark.asyncio
    async def test_full_parity_flow(
        self, clean_repo_tables: AsyncConversationRepository
    ) -> None:
        repo = clean_repo_tables
        conv = await repo.create("warfarin case", tenant_id="hospital_a")
        await repo.add_message(conv["id"], "user", "INR target?", tenant_id="hospital_a")

        items = await repo.list(tenant_id="hospital_a")
        assert [c["title"] for c in items] == ["warfarin case"]
        assert items[0]["message_count"] == 1

        got = await repo.get_for_tenant(conv["id"], "hospital_a")
        assert got is not None
        assert got["messages"][0]["content"] == "INR target?"
        assert await repo.get_for_tenant(conv["id"], "hospital_b") is None

        branched = await repo.fork(conv["id"], tenant_id="hospital_a")
        assert branched is not None and len(branched["messages"]) == 1

        share = await repo.share(conv["id"], tenant_id="hospital_a")
        assert share is not None
        shared = await repo.get_shared(share["token"])
        assert shared is not None and shared["id"] == conv["id"]

        stats = await repo.stats()
        assert stats["conversations"] == 2  # original + fork
        assert stats["messages"] == 2

    @pytest.mark.asyncio
    async def test_foreign_tenant_isolation(
        self, clean_repo_tables: AsyncConversationRepository
    ) -> None:
        repo = clean_repo_tables
        conv = await repo.create("private", tenant_id="hospital_a")
        await repo.add_message(conv["id"], "user", "secret", tenant_id="hospital_a")

        assert await repo.get_for_tenant(conv["id"], "hospital_b") is None
        assert (
            await repo.add_message(conv["id"], "user", "leak", tenant_id="hospital_b")
            is None
        )
        assert not await repo.rename(conv["id"], "hacked", tenant_id="hospital_b")
        assert not await repo.delete(conv["id"], tenant_id="hospital_b")
        assert await repo.share(conv["id"], tenant_id="hospital_b") is None
        hits = await repo.list("private", tenant_id="hospital_b")
        assert hits == []


class TestPgvectorSmoke:
    @pytest.mark.asyncio
    async def test_vector_column_and_cosine_operator(self, engine: Any) -> None:
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception:  # noqa: BLE001 — extension unavailable → skip cleanly
            pytest.skip("pgvector extension not available")
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS _vec_probe"))
            await conn.execute(
                text("CREATE TABLE _vec_probe (id TEXT PRIMARY KEY, v vector(2))")
            )
            await conn.execute(
                text("INSERT INTO _vec_probe VALUES ('x', '[1,0]'), ('y', '[0,1]')")
            )
        async with engine.connect() as conn:
            top = (
                await conn.execute(
                    text(
                        "SELECT id FROM _vec_probe ORDER BY v <=> '[1,0]' LIMIT 1"
                    )
                )
            ).scalar_one()
        assert top == "x"


# ---------------------------------------------------------------------------
# PgVectorStore backend (P3b) + HybridRetriever end-to-end on real Postgres
# ---------------------------------------------------------------------------

PG_DSN = PG_URL.replace("+asyncpg", "")  # psycopg speaks plain postgresql://


@pytest.fixture
def pg_store() -> Any:
    pytest.importorskip("psycopg")
    from doctoragent.model.vectorstore.pgvector_store import (
        _TABLE,
        PgVectorStore,
    )

    store = PgVectorStore(dsn=PG_DSN)
    with store._conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_TABLE}")
    yield store
    store.close()


class TestPgVectorStoreLive:
    def test_upsert_count_and_metadata_roundtrip(self, pg_store: Any) -> None:
        pg_store.add(
            [
                {
                    "id": "c1",
                    "vector": [1.0, 0.0],
                    "metadata": {"tenant_id": "hospital_a", "category": "clinical"},
                    "document": "warfarin protocol",
                }
            ]
        )
        assert pg_store.count() == 1
        # Upsert same id → no duplicate, updated payload wins.
        pg_store.add(
            [
                {
                    "id": "c1",
                    "vector": [0.99, 0.01],
                    "metadata": {"tenant_id": "hospital_a", "category": "updated"},
                    "document": "warfarin protocol v2",
                }
            ]
        )
        assert pg_store.count() == 1
        hits = pg_store.search([1.0, 0.0], top_k=5)
        assert hits[0].record.id == "c1"
        assert hits[0].record.metadata["category"] == "updated"
        assert hits[0].record.document == "warfarin protocol v2"
        # tenant_id was lifted into its own column.
        assert hits[0].record.metadata.get("tenant_id") is None

    def test_cosine_ordering_and_scores(self, pg_store: Any) -> None:
        pg_store.add(
            [
                {"id": "x", "vector": [1.0, 0.0], "metadata": {}, "document": ""},
                {"id": "y", "vector": [0.0, 1.0], "metadata": {}, "document": ""},
            ]
        )
        hits = pg_store.search([0.95, 0.05], top_k=2)
        assert [h.record.id for h in hits] == ["x", "y"]
        assert hits[0].score > 0.99
        assert hits[0].score >= hits[1].score

    def test_delete(self, pg_store: Any) -> None:
        pg_store.add([{"id": "d1", "vector": [1.0], "metadata": {}, "document": ""}])
        assert pg_store.count() == 1
        pg_store.delete(["d1"])
        assert pg_store.count() == 0

    def test_zero_topk_returns_empty(self, pg_store: Any) -> None:
        assert pg_store.search([1.0, 0.0], top_k=0) == []


class TestHybridRetrieverOnPgvector:
    @pytest.mark.asyncio
    async def test_ingest_dualwrite_then_retrieve_via_pg_ann(
        self, tmp_path: Path, pg_store: Any
    ) -> None:
        """Full P3b loop: TaskStore dual-writes into pgvector; HybridRetriever
        serves dense hits from it and materialises text from SQLite."""

        from doctoragent.api.schemas import ClassificationResult, SensitivityLevel

        cls = ClassificationResult(
            sensitivity=SensitivityLevel.MEDIUM,
            category="clinical",
            summary="s",
            disguise_name="n",
            disguise_extension="md",
        )

        class Embedder:
            model_name = "kw"

            def embed(self, texts):  # type: ignore[no-untyped-def]
                out = []
                for t in texts:
                    low = t.lower()
                    if "warfarin" in low:
                        out.append([1.0, 0.0])
                    elif "aspirin" in low:
                        out.append([0.0, 1.0])
                    else:
                        out.append([0.7071, 0.7071])
                return out

        embedder = Embedder()
        db_path = tmp_path / "tasks.db"
        ts = TaskStore(db_path, tenant_id="hospital_a", vector_store=pg_store)
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        ts.index_content_chunks(
            task_id,
            Path("v.md"),
            cls,
            [
                {"text": "warfarin anticoagulation protocol"},
                {"text": "aspirin antiplatelet protocol"},
            ],
            provider=embedder,
        )
        assert pg_store.count() == 2  # dual-write reached Postgres

        retriever = HybridRetriever(
            db_path,
            embedding_provider=embedder,
            tenant_id="hospital_a",
            config=RagConfig(vector_backend="pgvector", vector_backend_path=PG_DSN),
        )
        results = retriever.retrieve("warfarin dosing")
        assert results, "pgvector-backed retrieval returned nothing"
        assert "warfarin" in results[0]["text"]

        # Foreign-tenant ANN hit must be filtered by SQLite row lookup.
        pg_store.add(
            [
                {
                    "id": "foreign_chunk",
                    "vector": [1.0, 0.0],
                    "metadata": {"tenant_id": "hospital_b"},
                    "document": "other hospital secret",
                }
            ]
        )
        ids = [r["chunk_id"] for r in retriever.retrieve("warfarin dosing")]
        assert "foreign_chunk" not in ids

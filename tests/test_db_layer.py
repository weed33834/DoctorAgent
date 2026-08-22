"""Tests: database abstraction layer (P1 of docs/POSTGRES_MIGRATION.md).

Pure-Python parts (URL resolution, dialect classification, RLS policy DDL)
always run; SQLAlchemy-dependent construction tests importorskip so CI
without the ``[database]`` extra stays green.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from doctoragent.config import AegisConfig
from doctoragent.db import (
    POSTGRES_SCHEMA_SQL,
    create_async_engine_from_url,
    dialect_of,
    install_rls_policies,
    resolve_database_url,
    rls_policy_sql,
)
from doctoragent.db.engine import TENANT_GUC, tenant_scope


@pytest.fixture
def config(tmp_path: Path) -> AegisConfig:
    cfg = AegisConfig()
    cfg.paths.inbox = tmp_path / "Inbox"
    cfg.paths.vault = tmp_path / "Vault"
    cfg.paths.index = tmp_path / "Index"
    cfg.paths.logs = tmp_path / "Logs"
    cfg.paths.connections = tmp_path / "Config" / "connections.json"
    for p in [cfg.paths.inbox, cfg.paths.vault, cfg.paths.index, cfg.paths.logs]:
        p.mkdir(parents=True, exist_ok=True)
    cfg.paths.connections.parent.mkdir(parents=True, exist_ok=True)
    return cfg


class TestUrlResolution:
    def test_empty_config_yields_consolidated_sqlite(self, config: AegisConfig) -> None:
        url = resolve_database_url(config)
        assert url.startswith("sqlite+aiosqlite:///")
        assert config.paths.index.name in url

    def test_explicit_url_wins(self, config: AegisConfig) -> None:
        config.database_url = "postgresql+asyncpg://u:p@db:5432/doctor"
        assert resolve_database_url(config) == (
            "postgresql+asyncpg://u:p@db:5432/doctor"
        )


class TestDialectClassification:
    def test_sqlite_variants(self) -> None:
        assert dialect_of("sqlite:///x.db") == "sqlite"
        assert dialect_of("sqlite+aiosqlite:///x.db") == "sqlite"

    def test_postgres_variants(self) -> None:
        assert dialect_of("postgresql+asyncpg://db/x") == "postgres"
        assert dialect_of("postgresql://db/x") == "postgres"

    def test_unsupported_dialect_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported database URL"):
            dialect_of("mysql+pymysql://db/x")


class TestRlsPolicySql:
    def test_policy_compares_against_tenant_guc(self) -> None:
        sql = rls_policy_sql("vault_chunks")
        assert TENANT_GUC in sql
        assert "ENABLE ROW LEVEL SECURITY" not in sql  # enable handled separately
        assert "CREATE POLICY tenant_isolation ON vault_chunks" in sql
        assert "USING (" in sql and "WITH CHECK (" in sql

    def test_enable_sql_forces_rls(self) -> None:
        from doctoragent.db.bootstrap import _ENABLE_RLS_SQL

        rendered = _ENABLE_RLS_SQL.format(table="tasks")
        assert "ENABLE ROW LEVEL SECURITY" in rendered
        assert "FORCE ROW LEVEL SECURITY" in rendered

    def test_all_tenant_tables_have_policy_coverage(self) -> None:
        from doctoragent.db.bootstrap import TENANT_TABLES

        assert {"tasks", "vault_chunks", "conversations"} <= set(TENANT_TABLES)

    def test_schema_covers_conversations_with_tenant_column(self) -> None:
        assert "CREATE TABLE IF NOT EXISTS conversations" in POSTGRES_SCHEMA_SQL
        assert "tenant_id   TEXT NOT NULL DEFAULT 'default'" in POSTGRES_SCHEMA_SQL


class TestTenantScope:
    @pytest.mark.asyncio
    async def test_postgres_issues_set_local(self) -> None:
        executed: list[str] = []

        class FakeSession:
            async def execute(self, sql: str) -> None:
                executed.append(sql)

        async with tenant_scope(
            FakeSession(), "hospital_a", "postgres"
        ) as session:
            assert session is not None
        assert len(executed) == 1
        assert executed[0] == f"SET LOCAL {TENANT_GUC} = 'hospital_a'"

    @pytest.mark.asyncio
    async def test_quotes_defend_against_injection(self) -> None:
        executed: list[str] = []

        class FakeSession:
            async def execute(self, sql: str) -> None:
                executed.append(sql)

        async with tenant_scope(
            FakeSession(), "a'; DROP TABLE tasks; --", "postgres"):
            pass
        # The quote is escaped → the injected statement stays inert text.
        assert any("DROP TABLE" in e and "; --" not in e.split("'")[1] for e in executed)

    @pytest.mark.asyncio
    async def test_sqlite_is_noop_wrapper(self) -> None:
        executed: list[str] = []

        class FakeSession:
            async def execute(self, sql: str) -> None:
                executed.append(sql)

        async with tenant_scope(
            FakeSession(), "hospital_a", "sqlite"):
            pass
        assert executed == []

    @pytest.mark.asyncio
    async def test_postgres_requires_nonempty_tenant(self) -> None:
        class FakeSession:
            async def execute(self, sql: str) -> None:
                pass

        with pytest.raises(ValueError):
            async with tenant_scope(
            FakeSession(), "", "postgres"):
                pass


class TestEngineFactory:
    def test_missing_sqlalchemy_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("sqlalchemy"):
                raise ImportError("No module named 'sqlalchemy'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match=r"doctoragent\[database\]"):
            create_async_engine_from_url("sqlite+aiosqlite:///x.db")

    @pytest.mark.skipif(
        importlib.util.find_spec("sqlalchemy") is None,
        reason="SQLAlchemy not installed ([database] extra)",
    )
    def test_sqlite_engine_constructs(self, tmp_path: Path) -> None:
        engine = create_async_engine_from_url(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        assert engine is not None


class TestPostgresBootstrapSqlOnly:
    """install_rls_policies against a fake async conn (no PG required)."""

    @pytest.mark.asyncio
    async def test_install_runs_enable_and_policy_per_table(self) -> None:
        executed: list[str] = []

        class FakeConn:
            async def execute(self, sql: str) -> None:
                executed.append(sql)

        tables = ("tasks", "vault_chunks")
        await install_rls_policies(FakeConn(), tables=tables)
        joined = "\n".join(executed)
        for t in tables:
            assert "ENABLE ROW LEVEL SECURITY" in joined
            assert f"CREATE POLICY tenant_isolation ON {t}" in joined
        # enable + policy per table
        assert len(executed) == len(tables) * 2


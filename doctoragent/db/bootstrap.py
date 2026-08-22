"""Postgres schema + Row-Level-Security bootstrap (P1).

The policy template is the single source of truth referenced by
``docs/POSTGRES_MIGRATION.md`` (P4 acceptance: cross-tenant probe suite).
All statements are idempotent so bootstrap can run on every deploy.
"""

from __future__ import annotations

from typing import Any

from doctoragent.db.engine import TENANT_GUC

# Tables that carry tenant data and therefore get an RLS policy. Kept in one
# place so P2's ORM models can assert coverage against it.
TENANT_TABLES: tuple[str, ...] = (
    "tasks",
    "vault_chunks",
    "conversations",
    "conv_messages",
)

# SET LOCAL is transaction-scoped: pool reuse can never leak a tenant.


def _enable_rls_statements(table: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
    ]


def rls_policy_sql(table: str) -> str:
    """Human-readable/CLI form: both statements joined with ``;``."""
    return ";".join(rls_policy_statements(table)) + ";"


def rls_policy_statements(table: str) -> list[str]:
    """Idempotent RLS DDL for *table*, one statement per item.

    asyncpg-style drivers forbid multiple commands per prepared statement,
    so programmatic callers consume the list form.
    """
    return [
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = current_setting('{TENANT_GUC}', true)) "
        f"WITH CHECK (tenant_id = current_setting('{TENANT_GUC}', true))",
    ]


async def install_rls_policies(conn: Any, tables: tuple[str, ...] = TENANT_TABLES) -> None:
    """Enable + (re)create the tenant-isolation policy on each table.

    Intended to run inside a migration/upgrade path with a role that may
    bypass RLS; normal application roles must NOT have BYPASSRLS.
    """
    for table in tables:
        from sqlalchemy import text as _sql_text

        for stmt in _enable_rls_statements(table):
            await conn.execute(_sql_text(stmt))
        for stmt in rls_policy_statements(table):
            await conn.execute(_sql_text(stmt))


POSTGRES_SCHEMA_SQL = """
-- Reference schema for P2/P4 (mirrors the SQLite DDL in
-- orchestration/task_store.py). Migrations tooling (alembic) takes over
-- versioning from P3 onward; this block bootstraps fresh databases.

CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    source_path  TEXT,
    classification TEXT,
    vault_path   TEXT,
    salt         BYTEA,
    nonce        BYTEA,
    message      TEXT DEFAULT '',
    created_at   TEXT DEFAULT '',
    updated_at   TEXT DEFAULT '',
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    parent_task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_state ON tasks(tenant_id, state);

CREATE TABLE IF NOT EXISTS vault_chunks (
    chunk_id    TEXT PRIMARY KEY,
    task_id     TEXT,
    vault_path  TEXT,
    category    TEXT,
    summary     TEXT,
    chunk_index INTEGER,
    text        TEXT,
    start_char  INTEGER,
    end_char    INTEGER,
    content_hash TEXT,
    embedding   BYTEA,
    model       TEXT,
    created_at  TEXT,
    tenant_id   TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant ON vault_chunks(tenant_id);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT,
    updated_at  TEXT,
    meta        JSONB DEFAULT '{{}}'::jsonb,
    tenant_id   TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_conv_tenant_updated
    ON conversations(tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS conv_messages (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT,
    role             TEXT,
    content          TEXT,
    ts               TEXT,
    feedback         INTEGER DEFAULT 0,
    feedback_comment TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_msgs_conv ON conv_messages(conversation_id);

CREATE TABLE IF NOT EXISTS conv_shares (
    token           TEXT PRIMARY KEY,
    conversation_id TEXT,
    created_at      TEXT,
    expires_at      DOUBLE PRECISION
);
"""

"""Database abstraction layer (P1 of docs/POSTGRES_MIGRATION.md).

Dual-driver foundation so the storage tier can move from per-module SQLite
files to PostgreSQL 15 + RLS without rewriting call sites blind:

* :func:`resolve_database_url` — config → SQLAlchemy URL (pure, testable)
* :func:`dialect_of` — "sqlite" | "postgres" classification (pure)
* :func:`create_async_engine_from_url` — lazy SQLAlchemy engine factory
* :func:`tenant_scope` — async context manager issuing
  ``SET LOCAL app.tenant_id`` inside a Postgres transaction (no-op on
  SQLite); every tenant-scoped query in later phases MUST run inside it.

The default configuration keeps the legacy per-module SQLite files; this
package is inert unless ``DOCTORAGENT_DATABASE_URL`` / ``database_url`` is
set or a caller imports it explicitly.
"""

from doctoragent.db.bootstrap import (
    POSTGRES_SCHEMA_SQL,
    install_rls_policies,
    rls_policy_sql,
)
from doctoragent.db.engine import (
    create_async_engine_from_url,
    dialect_of,
    resolve_database_url,
    tenant_scope,
)

__all__ = [
    "POSTGRES_SCHEMA_SQL",
    "create_async_engine_from_url",
    "dialect_of",
    "install_rls_policies",
    "resolve_database_url",
    "rls_policy_sql",
    "tenant_scope",
]

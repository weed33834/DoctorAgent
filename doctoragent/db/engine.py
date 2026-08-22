"""Dual-driver async engine factory + tenant context (P1).

SQLAlchemy is imported lazily so the core package keeps its zero-dependency
guarantee: importing :mod:`doctoragent.db` without the ``[database]`` extra
installed only fails when an engine is actually constructed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from doctoragent.config import AegisConfig

logger = logging.getLogger(__name__)

# Transaction-scoped GUC read by every RLS policy (see bootstrap.py).
TENANT_GUC = "app.tenant_id"


def resolve_database_url(config: AegisConfig) -> str:
    """Resolve the SQLAlchemy URL from *config*.

    Priority:

    1. ``config.database_url`` when set (env
       ``DOCTORAGENT_DATABASE_URL`` via pydantic-settings).
    2. Legacy default: a SQLite file at ``<index>/doctoragent.db`` — used by
       later phases as the consolidated store; existing per-module databases
       remain untouched during P1.
    """
    url = (getattr(config, "database_url", "") or "").strip()
    if url:
        return url
    return f"sqlite+aiosqlite:///{Path(config.paths.index) / 'doctoragent.db'}"


def dialect_of(url: str) -> str:
    """Classify *url* into ``"sqlite"`` or ``"postgres"``.

    Raises ``ValueError`` for URLs pointing at unsupported dialects — failing
    loudly beats silently running clinical data through an untested driver.
    """
    low = (url or "").lower()
    if low.startswith("sqlite"):
        return "sqlite"
    if low.startswith(("postgresql", "postgres")):
        return "postgres"
    raise ValueError(
        f"Unsupported database URL: {url!r}. "
        "Supported: sqlite / sqlite+aiosqlite / postgresql+asyncpg."
    )


def create_async_engine_from_url(url: str, **kwargs: Any) -> Any:
    """Build a SQLAlchemy async engine for *url*.

    Driver requirements: ``aiosqlite`` for sqlite URLs, ``asyncpg`` for
    postgres — both ship in the ``[database]`` extra. Postgres defaults are
    production-shaped (pre-ping, bounded pool); callers may override any
    engine kwarg.
    """
    try:
        from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — depends on extras
        raise ImportError(
            "SQLAlchemy is required for doctoragent.db. "
            "Install it with: pip install 'doctoragent[database]'"
        ) from exc

    kwargs.setdefault("pool_pre_ping", True)
    if dialect_of(url) == "postgres":
        kwargs.setdefault("pool_size", 10)
        kwargs.setdefault("max_overflow", 20)
    return create_async_engine(url, **kwargs)


@asynccontextmanager
async def tenant_scope(
    session: Any, tenant_id: str, dialect: str
) -> AsyncIterator[Any]:
    """Bind *session*'s transaction to *tenant_id* and yield it.

    On PostgreSQL this executes ``SET LOCAL app.tenant_id = …`` inside the
    current transaction; every RLS policy (see ``bootstrap.rls_policy_sql``)
    compares against that GUC. ``SET LOCAL`` auto-reverts at commit/rollback,
    so pool reuse can never leak a tenant across requests.

    On SQLite this is a no-op wrapper (RLS does not exist there; isolation is
    enforced by explicit WHERE clauses until P2/P4).
    """
    if dialect == "postgres":
        if not tenant_id:
            raise ValueError("tenant_scope requires a non-empty tenant_id")
        # Quote defensively; SET LOCAL has no bind-parameter syntax.
        safe = str(tenant_id).replace("'", "''")
        await session.execute(f"SET LOCAL {TENANT_GUC} = '{safe}'")
    yield session

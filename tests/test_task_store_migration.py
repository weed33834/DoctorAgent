"""Regression test: legacy v0.2-style tasks table auto-migrates (v0.3.27).

Deployments upgraded from the old job-queue design carry a ``tasks`` table
keyed by ``id`` with ``status/params/progress`` columns. The current code
needs ``task_id/state``; before this fix ``_init_db`` only bolted on
timestamp columns and every query crashed with "no such column: task_id".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from doctoragent.orchestration.task_store import TaskStore

LEGACY_DDL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT,
    name TEXT,
    params TEXT,
    status TEXT,
    progress REAL,
    error TEXT,
    result TEXT,
    retries INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3
)
"""


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    db = tmp_path / "tasks.db"
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_DDL)
    conn.execute(
        "INSERT INTO tasks (id, task_type, name, params, status, error) "
        "VALUES ('legacy-1', 'classify', 'demo', '{}', 'failed', 'boom')"
    )
    conn.commit()
    conn.close()
    return db


class TestLegacyTasksMigration:
    def test_store_constructs_and_migrates(self, legacy_db: Path) -> None:
        TaskStore(legacy_db)
        cols = {r[1] for r in sqlite3.connect(legacy_db).execute("PRAGMA table_info(tasks)")}
        assert {"task_id", "state", "tenant_id"} <= cols

    def test_rows_mapped_and_archived(self, legacy_db: Path) -> None:
        ts = TaskStore(legacy_db)
        recent = ts.list_recent(limit=5)
        assert len(recent) == 1
        # Non-UUID legacy ids get a deterministic uuid5 synthesized.
        import uuid as _u

        expected = str(_u.uuid5(_u.NAMESPACE_URL, "doctoragent-legacy:legacy-1"))
        assert str(recent[0].task_id) == expected
        assert (recent[0].state or "").upper() == "FAILED"
        # Original data preserved in an archive table for manual inspection.
        conn = sqlite3.connect(legacy_db)
        n = conn.execute("SELECT count(*) FROM tasks_legacy_v1").fetchone()[0]
        assert n == 1

    def test_list_recent_no_longer_crashes(self, legacy_db: Path) -> None:
        """The exact pre-fix failure mode: SELECT task_id → no such column."""
        ts = TaskStore(legacy_db)
        rows = ts.list_recent(limit=5)
        assert isinstance(rows, list)

    def test_fresh_db_unaffected(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        ts = TaskStore(db)
        tid = __import__("uuid").uuid4()
        ts.create(tid, Path("v.md"))
        assert str(ts.list_recent(limit=5)[0].task_id) == str(tid)

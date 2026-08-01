"""SQLite-backed agent checkpoint persistence.

Provides :class:`CheckpointStore` for atomic save/load of
:class:`AgentCheckpoint` snapshots, enabling an agent run to be paused and
later resumed (see :meth:`Agent.save_checkpoint` /
:meth:`Agent.resume_from_checkpoint` in :mod:`doctoragent.model.agent`).

The store uses its own SQLite database file (``checkpoints.db`` under the
configured index directory) so it does not couple with the
:class:`~doctoragent.orchestration.task_store.TaskStore` schema. Writes are
serialised by a :class:`threading.Lock` and the connection is opened with
WAL mode + ``busy_timeout`` for safe concurrent reads.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from doctoragent._utils import open_sqlite

logger = logging.getLogger(__name__)


class AgentCheckpoint(BaseModel):
    """Serialisable snapshot of an in-flight agent run.

    Stored by :class:`CheckpointStore` so an agent can be rebuilt and
    resumed after a crash or explicit pause. ``messages`` is a list of the
    OpenAI-style ``{"role", "content", ...}`` dicts accumulated so far;
    ``plan`` is the current :class:`~doctoragent.model.agent.ExecutionPlan`
    serialised to a plain dict (or ``None`` when planning is disabled).
    """

    task_id: str
    iteration: int = 0
    messages: list[dict[str, Any]] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    tool_calls_made: int = 0
    created_at: str = ""
    status: str = "paused"  # paused | completed | failed
    # 完整执行轨迹：每个元素为 AgentStep.model_dump() 的 dict，恢复时重建为 AgentStep
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
    # 工作记忆：执行过程中累积的中间结果（_working_memory）
    working_memory: dict[str, Any] = Field(default_factory=dict)


class CheckpointStore:
    """SQLite-backed store for :class:`AgentCheckpoint` snapshots.

    Schema: ``checkpoints(task_id TEXT PRIMARY KEY, state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL)``. Connections are opened per-operation with
    WAL mode (via :func:`doctoragent._utils.open_sqlite`) and writes are
    serialised by an instance-level :class:`threading.Lock`.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived WAL-mode connection (caller closes it)."""
        return open_sqlite(self.db_path)

    def _init_db(self) -> None:
        """Create the checkpoints table if it does not exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save(self, task_id: str, state: AgentCheckpoint) -> None:
        """Atomically upsert *state* under *task_id*.

        Serialised by ``self._lock`` so concurrent saves from multiple
        threads cannot interleave; SQLite's own ``busy_timeout`` handles
        any cross-process contention.
        """
        state_json = state.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (task_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (task_id, state_json, now),
            )
            conn.commit()

    def load(self, task_id: str) -> AgentCheckpoint | None:
        """Load the checkpoint for *task_id*, or ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return AgentCheckpoint.model_validate_json(row[0])
        except Exception as e:  # noqa: BLE001 - corrupt JSON shouldn't crash reads
            logger.warning("CheckpointStore.load: failed to parse %r: %s", task_id, e)
            return None

    def list_checkpoints(self) -> list[str]:
        """Return all stored task_ids (unordered)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT task_id FROM checkpoints").fetchall()
        return [r[0] for r in rows]

    def delete(self, task_id: str) -> None:
        """Delete the checkpoint for *task_id* (no-op if absent)."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM checkpoints WHERE task_id = ?", (task_id,))
            conn.commit()

"""Task center (M14 K).

A real async task registry: register background tasks (import / export /
re-index / backup), track status & progress, list, retry failed tasks, and
cancel running ones. Used as the unified "task center" for long-running
platform operations.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_TYPES = {"import", "export", "reindex", "backup", "sync", "custom"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class TaskCenter:
    """SQLite-backed task registry with retry / cancel semantics."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, task_type TEXT, name TEXT, params TEXT,
                    status TEXT, progress REAL, error TEXT, result TEXT,
                    retries INTEGER, created_at TEXT, updated_at TEXT
                );
                """
            )
            conn.commit()

    def register_handler(self, task_type: str, fn: Callable[[dict[str, Any]], Any]) -> None:
        self._handlers[task_type] = fn

    def create(
        self, task_type: str, name: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if task_type not in TASK_TYPES:
            raise ValueError(f"unknown task_type {task_type}")
        row = {
            "id": _id("task"),
            "task_type": task_type,
            "name": name,
            "params": params or {},
            "status": "pending",
            "progress": 0.0,
            "error": "",
            "result": "",
            "retries": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id,task_type,name,params,status,progress,error,result,"
                "retries,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    task_type,
                    name,
                    __import__("json").dumps(row["params"], ensure_ascii=False),
                    "pending",
                    0.0,
                    "",
                    "",
                    0,
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            conn.commit()
        # Auto-execute synchronously when a handler is registered (keeps it real).
        if task_type in self._handlers:
            try:
                self._update(row["id"], status="running", progress=0.05)
                result = self._handlers[task_type](row["params"])
                self._update(row["id"], status="completed", progress=1.0, result=str(result))
            except Exception as exc:  # noqa: BLE001
                self._update(row["id"], status="failed", error=str(exc))
        return row

    def _update(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "progress", "error", "result", "retries"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return
        sets.append("updated_at=?")
        vals = [fields[k] for k in fields if k in allowed]
        vals.append(_now())
        vals.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)  # nosec B608
            conn.commit()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["params"] = __import__("json").loads(d["params"] or "{}")
        return d

    def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) | {"params": __import__("json").loads(r["params"] or "{}")} for r in rows]

    def retry(self, task_id: str) -> dict[str, Any] | None:
        task = self.get(task_id)
        if task is None:
            return None
        handler = self._handlers.get(task["task_type"])
        if handler is None:
            self._update(task_id, status="failed", error="no handler for retry")
            return self.get(task_id)
        try:
            self._update(task_id, status="running", retries=task["retries"] + 1, error="")
            result = handler(task["params"])
            self._update(task_id, status="completed", progress=1.0, result=str(result))
        except Exception as exc:  # noqa: BLE001
            self._update(task_id, status="failed", error=str(exc))
        return self.get(task_id)

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        task = self.get(task_id)
        if task is None:
            return None
        if task["status"] in ("pending", "running"):
            self._update(task_id, status="canceled")
        return self.get(task_id)

    def summary(self) -> dict[str, Any]:
        tasks = self.list(limit=100000)
        by_status: dict[str, int] = {}
        for t in tasks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        return {"total": len(tasks), "by_status": by_status}

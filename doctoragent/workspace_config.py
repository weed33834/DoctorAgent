"""Workspace configuration store (shared by chat tools and management UI).

A single SQLite-backed store for **conversation-editable** resources:

* prompt templates (system prompts)
* skills (custom capability packs)
* experts (custom role presets)

Both the agent's management *tools* (callable in chat) and the management
*API endpoints* read/write this same store, so a change made in the chat is
immediately visible in the management interface and vice-versa.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from doctoragent._utils import open_sqlite


def _now() -> str:
    from doctoragent._utils import utcnow_iso

    return utcnow_iso()


def _id(prefix: str) -> str:
    """Delegate to the shared :func:`generate_id` in :mod:`doctoragent._utils`."""
    from doctoragent._utils import generate_id

    return generate_id(prefix)


class WorkspaceConfig:
    """SQLite store for prompts / skills / experts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self.db_path, row_factory=sqlite3.Row)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ws_prompts (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, template TEXT,
                    description TEXT, variables TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS ws_skills (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, description TEXT,
                    triggers TEXT, code TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS ws_experts (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, title TEXT,
                    system_prompt TEXT, tools TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS ws_settings (
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
                );
                """
            )
            conn.commit()

    # ── prompts ─────────────────────────────────────────────────────

    def list_prompts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM ws_prompts ORDER BY name").fetchall()
        return [dict(r) | {"variables": self._loads(r["variables"])} for r in rows]

    def upsert_prompt(
        self, name: str, template: str, *, description: str = "", variables: list[str] | None = None
    ) -> dict[str, Any]:
        pid = _id("prm")
        row = {
            "id": pid,
            "name": name,
            "template": template,
            "description": description,
            "variables": variables or _extract_vars(template),
            "updated_at": _now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ws_prompts (id,name,template,description,variables,updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET template=excluded.template, "
                "description=excluded.description, variables=excluded.variables, updated_at=excluded.updated_at",
                (
                    pid,
                    name,
                    template,
                    description,
                    __import__("json").dumps(row["variables"], ensure_ascii=False),
                    row["updated_at"],
                ),
            )
            conn.commit()
        # return the stored row (with the same id used by name upsert)
        return self.get_prompt(name) or row

    def get_prompt(self, name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM ws_prompts WHERE name=? OR id=?", (name, name)
            ).fetchone()
        return dict(r) | {"variables": self._loads(r["variables"])} if r else None

    # ── skills ──────────────────────────────────────────────────────

    def list_skills(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM ws_skills ORDER BY name").fetchall()
        return [dict(r) | {"triggers": self._loads(r["triggers"])} for r in rows]

    def register_skill(
        self, name: str, description: str, *, triggers: list[str] | None = None, code: str = ""
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ws_skills (id,name,description,triggers,code,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
                "triggers=excluded.triggers, code=excluded.code, updated_at=excluded.updated_at",
                (
                    _id("sk"),
                    name,
                    description,
                    __import__("json").dumps(triggers or [], ensure_ascii=False),
                    code,
                    _now(),
                ),
            )
            conn.commit()
        return next(s for s in self.list_skills() if s["name"] == name)

    # ── experts ─────────────────────────────────────────────────────

    def list_experts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM ws_experts ORDER BY name").fetchall()
        return [dict(r) | {"tools": self._loads(r["tools"])} for r in rows]

    def create_expert(
        self, name: str, title: str, system_prompt: str, *, tools: list[str] | None = None
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ws_experts (id,name,title,system_prompt,tools,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET title=excluded.title, "
                "system_prompt=excluded.system_prompt, tools=excluded.tools, updated_at=excluded.updated_at",
                (
                    _id("exp"),
                    name,
                    title,
                    system_prompt,
                    __import__("json").dumps(tools or [], ensure_ascii=False),
                    _now(),
                ),
            )
            conn.commit()
        return next(e for e in self.list_experts() if e["name"] == name)

    # ── settings (generic key-value persistence) ─────────────────────

    def set_settings(self, values: dict[str, str]) -> None:
        with self._connect() as conn:
            for k, v in values.items():
                conn.execute(
                    "INSERT INTO ws_settings (key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, str(v), _now()),
                )
            conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM ws_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def summary(self) -> dict[str, Any]:
        return {
            "prompts": len(self.list_prompts()),
            "skills": len(self.list_skills()),
            "experts": len(self.list_experts()),
        }

    @staticmethod
    def _loads(v: str | None) -> Any:
        try:
            return __import__("json").loads(v or "[]")
        except Exception:  # noqa: BLE001
            return []


def _extract_vars(template: str) -> list[str]:
    """Extract ``{var}`` placeholders from a template string."""
    import re

    return sorted(set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template or "")))

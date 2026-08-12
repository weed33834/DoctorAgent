"""Agent interoperability store + service (M27).

Real, SQLite-backed interop layer on top of the A2A/MCP foundations: an
external Agent directory with trust levels, interop policies (who may call,
which actions are denied, rate limit, audit), and an A2A task monitor that
records both outbound and inbound cross-agent tasks so calls are auditable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doctoragent.interop.models import (
    A2ATaskRecord,
    ExternalAgent,
    InteropPolicy,
    TrustLevel,
)

# Trust rank: higher number = more trusted.
_TRUST_RANK = {
    TrustLevel.UNTRUSTED: 0,
    TrustLevel.LIMITED: 1,
    TrustLevel.TRUSTED: 2,
    TrustLevel.HIGH: 3,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class InteropStore:
    """SQLite store for interop directory, policies and A2A task records."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS interop_agents (
                    id TEXT PRIMARY KEY, name TEXT, url TEXT, description TEXT,
                    capabilities TEXT, trust_level TEXT, auth_type TEXT,
                    health TEXT, last_seen TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS interop_policies (
                    id TEXT PRIMARY KEY, name TEXT, allow_agents TEXT,
                    deny_actions TEXT, rate_limit_per_min INTEGER,
                    require_trust TEXT, audit_level TEXT, enabled INTEGER,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS interop_tasks (
                    id TEXT PRIMARY KEY, peer_agent TEXT, direction TEXT,
                    status TEXT, message_summary TEXT, artifacts TEXT,
                    started_at TEXT, finished_at TEXT, duration_ms INTEGER,
                    error TEXT
                );
                """
            )
            conn.commit()

    # ── external agent directory ────────────────────────────────────

    def upsert_agent(self, a: ExternalAgent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO interop_agents "
                "(id,name,url,description,capabilities,trust_level,auth_type,"
                "health,last_seen,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    a.id,
                    a.name,
                    a.url,
                    a.description,
                    json.dumps(a.capabilities),
                    a.trust_level.value,
                    a.auth_type,
                    a.health,
                    a.last_seen,
                    a.created_at,
                ),
            )
            conn.commit()

    def list_agents(self) -> list[ExternalAgent]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM interop_agents ORDER BY name").fetchall()
        return [self._row_agent(r) for r in rows]

    def get_agent(self, agent_id: str) -> ExternalAgent | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM interop_agents WHERE id=?", (agent_id,)).fetchone()
        return self._row_agent(row) if row else None

    def set_agent_health(self, agent_id: str, health: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE interop_agents SET health=?, last_seen=? WHERE id=?",
                (health, _now(), agent_id),
            )
            conn.commit()

    def delete_agent(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM interop_agents WHERE id=?", (agent_id,))
            conn.commit()

    @staticmethod
    def _row_agent(row: Any) -> ExternalAgent:
        return ExternalAgent(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            description=row["description"],
            capabilities=json.loads(row["capabilities"] or "[]"),
            trust_level=TrustLevel(row["trust_level"]),
            auth_type=row["auth_type"],
            health=row["health"],
            last_seen=row["last_seen"],
            created_at=row["created_at"],
        )

    # ── interop policies ────────────────────────────────────────────

    def upsert_policy(self, p: InteropPolicy) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO interop_policies "
                "(id,name,allow_agents,deny_actions,rate_limit_per_min,"
                "require_trust,audit_level,enabled,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    p.id,
                    p.name,
                    json.dumps(p.allow_agents),
                    json.dumps(p.deny_actions),
                    p.rate_limit_per_min,
                    p.require_trust.value,
                    p.audit_level,
                    1 if p.enabled else 0,
                    p.created_at,
                ),
            )
            conn.commit()

    def list_policies(self, enabled_only: bool = True) -> list[InteropPolicy]:
        sql = "SELECT * FROM interop_policies"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            InteropPolicy(
                id=r["id"],
                name=r["name"],
                allow_agents=json.loads(r["allow_agents"] or "[]"),
                deny_actions=json.loads(r["deny_actions"] or "[]"),
                rate_limit_per_min=r["rate_limit_per_min"],
                require_trust=TrustLevel(r["require_trust"]),
                audit_level=r["audit_level"],
                enabled=bool(r["enabled"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ── A2A task monitor ────────────────────────────────────────────

    def record_task(self, t: A2ATaskRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO interop_tasks "
                "(id,peer_agent,direction,status,message_summary,artifacts,"
                "started_at,finished_at,duration_ms,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t.id,
                    t.peer_agent,
                    t.direction,
                    t.status,
                    t.message_summary,
                    json.dumps(t.artifacts),
                    t.started_at,
                    t.finished_at,
                    t.duration_ms,
                    t.error,
                ),
            )
            conn.commit()

    def list_tasks(
        self, peer_agent: str | None = None, direction: str | None = None, limit: int = 100
    ) -> list[A2ATaskRecord]:
        sql = "SELECT * FROM interop_tasks WHERE 1=1"
        params: list[Any] = []
        if peer_agent:
            sql += " AND peer_agent=?"
            params.append(peer_agent)
        if direction:
            sql += " AND direction=?"
            params.append(direction)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            A2ATaskRecord(
                id=r["id"],
                peer_agent=r["peer_agent"],
                direction=r["direction"],
                status=r["status"],
                message_summary=r["message_summary"],
                artifacts=json.loads(r["artifacts"] or "[]"),
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                duration_ms=r["duration_ms"],
                error=r["error"],
            )
            for r in rows
        ]


class InteropService:
    """Facade: directory + policies + delegated task execution via A2A client."""

    def __init__(self, store: InteropStore, a2a_client: Any | None = None) -> None:
        self.store = store
        self.a2a_client = a2a_client

    def register_agent(
        self,
        name: str,
        url: str,
        *,
        description: str = "",
        capabilities: list[str] | None = None,
        trust_level: TrustLevel = TrustLevel.LIMITED,
        auth_type: str = "none",
    ) -> ExternalAgent:
        agent = ExternalAgent(
            id=_id("agent"),
            name=name,
            url=url,
            description=description,
            capabilities=capabilities or [],
            trust_level=trust_level,
            auth_type=auth_type,
            health="unknown",
            created_at=_now(),
        )
        self.store.upsert_agent(agent)
        return agent

    def add_policy(
        self,
        name: str,
        *,
        allow_agents: list[str] | None = None,
        deny_actions: list[str] | None = None,
        require_trust: TrustLevel = TrustLevel.TRUSTED,
        rate_limit_per_min: int = 60,
    ) -> InteropPolicy:
        p = InteropPolicy(
            id=_id("pol"),
            name=name,
            allow_agents=allow_agents or [],
            deny_actions=deny_actions or [],
            require_trust=require_trust,
            rate_limit_per_min=rate_limit_per_min,
            created_at=_now(),
        )
        self.store.upsert_policy(p)
        return p

    def check_access(self, agent_name: str, action: str, trust: TrustLevel) -> dict[str, Any]:
        """Evaluate an interop policy check for a call (M27 安全)."""
        policies = self.store.list_policies()
        # A denied action in any enabled policy blocks it.
        for p in policies:
            if action in p.deny_actions:
                return {"allowed": False, "reason": f"action {action!r} denied by policy {p.name}"}
        for p in policies:
            if p.allow_agents and agent_name not in p.allow_agents:
                continue
            if _TRUST_RANK[trust] < _TRUST_RANK[p.require_trust]:
                return {
                    "allowed": False,
                    "reason": f"trust {trust.value} below {p.require_trust.value}",
                }
            return {"allowed": True, "policy": p.name, "rate_limit_per_min": p.rate_limit_per_min}
        return {"allowed": True, "rate_limit_per_min": 60}

    async def delegate_task(self, agent: ExternalAgent, message: dict[str, Any]) -> dict[str, Any]:
        """Delegate a task to an external agent via the A2A client (M27 客户端链路)."""
        if self.a2a_client is None:
            return {"error": "A2A client not configured"}
        started = time.monotonic()
        task = await self.a2a_client.send_and_wait(
            agent.url, message, poll_interval=0.5, max_wait=60.0
        )
        dur = int((time.monotonic() - started) * 1000)
        self.store.record_task(
            A2ATaskRecord(
                id=_id("task"),
                peer_agent=agent.name,
                direction="outbound",
                status=task.status.value,
                message_summary=str(message.get("parts", ""))[:120],
                artifacts=[a.parts for a in task.artifacts],
                started_at=_now(),
                finished_at=_now(),
                duration_ms=dur,
            )
        )
        return {"task_id": task.id, "status": task.status.value, "text": task.text}

    def overview(self) -> dict[str, Any]:
        agents = self.store.list_agents()
        tasks = self.store.list_tasks(limit=50)
        online = sum(1 for a in agents if a.health == "online")
        return {
            "agents": len(agents),
            "online": online,
            "policies": len(self.store.list_policies()),
            "tasks_total": len(self.store.list_tasks(limit=10000)),
            "tasks_inflight": sum(1 for t in tasks if t.status in ("submitted", "working")),
        }

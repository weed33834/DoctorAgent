"""Enterprise / organization SQLite store (M14).

A real, self-contained persistence layer for the organization & governance
platform: organizations, departments, user accounts, MFA challenges, login
events, budgets, quotas, system settings, announcements, maintenance state and
API keys. Uses SQLite with WAL-like robustness via ``open_sqlite`` semantics
(one connection per operation, autocommit on context exit).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from doctoragent.enterprise.models import (
    AccountStatus,
    Announcement,
    ApiKeyRecord,
    Budget,
    Department,
    LoginEvent,
    MaintenanceState,
    MFAChallenge,
    Org,
    OrgStatus,
    Quota,
    SystemSetting,
    UserAccount,
    UserRole,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_sqlite(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults (row factory off, WAL)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class EnterpriseStore:
    """SQLite-backed store for enterprise / organization data."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self.db_path)

    # ── schema ──────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS enterprise_orgs (
                    id TEXT PRIMARY KEY, name TEXT, domain TEXT, plan TEXT,
                    status TEXT, settings TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_departments (
                    id TEXT PRIMARY KEY, org_id TEXT, name TEXT,
                    parent_id TEXT, path TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_users (
                    id TEXT PRIMARY KEY, org_id TEXT, dept_id TEXT, email TEXT,
                    display_name TEXT, role TEXT, status TEXT, password_hash TEXT,
                    mfa_status TEXT, mfa_secret TEXT, failed_attempts INTEGER,
                    locked_until TEXT, last_login TEXT, locale TEXT, timezone TEXT,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_login_events (
                    id TEXT PRIMARY KEY, user_id TEXT, org_id TEXT, email TEXT,
                    ip TEXT, user_agent TEXT, result TEXT, detail TEXT, at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_mfa_challenges (
                    user_id TEXT PRIMARY KEY, org_id TEXT, secret TEXT,
                    verified INTEGER, expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_budgets (
                    id TEXT PRIMARY KEY, scope TEXT, scope_id TEXT, period TEXT,
                    amount_usd REAL, alert_threshold REAL, hard_limit INTEGER,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_quotas (
                    id TEXT PRIMARY KEY, scope TEXT, scope_id TEXT,
                    tokens_per_day INTEGER, calls_per_day INTEGER,
                    storage_mb INTEGER, concurrent INTEGER, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_settings (
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_announcements (
                    id TEXT PRIMARY KEY, title TEXT, content TEXT, level TEXT,
                    pinned INTEGER, active INTEGER, created_at TEXT, expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_maintenance (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER, message TEXT, readonly INTEGER, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enterprise_api_keys (
                    id TEXT PRIMARY KEY, org_id TEXT, label TEXT, prefix TEXT,
                    scopes TEXT, expires_at TEXT, last_used_at TEXT, created_at TEXT
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO enterprise_maintenance "
                "(id, enabled, message, readonly, updated_at) VALUES (1, 0, '', 0, ?)",
                (_now(),),
            )
            conn.commit()

    # ── org (A) ─────────────────────────────────────────────────────

    def create_org(self, org: Org) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_orgs "
                "(id,name,domain,plan,status,settings,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (org.id, org.name, org.domain, org.plan, org.status.value,
                 json.dumps(org.settings), org.created_at, org.updated_at),
            )
            conn.commit()

    def get_org(self, org_id: str) -> Org | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_orgs WHERE id = ?", (org_id,)
            ).fetchone()
        return self._row_org(row) if row else None

    def list_orgs(self) -> list[Org]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM enterprise_orgs ORDER BY created_at").fetchall()
        return [self._row_org(r) for r in rows]

    def update_org(self, org_id: str, name: str | None = None, domain: str | None = None,
                   plan: str | None = None, status: str | None = None,
                   settings: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE enterprise_orgs SET name=COALESCE(?,name), domain=COALESCE(?,domain), "
                "plan=COALESCE(?,plan), status=COALESCE(?,status), "
                "settings=COALESCE(?,settings), updated_at=? WHERE id=?",
                (name, domain, plan, status,
                 json.dumps(settings) if settings is not None else None,
                 _now(), org_id),
            )
            conn.commit()

    @staticmethod
    def _row_org(row: Any) -> Org:
        return Org(
            id=row["id"], name=row["name"], domain=row["domain"], plan=row["plan"],
            status=OrgStatus(row["status"]),
            settings=json.loads(row["settings"] or "{}"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # ── department (A) ──────────────────────────────────────────────

    def create_department(self, dept: Department) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_departments "
                "(id,org_id,name,parent_id,path,created_at) VALUES (?,?,?,?,?,?)",
                (dept.id, dept.org_id, dept.name, dept.parent_id, dept.path, dept.created_at),
            )
            conn.commit()

    def list_departments(self, org_id: str) -> list[Department]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM enterprise_departments WHERE org_id = ?", (org_id,)
            ).fetchall()
        return [self._row_dept(r) for r in rows]

    def get_department(self, dept_id: str) -> Department | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_departments WHERE id = ?", (dept_id,)
            ).fetchone()
        return self._row_dept(row) if row else None

    def update_department(self, dept_id: str, name: str | None = None,
                          parent_id: str | None = None, path: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE enterprise_departments SET name=COALESCE(?,name), "
                "parent_id=COALESCE(?,parent_id), path=COALESCE(?,path) WHERE id=?",
                (name, parent_id, path, dept_id),
            )
            conn.commit()

    def delete_department(self, dept_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM enterprise_departments WHERE id=?", (dept_id,))
            conn.commit()

    @staticmethod
    def _row_dept(row: Any) -> Department:
        return Department(
            id=row["id"], org_id=row["org_id"], name=row["name"],
            parent_id=row["parent_id"], path=row["path"], created_at=row["created_at"],
        )

    # ── users (B) ───────────────────────────────────────────────────

    def create_user(self, user: UserAccount) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_users "
                "(id,org_id,dept_id,email,display_name,role,status,password_hash,"
                "mfa_status,mfa_secret,failed_attempts,locked_until,last_login,"
                "locale,timezone,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user.id, user.org_id, user.dept_id, user.email, user.display_name,
                 user.role.value, user.status.value, user.password_hash,
                 user.mfa_status.value, user.mfa_secret, user.failed_attempts,
                 user.locked_until, user.last_login, user.locale, user.timezone,
                 user.created_at, user.updated_at),
            )
            conn.commit()

    def get_user_by_email(self, org_id: str, email: str) -> UserAccount | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_users WHERE org_id=? AND email=?",
                (org_id, email.lower()),
            ).fetchone()
        return self._row_user(row) if row else None

    def get_user(self, user_id: str) -> UserAccount | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_users WHERE id=?", (user_id,)
            ).fetchone()
        return self._row_user(row) if row else None

    def list_users(self, org_id: str | None = None, dept_id: str | None = None,
                   role: str | None = None, status: str | None = None) -> list[UserAccount]:
        sql = "SELECT * FROM enterprise_users WHERE 1=1"
        params: list[Any] = []
        if org_id:
            sql += " AND org_id=?"; params.append(org_id)
        if dept_id:
            sql += " AND dept_id=?"; params.append(dept_id)
        if role:
            sql += " AND role=?"; params.append(role)
        if status:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_user(r) for r in rows]

    def update_user(self, user_id: str, **fields: Any) -> None:
        allowed = {"dept_id", "display_name", "role", "status", "password_hash",
                   "mfa_status", "mfa_secret", "failed_attempts", "locked_until",
                   "last_login", "locale", "timezone"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return
        vals = [
            f.value if isinstance(f, (UserRole, AccountStatus)) else f
            for f in fields.values() if True
        ]
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(user_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE enterprise_users SET {', '.join(sets)} WHERE id=?",
                vals,
            )
            conn.commit()

    def count_users(self, org_id: str | None = None) -> int:
        with self._connect() as conn:
            if org_id:
                n = conn.execute(
                    "SELECT COUNT(*) c FROM enterprise_users WHERE org_id=?", (org_id,)
                ).fetchone()["c"]
            else:
                n = conn.execute("SELECT COUNT(*) c FROM enterprise_users").fetchone()["c"]
        return int(n)

    def delete_user(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM enterprise_users WHERE id=?", (user_id,))
            conn.commit()

    @staticmethod
    def _row_user(row: Any) -> UserAccount:
        return UserAccount(
            id=row["id"], org_id=row["org_id"], dept_id=row["dept_id"], email=row["email"],
            display_name=row["display_name"], role=UserRole(row["role"]),
            status=AccountStatus(row["status"]), password_hash=row["password_hash"],
            mfa_status=row["mfa_status"], mfa_secret=row["mfa_secret"],
            failed_attempts=row["failed_attempts"], locked_until=row["locked_until"],
            last_login=row["last_login"], locale=row["locale"], timezone=row["timezone"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # ── login events (C) ────────────────────────────────────────────

    def record_login(self, ev: LoginEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO enterprise_login_events "
                "(id,user_id,org_id,email,ip,user_agent,result,detail,at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (ev.id, ev.user_id, ev.org_id, ev.email, ev.ip, ev.user_agent,
                 ev.result, ev.detail, ev.at),
            )
            conn.commit()

    def list_login_events(self, user_id: str | None = None, org_id: str | None = None,
                          limit: int = 100) -> list[LoginEvent]:
        sql = "SELECT * FROM enterprise_login_events WHERE 1=1"
        params: list[Any] = []
        if user_id:
            sql += " AND user_id=?"; params.append(user_id)
        if org_id:
            sql += " AND org_id=?"; params.append(org_id)
        sql += " ORDER BY at DESC LIMIT ?"; params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [LoginEvent(**dict(r)) for r in rows]

    def count_recent_failures(self, user_id: str, minutes: int = 15) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM enterprise_login_events "
                "WHERE user_id=? AND result IN ('bad_password','mfa_fail') "
                "AND at > datetime('now', ?)",
                (user_id, f"-{minutes} minutes"),
            ).fetchone()
        return int(row["c"])

    # ── MFA (C) ─────────────────────────────────────────────────────

    def save_mfa_challenge(self, ch: MFAChallenge) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_mfa_challenges "
                "(user_id,org_id,secret,verified,expires_at) VALUES (?,?,?,?,?)",
                (ch.user_id, ch.org_id, ch.secret, 1 if ch.verified else 0, ch.expires_at),
            )
            conn.commit()

    def get_mfa_challenge(self, user_id: str) -> MFAChallenge | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_mfa_challenges WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return MFAChallenge(
            user_id=row["user_id"], org_id=row["org_id"], secret=row["secret"],
            verified=bool(row["verified"]), expires_at=row["expires_at"],
        )

    def delete_mfa_challenge(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM enterprise_mfa_challenges WHERE user_id=?", (user_id,))
            conn.commit()

    # ── budget / quota (F) ──────────────────────────────────────────

    def upsert_budget(self, b: Budget) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_budgets "
                "(id,scope,scope_id,period,amount_usd,alert_threshold,hard_limit,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (b.id, b.scope, b.scope_id, b.period, b.amount_usd,
                 b.alert_threshold, 1 if b.hard_limit else 0, b.created_at),
            )
            conn.commit()

    def get_budget(self, scope: str, scope_id: str) -> Budget | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_budgets WHERE scope=? AND scope_id=?",
                (scope, scope_id),
            ).fetchone()
        if not row:
            return None
        return Budget(
            id=row["id"], scope=row["scope"], scope_id=row["scope_id"],
            period=row["period"], amount_usd=row["amount_usd"],
            alert_threshold=row["alert_threshold"], hard_limit=bool(row["hard_limit"]),
            created_at=row["created_at"],
        )

    def upsert_quota(self, q: Quota) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_quotas "
                "(id,scope,scope_id,tokens_per_day,calls_per_day,storage_mb,concurrent,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (q.id, q.scope, q.scope_id, q.tokens_per_day, q.calls_per_day,
                 q.storage_mb, q.concurrent, q.updated_at),
            )
            conn.commit()

    def get_quota(self, scope: str, scope_id: str) -> Quota | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_quotas WHERE scope=? AND scope_id=?",
                (scope, scope_id),
            ).fetchone()
        if not row:
            return None
        return Quota(
            id=row["id"], scope=row["scope"], scope_id=row["scope_id"],
            tokens_per_day=row["tokens_per_day"], calls_per_day=row["calls_per_day"],
            storage_mb=row["storage_mb"], concurrent=row["concurrent"],
            updated_at=row["updated_at"],
        )

    # ── settings / announcements / maintenance (K) ─────────────────

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_settings (key,value,updated_at) "
                "VALUES (?,?,?)",
                (key, value, _now()),
            )
            conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM enterprise_settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def list_settings(self) -> list[SystemSetting]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM enterprise_settings ORDER BY key").fetchall()
        return [SystemSetting(**dict(r)) for r in rows]

    def create_announcement(self, a: Announcement) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO enterprise_announcements "
                "(id,title,content,level,pinned,active,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a.id, a.title, a.content, a.level, 1 if a.pinned else 0,
                 1 if a.active else 0, a.created_at, a.expires_at),
            )
            conn.commit()

    def list_announcements(self, active_only: bool = True) -> list[Announcement]:
        sql = "SELECT * FROM enterprise_announcements"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY pinned DESC, created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            Announcement(
                id=r["id"], title=r["title"], content=r["content"], level=r["level"],
                pinned=bool(r["pinned"]), active=bool(r["active"]),
                created_at=r["created_at"], expires_at=r["expires_at"],
            )
            for r in rows
        ]

    def update_announcement(self, ann_id: str, active: bool | None = None,
                            pinned: bool | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE enterprise_announcements SET active=COALESCE(?,active), "
                "pinned=COALESCE(?,pinned) WHERE id=?",
                (1 if active is not None else None,
                 1 if pinned is not None else None, ann_id),
            )
            conn.commit()

    def get_maintenance(self) -> MaintenanceState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM enterprise_maintenance WHERE id=1"
            ).fetchone()
        if not row:
            return MaintenanceState()
        return MaintenanceState(
            enabled=bool(row["enabled"]), message=row["message"],
            readonly=bool(row["readonly"]), updated_at=row["updated_at"],
        )

    def set_maintenance(self, state: MaintenanceState) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE enterprise_maintenance SET enabled=?, message=?, "
                "readonly=?, updated_at=? WHERE id=1",
                (1 if state.enabled else 0, state.message,
                 1 if state.readonly else 0, _now()),
            )
            conn.commit()

    # ── API keys (G) ────────────────────────────────────────────────

    def create_api_key(self, key: ApiKeyRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enterprise_api_keys "
                "(id,org_id,label,prefix,scopes,expires_at,last_used_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key.id, key.org_id, key.label, key.prefix,
                 json.dumps(key.scopes), key.expires_at, key.last_used_at, key.created_at),
            )
            conn.commit()

    def list_api_keys(self, org_id: str | None = None) -> list[ApiKeyRecord]:
        sql = "SELECT * FROM enterprise_api_keys"
        params: list[Any] = []
        if org_id:
            sql += " WHERE org_id=?"; params.append(org_id)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            ApiKeyRecord(
                id=r["id"], org_id=r["org_id"], label=r["label"], prefix=r["prefix"],
                scopes=json.loads(r["scopes"] or "[]"), expires_at=r["expires_at"],
                last_used_at=r["last_used_at"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def delete_api_key(self, key_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM enterprise_api_keys WHERE id=?", (key_id,))
            conn.commit()

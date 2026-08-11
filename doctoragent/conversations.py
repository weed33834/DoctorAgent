"""Server-side conversation store (general agent platform feature).

Currently chat sessions live only in the browser (localStorage) — they vanish
across devices and cannot be searched globally. This adds a real, SQLite-backed
conversation store so sessions persist server-side and are manageable:

* create / list (with search) / get / rename / delete conversations
* add messages, record like/dislike feedback
* fork (branch) a conversation for alternative answers

Wired to the REST API at ``/api/v1/conversations/*``.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ConversationStore:
    """SQLite store for conversations, messages and feedback."""

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
                CREATE TABLE IF NOT EXISTS conv_conversations (
                    id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT,
                    meta TEXT
                );
                CREATE TABLE IF NOT EXISTS conv_messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT,
                    content TEXT, ts TEXT, feedback INTEGER DEFAULT 0,
                    feedback_comment TEXT
                );
                CREATE TABLE IF NOT EXISTS conv_shares (
                    token TEXT PRIMARY KEY, conversation_id TEXT,
                    created_at TEXT, expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conv_msgs_conv ON conv_messages(conversation_id);
                """
            )
            conn.commit()

    # ── share links ──────────────────────────────────────────────────

    def share(self, conversation_id: str, ttl_hours: int = 168) -> dict[str, Any] | None:
        """Generate a share token for a conversation (default 7-day TTL)."""
        if self.get(conversation_id) is None:
            return None
        token = uuid.uuid4().hex[:24]
        expires = datetime.now(timezone.utc).timestamp() + ttl_hours * 3600
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conv_shares (token,conversation_id,created_at,expires_at) VALUES (?,?,?,?)",
                (token, conversation_id, _now(), expires),
            )
            conn.commit()
        return {"token": token, "conversation_id": conversation_id,
                "expires_at": expires, "ttl_hours": ttl_hours}

    def get_shared(self, token: str) -> dict[str, Any] | None:
        """Resolve a share token to a conversation (for public view)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT conversation_id, expires_at FROM conv_shares WHERE token=?",
                (token,),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] and datetime.now(timezone.utc).timestamp() > float(row["expires_at"]):
            return None
        return self.get(row["conversation_id"])

    def revoke_share(self, token: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM conv_shares WHERE token=?", (token,))
            conn.commit()
        return cur.rowcount > 0

    # ── auto-title / summary ─────────────────────────────────────────

    @staticmethod
    def auto_title(first_user_message: str, fallback: str = "新对话") -> str:
        """Generate a concise title from the first user message."""
        text = (first_user_message or "").strip()
        if not text:
            return fallback
        text = " ".join(text.split())
        return text[:24] + ("…" if len(text) > 24 else "")

    def summarize(self, conversation_id: str) -> str | None:
        """Heuristic summary from the conversation messages (head + tail)."""
        conv = self.get(conversation_id)
        if not conv:
            return None
        msgs = conv.get("messages", [])
        if not msgs:
            return "（空对话）"
        user_msgs = [m["content"] for m in msgs if m["role"] == "user"][:3]
        last = msgs[-1]["content"]
        return "；".join(u[:40] for u in user_msgs) + " —— 结尾：" + last[:60]

    # ── conversations ────────────────────────────────────────────────

    def create(self, title: str = "新对话", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        now = _now()
        row = {"id": _id("conv"), "title": title or "新对话",
               "created_at": now, "updated_at": now, "meta": meta or {}}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conv_conversations (id,title,created_at,updated_at,meta) VALUES (?,?,?,?,?)",
                (row["id"], row["title"], now, now, _dumps(row["meta"])),
            )
            conn.commit()
        return row

    def list(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM conv_conversations"
        params: list[Any] = []
        if query:
            sql += " WHERE title LIKE ? OR id IN (SELECT conversation_id FROM conv_messages WHERE content LIKE ?)"
            params += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = _loads(d.get("meta"))
            d["message_count"] = self._count_messages(conn, r["id"])
            out.append(d)
        return out

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conv_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                return None
            msgs = conn.execute(
                "SELECT id,role,content,ts,feedback,feedback_comment FROM conv_messages "
                "WHERE conversation_id=? ORDER BY ts ASC, rowid ASC",
                (conversation_id,),
            ).fetchall()
        d = dict(row)
        d["meta"] = _loads(d.get("meta"))
        d["messages"] = [dict(m) for m in msgs]
        return d

    def rename(self, conversation_id: str, title: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE conv_conversations SET title=?, updated_at=? WHERE id=?",
                               (title, _now(), conversation_id))
            conn.commit()
        return cur.rowcount > 0

    def delete(self, conversation_id: str) -> bool:
        with self._connect() as conn:
            c1 = conn.execute("DELETE FROM conv_messages WHERE conversation_id=?", (conversation_id,))
            c2 = conn.execute("DELETE FROM conv_conversations WHERE id=?", (conversation_id,))
            conn.commit()
        return c2.rowcount > 0 or c1.rowcount > 0

    def fork(self, conversation_id: str, new_title: str = "") -> dict[str, Any] | None:
        """Branch a conversation (copy its messages into a new one)."""
        src = self.get(conversation_id)
        if src is None:
            return None
        title = new_title or (src.get("title", "新对话") + " (分叉)")
        conv = self.create(title)
        with self._connect() as conn:
            for m in src.get("messages", []):
                conn.execute(
                    "INSERT INTO conv_messages (id,conversation_id,role,content,ts,feedback,feedback_comment) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (_id("msg"), conv["id"], m["role"], m["content"], m["ts"], 0, ""),
                )
            conn.commit()
        return self.get(conv["id"])

    def _count_messages(self, conn: sqlite3.Connection, cid: str) -> int:
        return conn.execute("SELECT COUNT(*) c FROM conv_messages WHERE conversation_id=?", (cid,)).fetchone()["c"]

    # ── messages & feedback ──────────────────────────────────────────

    def add_message(self, conversation_id: str, role: str, content: str) -> dict[str, Any] | None:
        if not self.get(conversation_id):
            return None
        row = {"id": _id("msg"), "conversation_id": conversation_id, "role": role,
               "content": content, "ts": _now(), "feedback": 0, "feedback_comment": ""}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conv_messages (id,conversation_id,role,content,ts,feedback,feedback_comment) "
                "VALUES (?,?,?,?,?,?,?)",
                (row["id"], conversation_id, role, content, row["ts"], 0, ""),
            )
            conn.execute("UPDATE conv_conversations SET updated_at=? WHERE id=?", (_now(), conversation_id))
            conn.commit()
        return row

    def feedback(self, message_id: str, rating: int, comment: str = "") -> bool:
        """Record like(1)/dislike(-1)/neutral(0) feedback on a message."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE conv_messages SET feedback=?, feedback_comment=? WHERE id=?",
                (int(rating), comment, message_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            convs = conn.execute("SELECT COUNT(*) c FROM conv_conversations").fetchone()["c"]
            msgs = conn.execute("SELECT COUNT(*) c FROM conv_messages").fetchone()["c"]
            likes = conn.execute("SELECT COUNT(*) c FROM conv_messages WHERE feedback=1").fetchone()["c"]
            dislikes = conn.execute("SELECT COUNT(*) c FROM conv_messages WHERE feedback=-1").fetchone()["c"]
        return {"conversations": convs, "messages": msgs,
                "likes": likes, "dislikes": dislikes}


def _dumps(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, ensure_ascii=False, default=str)


def _loads(s: Any) -> Any:
    import json

    try:
        return json.loads(s) if s else {}
    except Exception:  # noqa: BLE001
        return {}

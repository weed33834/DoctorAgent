"""Async SQLAlchemy repository for the conversations subsystem (P2 pilot).

Full behavioural parity with :class:`doctoragent.conversations.ConversationStore`
(same method names, argument shapes, return-dict shapes, tenant-scoping rules)
so ``api/conversation_routes.py`` can serve either backend through a thin
facade without contract drift:

* legacy path  — raw sqlite3 against per-module files (unchanged default)
* repo path    — this class over any SQLAlchemy URL from
  ``DOCTORAGENT_DATABASE_URL`` (sqlite+aiosqlite today, postgres+asyncpg when
  RLS lands in P4)

Divergence note: message ordering uses ``ts ASC, id ASC`` here vs the legacy
``ts ASC, rowid ASC`` (Postgres has no rowid); ids are monotonic enough that
ordering is equivalent in practice.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doctoragent._utils import generate_id, utcnow_iso
from doctoragent.db.engine import create_async_engine_from_url, dialect_of, tenant_scope
from doctoragent.db.models import (
    ConversationORM,
    ConversationsBase,
    ConvMessageORM,
    ConvShareORM,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return utcnow_iso()


def _id(prefix: str) -> str:
    return generate_id(prefix)


def _dumps(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, ensure_ascii=False, default=str)


def _loads(s: Any) -> dict[str, Any]:
    import json

    try:
        return json.loads(s) if s else {}
    except Exception:  # noqa: BLE001
        return {}


class AsyncConversationRepository:
    """Tenant-aware async store mirroring ConversationStore semantics."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.dialect = dialect_of(database_url)
        url = database_url
        if self.dialect == "sqlite":
            # Ensure parent directory exists before aiosqlite opens the file.
            raw_path = database_url.split("///", 1)[-1]
            parent = Path(raw_path).parent
            parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine_from_url(url)
        self._session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self._schema_ready = False

    # ── bootstrap ────────────────────────────────────────────────────

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self.engine.begin() as conn:
            await conn.run_sync(ConversationsBase.metadata.create_all)
        self._schema_ready = True

    async def _session(self) -> AsyncSession:
        await self._ensure_schema()
        return self._session_factory()

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def auto_title(first_user_message: str, fallback: str = "新对话") -> str:
        text = (first_user_message or "").strip()
        if not text:
            return fallback
        text = " ".join(text.split())
        return text[:24] + ("…" if len(text) > 24 else "")

    async def _load_messages(self, session: AsyncSession, cid: str) -> list[dict[str, Any]]:
        rows = (
            (
                await session.execute(
                    select(ConvMessageORM)
                    .where(ConvMessageORM.conversation_id == cid)
                    .order_by(ConvMessageORM.ts.asc(), ConvMessageORM.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": m.id,
                "role": m.role or "",
                "content": m.content or "",
                "ts": m.ts or "",
                "feedback": m.feedback or 0,
                "feedback_comment": m.feedback_comment or "",
            }
            for m in rows
        ]

    @staticmethod
    def _conv_dict(row: ConversationORM, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        d = {
            "id": row.id,
            "title": row.title or "",
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "meta": _loads(row.meta),
            "tenant_id": row.tenant_id,
        }
        if meta is not None:
            d["messages"] = meta
        return d

    # ── conversations ────────────────────────────────────────────────

    async def create(
        self,
        title: str = "新对话",
        meta: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        now = _now()
        conv_id = _id("conv")
        async with await self._session() as session:
            session.add(
                ConversationORM(
                    id=conv_id,
                    title=title or "新对话",
                    created_at=now,
                    updated_at=now,
                    meta=_dumps(meta or {}),
                    tenant_id=tenant_id,
                )
            )
            await session.commit()
        return {
            "id": conv_id,
            "title": title or "新对话",
            "created_at": now,
            "updated_at": now,
            "meta": meta or {},
            "tenant_id": tenant_id,
        }

    async def list(
        self, query: str = "", limit: int = 50, tenant_id: str = "default"
    ) -> list[dict[str, Any]]:
        async with await self._session() as session:
            stmt = select(ConversationORM).where(ConversationORM.tenant_id == tenant_id)
            if query:
                like = f"%{query}%"
                matching_ids = (
                    select(ConvMessageORM.conversation_id)
                    .join(
                        ConversationORM,
                        ConvMessageORM.conversation_id == ConversationORM.id,
                    )
                    .where(
                        ConversationORM.tenant_id == tenant_id,
                        ConvMessageORM.content.like(like),
                    )
                )
                stmt = stmt.where(
                    ConversationORM.title.like(like) | ConversationORM.id.in_(matching_ids)
                )
            stmt = stmt.order_by(ConversationORM.updated_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()

            counts: dict[str, int] = {}
            if rows:
                id_list = [r.id for r in rows]
                count_rows = (
                    await session.execute(
                        select(
                            ConvMessageORM.conversation_id,
                            func.count(ConvMessageORM.id),
                        )
                        .where(ConvMessageORM.conversation_id.in_(id_list))
                        .group_by(ConvMessageORM.conversation_id)
                    )
                ).all()
                counts = {cid: int(n) for cid, n in count_rows}

        out = []
        for r in rows:
            d = self._conv_dict(r)
            d["message_count"] = counts.get(r.id, 0)
            out.append(d)
        return out

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        """Fetch regardless of tenant (ownership enforced by get_for_tenant)."""
        async with await self._session() as session:
            row = (
                await session.execute(
                    select(ConversationORM).where(ConversationORM.id == conversation_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            messages = await self._load_messages(session, conversation_id)
        d = self._conv_dict(row, messages)
        return d

    async def get_for_tenant(
        self, conversation_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        conv = await self.get(conversation_id)
        if conv is None or conv.get("tenant_id", "default") != tenant_id:
            return None
        return conv

    async def rename(
        self, conversation_id: str, title: str, tenant_id: str = "default"
    ) -> bool:
        async with await self._session() as session:
            cur = await session.execute(
                update(ConversationORM)
                .where(
                    ConversationORM.id == conversation_id,
                    ConversationORM.tenant_id == tenant_id,
                )
                .values(title=title, updated_at=_now())
            )
            await session.commit()
            return cur.rowcount > 0

    async def delete(self, conversation_id: str, tenant_id: str = "default") -> bool:
        async with await self._session() as session:
            owned = (
                await session.execute(
                    select(ConversationORM.id).where(
                        ConversationORM.id == conversation_id,
                        ConversationORM.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if owned is None:
                return False
            await session.execute(
                delete(ConvMessageORM).where(
                    ConvMessageORM.conversation_id == conversation_id
                )
            )
            await session.execute(
                delete(ConversationORM).where(ConversationORM.id == conversation_id)
            )
            await session.commit()
            return True

    async def fork(
        self, conversation_id: str, new_title: str = "", tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        src = await self.get_for_tenant(conversation_id, tenant_id)
        if src is None:
            return None
        title = new_title or (src.get("title", "新对话") + " (分叉)")
        created = await self.create(title, tenant_id=tenant_id)
        async with await self._session() as session:
            for m in src.get("messages", []):
                session.add(
                    ConvMessageORM(
                        id=_id("msg"),
                        conversation_id=created["id"],
                        role=m["role"],
                        content=m["content"],
                        ts=m["ts"],
                        feedback=0,
                        feedback_comment="",
                    )
                )
            await session.commit()
        return await self.get(created["id"])

    # ── messages / feedback / share / summary ────────────────────────

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tenant_id: str = "default",
    ) -> dict[str, Any] | None:
        if not await self.get_for_tenant(conversation_id, tenant_id):
            return None
        row = {
            "id": _id("msg"),
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "ts": _now(),
        }
        async with await self._session() as session:
            session.add(
                ConvMessageORM(
                    id=row["id"],
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    ts=row["ts"],
                    feedback=0,
                    feedback_comment="",
                )
            )
            await session.execute(
                update(ConversationORM)
                .where(ConversationORM.id == conversation_id)
                .values(updated_at=_now())
            )
            await session.commit()
        return {**row, "feedback": 0, "feedback_comment": ""}

    async def feedback(self, message_id: str, rating: int, comment: str = "") -> bool:
        async with await self._session() as session:
            cur = await session.execute(
                update(ConvMessageORM)
                .where(ConvMessageORM.id == message_id)
                .values(feedback=int(rating), feedback_comment=comment)
            )
            await session.commit()
            return cur.rowcount > 0

    async def share(
        self, conversation_id: str, ttl_hours: int = 168, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        if not await self.get_for_tenant(conversation_id, tenant_id):
            return None
        token = uuid.uuid4().hex[:24]
        expires = datetime.now(timezone.utc).timestamp() + ttl_hours * 3600
        async with await self._session() as session:
            session.add(
                ConvShareORM(
                    token=token,
                    conversation_id=conversation_id,
                    created_at=_now(),
                    expires_at=expires,
                )
            )
            await session.commit()
        return {
            "token": token,
            "conversation_id": conversation_id,
            "expires_at": expires,
            "ttl_hours": ttl_hours,
        }

    async def revoke_share(self, token: str) -> bool:
        async with await self._session() as session:
            cur = await session.execute(
                delete(ConvShareORM).where(ConvShareORM.token == token)
            )
            await session.commit()
            return cur.rowcount > 0

    async def get_shared(self, token: str) -> dict[str, Any] | None:
        """Resolve a share token to a conversation (public, cross-tenant by design)."""
        async with await self._session() as session:
            row = (
                await session.execute(
                    select(ConvShareORM).where(ConvShareORM.token == token)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at and datetime.now(timezone.utc).timestamp() > float(
            row.expires_at
        ):
            return None
        return await self.get(row.conversation_id or "")

    async def summarize(self, conversation_id: str, tenant_id: str = "default") -> str | None:
        conv = await self.get_for_tenant(conversation_id, tenant_id)
        if not conv:
            return None
        msgs = conv.get("messages", [])
        if not msgs:
            return "（空对话）"
        user_msgs = [m["content"] for m in msgs if m["role"] == "user"][:3]
        last = msgs[-1]["content"]
        return "；".join(u[:40] for u in user_msgs) + " —— 结尾：" + last[:60]

    async def stats(self) -> dict[str, Any]:
        async with await self._session() as session:
            convs = (
                await session.execute(select(func.count()).select_from(ConversationORM))
            ).scalar_one()
            msgs = (
                await session.execute(select(func.count()).select_from(ConvMessageORM))
            ).scalar_one()
            likes = (
                await session.execute(
                    select(func.count())
                    .select_from(ConvMessageORM)
                    .where(ConvMessageORM.feedback == 1)
                )
            ).scalar_one()
            dislikes = (
                await session.execute(
                    select(func.count())
                    .select_from(ConvMessageORM)
                    .where(ConvMessageORM.feedback == -1)
                )
            ).scalar_one()
        return {
            "conversations": int(convs),
            "messages": int(msgs),
            "likes": int(likes),
            "dislikes": int(dislikes),
        }

    # ── RLS hook (P4): on Postgres every session binds the GUC ───────
    async def scoped_session(self, tenant_id: str) -> AsyncSession:
        """Open a session bound to *tenant_id* via the RLS GUC (postgres only).

        SQLite ignores the scope; explicit WHERE clauses remain authoritative.
        """
        session = await self._session()
        if self.dialect == "postgres":
            await tenant_scope(session, tenant_id, self.dialect).__aenter__()
        return session


# re-exported for facade typing convenience
_ = time  # keep import surface stable for future expiry helpers

"""Unified async facade over the two conversation backends (P2 dual-run).

* ``database_url`` empty → wraps the legacy :class:`ConversationStore`
  (raw sqlite3, per-module file). Calls execute inline exactly as before.
* ``database_url`` set   → wraps :class:`AsyncConversationRepository`
  (SQLAlchemy async; Postgres-ready).

Method names/signatures match what ``api/conversation_routes.py`` already
calls, so handlers only change from ``store.x(...)`` to
``await backend.x(...)``.
"""

from __future__ import annotations

from typing import Any

from doctoragent.conversations import ConversationStore
from doctoragent.db.repositories import AsyncConversationRepository


class ConversationFacade:
    """Dispatch conversation operations to the active backend."""

    def __init__(
        self,
        legacy: ConversationStore | None = None,
        repo: AsyncConversationRepository | None = None,
    ) -> None:
        if legacy is None and repo is None:
            raise ValueError("ConversationFacade requires a legacy store or a repo")
        self.legacy = legacy
        self.repo = repo

    @staticmethod
    def auto_title(first_user_message: str, fallback: str = "新对话") -> str:
        return ConversationStore.auto_title(first_user_message, fallback)

    # ── conversations ────────────────────────────────────────────────

    async def create(
        self,
        title: str = "新对话",
        meta: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        if self.repo is not None:
            return await self.repo.create(title, meta, tenant_id)
        assert self.legacy is not None
        return self.legacy.create(title, meta, tenant_id)

    async def list_conversations(
        self, query: str = "", limit: int = 50, tenant_id: str = "default"
    ) -> list[dict[str, Any]]:
        if self.repo is not None:
            return await self.repo.list(query, limit, tenant_id)
        assert self.legacy is not None
        return self.legacy.list(query, limit, tenant_id)

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        if self.repo is not None:
            return await self.repo.get(conversation_id)
        assert self.legacy is not None
        return self.legacy.get(conversation_id)

    async def get_for_tenant(
        self, conversation_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        if self.repo is not None:
            return await self.repo.get_for_tenant(conversation_id, tenant_id)
        assert self.legacy is not None
        return self.legacy.get_for_tenant(conversation_id, tenant_id)

    async def rename(self, cid: str, title: str, tenant_id: str = "default") -> bool:
        if self.repo is not None:
            return await self.repo.rename(cid, title, tenant_id)
        assert self.legacy is not None
        return self.legacy.rename(cid, title, tenant_id)

    async def delete(self, cid: str, tenant_id: str = "default") -> bool:
        if self.repo is not None:
            return await self.repo.delete(cid, tenant_id)
        assert self.legacy is not None
        return self.legacy.delete(cid, tenant_id)

    async def fork(
        self, cid: str, new_title: str = "", tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        if self.repo is not None:
            return await self.repo.fork(cid, new_title, tenant_id)
        assert self.legacy is not None
        return self.legacy.fork(cid, new_title, tenant_id)

    # ── messages / feedback / share / summary ────────────────────────

    async def add_message(
        self,
        cid: str,
        role: str,
        content: str,
        tenant_id: str = "default",
    ) -> dict[str, Any] | None:
        if self.repo is not None:
            return await self.repo.add_message(cid, role, content, tenant_id)
        assert self.legacy is not None
        return self.legacy.add_message(cid, role, content, tenant_id)

    async def feedback(self, message_id: str, rating: int, comment: str = "") -> bool:
        if self.repo is not None:
            return await self.repo.feedback(message_id, rating, comment)
        assert self.legacy is not None
        return self.legacy.feedback(message_id, rating, comment)

    async def share(
        self, cid: str, ttl_hours: int = 168, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        if self.repo is not None:
            return await self.repo.share(cid, ttl_hours, tenant_id)
        assert self.legacy is not None
        return self.legacy.share(cid, ttl_hours, tenant_id)

    async def revoke_share(self, token: str) -> bool:
        if self.repo is not None:
            return await self.repo.revoke_share(token)
        assert self.legacy is not None
        return self.legacy.revoke_share(token)

    async def get_shared(self, token: str) -> dict[str, Any] | None:
        if self.repo is not None:
            return await self.repo.get_shared(token)
        assert self.legacy is not None
        return self.legacy.get_shared(token)

    async def summarize(self, cid: str, tenant_id: str = "default") -> str | None:
        if self.repo is not None:
            return await self.repo.summarize(cid, tenant_id)
        assert self.legacy is not None
        return self.legacy.summarize(cid, tenant_id)

    async def stats(self) -> dict[str, Any]:
        if self.repo is not None:
            return await self.repo.stats()
        assert self.legacy is not None
        return self.legacy.stats()

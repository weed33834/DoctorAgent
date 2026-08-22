"""End-to-end tests: AsyncConversationRepository over aiosqlite (P2 pilot).

Runs the real SQLAlchemy async stack against an on-disk SQLite database —
no mocks. Mirrors the tenant-isolation semantics pinned by
``tests/test_conversation_tenants.py`` (v0.3.10) plus facade dual-run
routing, so the Postgres cutover later reuses these exact contracts.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

import asyncio  # noqa: E402

from doctoragent.api.conversation_facade import ConversationFacade  # noqa: E402
from doctoragent.conversations import ConversationStore  # noqa: E402
from doctoragent.db.engine import resolve_database_url  # noqa: E402
from doctoragent.db.repositories import AsyncConversationRepository  # noqa: E402


def _url(tmp_path: Path) -> str:
    return "sqlite+aiosqlite:///" + str(tmp_path / "conv.db")


@pytest.fixture
def repo(tmp_path: Path) -> AsyncConversationRepository:
    return AsyncConversationRepository(_url(tmp_path))


class TestRepositoryParity:
    """v0.3.10 semantics, replayed against the async ORM path."""

    @pytest.mark.asyncio
    async def test_create_scopes_tenant(self, repo: AsyncConversationRepository) -> None:
        conv = await repo.create("t1", tenant_id="hospital_a")
        assert conv["tenant_id"] == "hospital_a"
        assert conv["id"].startswith("conv-")

    @pytest.mark.asyncio
    async def test_list_filters_and_orders(
        self, repo: AsyncConversationRepository
    ) -> None:
        await repo.create("a1", tenant_id="hospital_a")
        await asyncio.sleep(0.01)
        await repo.create("a2", tenant_id="hospital_a")
        await repo.create("b1", tenant_id="hospital_b")
        items = await repo.list(tenant_id="hospital_a")
        assert [c["title"] for c in items] == ["a2", "a1"]  # newest first
        assert all(c["tenant_id"] == "hospital_a" for c in items)
        assert all("message_count" in c for c in items)

    @pytest.mark.asyncio
    async def test_get_for_tenant_hides_foreign(
        self, repo: AsyncConversationRepository
    ) -> None:
        conv = await repo.create("secret", tenant_id="hospital_a")
        assert await repo.get_for_tenant(conv["id"], "hospital_b") is None
        got = await repo.get_for_tenant(conv["id"], "hospital_a")
        assert got is not None and got["title"] == "secret"

    @pytest.mark.asyncio
    async def test_rename_delete_blocked_cross_tenant(
        self, repo: AsyncConversationRepository
    ) -> None:
        conv = await repo.create("orig", tenant_id="hospital_a")
        assert not await repo.rename(conv["id"], "hacked", tenant_id="hospital_b")
        assert not await repo.delete(conv["id"], tenant_id="hospital_b")
        assert (await repo.get(conv["id"]))["title"] == "orig"
        assert await repo.delete(conv["id"], tenant_id="hospital_a") is True

    @pytest.mark.asyncio
    async def test_add_message_and_feedback(
        self, repo: AsyncConversationRepository
    ) -> None:
        conv = await repo.create("chat", tenant_id="hospital_a")
        msg = await repo.add_message(
            conv["id"], "user", "华法林剂量?", tenant_id="hospital_a"
        )
        assert msg is not None and msg["feedback"] == 0
        # Foreign tenant cannot append into someone else's conversation.
        assert (
            await repo.add_message(conv["id"], "user", "leak", tenant_id="hospital_b")
            is None
        )
        assert await repo.feedback(msg["id"], 1, "helpful") is True
        full = await repo.get(conv["id"])
        assert full["messages"][0]["feedback"] == 1

    @pytest.mark.asyncio
    async def test_fork_copies_within_tenant(
        self, repo: AsyncConversationRepository
    ) -> None:
        conv = await repo.create("src", tenant_id="hospital_a")
        await repo.add_message(conv["id"], "user", "hello", tenant_id="hospital_a")
        assert await repo.fork(conv["id"], tenant_id="hospital_b") is None
        branched = await repo.fork(conv["id"], tenant_id="hospital_a")
        assert branched is not None
        assert branched["title"] == "src (分叉)"
        assert [m["content"] for m in branched["messages"]] == ["hello"]

    @pytest.mark.asyncio
    async def test_share_ownership_and_public_read(
        self, repo: AsyncConversationRepository
    ) -> None:
        conv = await repo.create("private", tenant_id="hospital_a")
        assert await repo.share(conv["id"], tenant_id="hospital_b") is None
        share = await repo.share(conv["id"], ttl_hours=1, tenant_id="hospital_a")
        assert share is not None
        shared = await repo.get_shared(share["token"])
        assert shared is not None and shared["id"] == conv["id"]
        # Revoke kills public access.
        assert await repo.revoke_share(share["token"]) is True
        assert await repo.get_shared(share["token"]) is None

    @pytest.mark.asyncio
    async def test_search_respects_tenant(
        self, repo: AsyncConversationRepository
    ) -> None:
        a = await repo.create("warfarin hospital A", tenant_id="hospital_a")
        await repo.create("warfarin hospital B", tenant_id="hospital_b")
        await repo.add_message(a["id"], "user", "INR target details", tenant_id="hospital_a")
        hits = await repo.list("warfarin", tenant_id="hospital_a")
        assert len(hits) == 1
        hits_msg = await repo.list("INR target", tenant_id="hospital_a")
        assert len(hits_msg) == 1  # content match inside same-tenant messages

    @pytest.mark.asyncio
    async def test_summarize_and_stats(self, repo: AsyncConversationRepository) -> None:
        conv = await repo.create("sum", tenant_id="hospital_a")
        await repo.add_message(conv["id"], "user", "问题一", tenant_id="hospital_a")
        summary = await repo.summarize(conv["id"], tenant_id="hospital_a")
        assert summary and "问题一" in summary
        assert await repo.summarize(conv["id"], tenant_id="hospital_b") is None
        stats = await repo.stats()
        assert stats["conversations"] == 1 and stats["messages"] == 1

    def test_auto_title_matches_legacy(self) -> None:
        long = "很长的临床问题描述需要被截断处理示例文本"
        assert AsyncConversationRepository.auto_title(long) == (
            ConversationStore.auto_title(long)
        )


class TestFacadeDualRun:
    @pytest.mark.asyncio
    async def test_repo_mode_dispatches_async(
        self, tmp_path: Path, repo: AsyncConversationRepository
    ) -> None:
        facade = ConversationFacade(repo=repo)
        conv = await facade.create("via-repo", tenant_id="hospital_a")
        got = await facade.get_for_tenant(conv["id"], "hospital_a")
        assert got is not None and got["title"] == "via-repo"

    @pytest.mark.asyncio
    async def test_legacy_mode_wraps_store(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "legacy.db")
        facade = ConversationFacade(legacy=store)
        conv = await facade.create("legacy", tenant_id="default")
        assert conv["id"].startswith("conv-")
        got = await facade.get_for_tenant(conv["id"], "default")
        assert got is not None

    def test_requires_one_backend(self) -> None:
        with pytest.raises(ValueError):
            ConversationFacade()

    def test_resolve_url_prefers_configured(self, tmp_path: Path) -> None:
        cfg = AegisConfigLike()
        assert resolve_database_url(cfg).startswith("sqlite+aiosqlite")


class AegisConfigLike:
    """Minimal stand-in exposing only what resolve_database_url reads."""

    class _Paths:
        index = Path(tempfile.mkdtemp(prefix="dblayer"))

    paths = _Paths()
    database_url = ""

"""Tests: conversation store tenant isolation (v0.3.10).

``ConversationStore`` previously had zero tenant awareness — every tenant's
conversations lived in one flat namespace, so any caller of the
``/api/v1/conversations/*`` routes could read, fork, share or delete any
other tenant's clinical conversations. The store now scopes every mutating
and listing operation to a ``tenant_id`` (defaulting to ``"default"`` for
backwards compatibility), and the API layer resolves the scope from the
authenticated identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doctoragent.conversations import ConversationStore


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations.db")


class TestTenantIsolation:
    """Cross-tenant operations must be indistinguishable from missing data."""

    def test_create_scopes_tenant(self, store: ConversationStore) -> None:
        conv = store.create("t1-conv", tenant_id="hospital_a")
        assert conv["tenant_id"] == "hospital_a"

    def test_list_filters_by_tenant(self, store: ConversationStore) -> None:
        store.create("a-conv", tenant_id="hospital_a")
        store.create("b-conv", tenant_id="hospital_b")
        a_items = store.list(tenant_id="hospital_a")
        b_items = store.list(tenant_id="hospital_b")
        assert [c["title"] for c in a_items] == ["a-conv"]
        assert [c["title"] for c in b_items] == ["b-conv"]

    def test_get_for_tenant_hides_foreign(self, store: ConversationStore) -> None:
        conv = store.create("secret", tenant_id="hospital_a")
        assert store.get_for_tenant(conv["id"], "hospital_b") is None
        assert store.get_for_tenant(conv["id"], "hospital_a") is not None

    def test_rename_blocked_cross_tenant(self, store: ConversationStore) -> None:
        conv = store.create("orig", tenant_id="hospital_a")
        assert store.rename(conv["id"], "hacked", tenant_id="hospital_b") is False
        assert store.get(conv["id"])["title"] == "orig"

    def test_delete_blocked_cross_tenant(self, store: ConversationStore) -> None:
        conv = store.create("keep-me", tenant_id="hospital_a")
        assert store.delete(conv["id"], tenant_id="hospital_b") is False
        assert store.get(conv["id"]) is not None
        assert store.delete(conv["id"], tenant_id="hospital_a") is True

    def test_fork_stays_in_own_tenant(self, store: ConversationStore) -> None:
        conv = store.create("src", tenant_id="hospital_a")
        assert store.fork(conv["id"], tenant_id="hospital_b") is None
        branched = store.fork(conv["id"], tenant_id="hospital_a")
        assert branched is not None
        assert branched["tenant_id"] == "hospital_a"
        # The original never appears in hospital_b's listing.
        titles = [c["title"] for c in store.list(tenant_id="hospital_b")]
        assert "src" not in titles

    def test_share_blocked_cross_tenant(self, store: ConversationStore) -> None:
        conv = store.create("private", tenant_id="hospital_a")
        assert store.share(conv["id"], tenant_id="hospital_b") is None
        share = store.share(conv["id"], tenant_id="hospital_a")
        assert share is not None
        # Share links remain publicly resolvable by token (by design).
        assert store.get_shared(share["token"]) is not None

    def test_add_message_blocked_cross_tenant(self, store: ConversationStore) -> None:
        conv = store.create("chat", tenant_id="hospital_a")
        assert (
            store.add_message(conv["id"], "user", "leak", tenant_id="hospital_b")
            is None
        )
        assert store.add_message(conv["id"], "user", "ok", tenant_id="hospital_a")

    def test_legacy_rows_default_tenant(self, tmp_path: Path) -> None:
        """A pre-isolation DB migrates cleanly: existing rows stay visible."""
        import sqlite3

        db = tmp_path / "legacy.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """
                CREATE TABLE conv_conversations (
                    id TEXT PRIMARY KEY, title TEXT, created_at TEXT,
                    updated_at TEXT, meta TEXT
                );
                INSERT INTO conv_conversations VALUES
                    ('conv_x', 'old', '2026-01-01', '2026-01-01', '{}');
                CREATE TABLE conv_messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT,
                    content TEXT, ts TEXT, feedback INTEGER DEFAULT 0,
                    feedback_comment TEXT
                );
                CREATE TABLE conv_shares (
                    token TEXT PRIMARY KEY, conversation_id TEXT,
                    created_at TEXT, expires_at TEXT
                );
                """
            )
        store = ConversationStore(db)
        items = store.list()  # default tenant sees migrated rows
        assert [c["id"] for c in items] == ["conv_x"]

    def test_search_respects_tenant(self, store: ConversationStore) -> None:
        a = store.create("warfarin discussion", tenant_id="hospital_a")
        store.create("warfarin other-hospital", tenant_id="hospital_b")
        hits = store.list("warfarin", tenant_id="hospital_a")
        assert [c["id"] for c in hits] == [a["id"]]

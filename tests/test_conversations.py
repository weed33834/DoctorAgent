# mypy: ignore-errors
"""Tests for server-side conversation store."""

from __future__ import annotations

from pathlib import Path

from doctoragent.conversations import ConversationStore


def _store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "conv.db")


def test_create_and_get(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("测试会话")
    got = s.get(c["id"])
    assert got["title"] == "测试会话"
    assert got["messages"] == []


def test_add_message_and_list(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("心衰")
    s.add_message(c["id"], "user", "华法林怎么用")
    s.add_message(c["id"], "assistant", "监测INR")
    # search by content
    hits = s.list("华法林")
    assert len(hits) == 1
    assert hits[0]["message_count"] == 2
    got = s.get(c["id"])
    assert len(got["messages"]) == 2
    assert got["messages"][0]["role"] == "user"


def test_rename_and_delete(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("旧")
    assert s.rename(c["id"], "新标题")
    assert s.get(c["id"])["title"] == "新标题"
    assert s.delete(c["id"]) is True
    assert s.get(c["id"]) is None


def test_feedback_and_fork(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("对话")
    s.add_message(c["id"], "user", "问题")
    s.add_message(c["id"], "assistant", "回答")
    msgs = s.get(c["id"])["messages"]
    aid = msgs[1]["id"]
    assert s.feedback(aid, 1, "很好") is True
    f = s.fork(c["id"], "分叉版")
    assert f is not None
    assert f["title"] == "分叉版"
    assert len(f["messages"]) == 2
    assert f["messages"][1]["feedback"] == 0  # fork does not copy feedback


def test_stats(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("a")
    s.add_message(c["id"], "user", "x")
    s.add_message(c["id"], "assistant", "y")
    m = s.get(c["id"])["messages"][1]["id"]
    s.feedback(m, -1)
    st = s.stats()
    assert st["conversations"] == 1
    assert st["messages"] == 2
    assert st["dislikes"] == 1


def test_auto_title() -> None:
    long_msg = "华法林和布洛芬能一起吃吗？这个药怎么用需要注意哪些副作用和出血风险"
    t = ConversationStore.auto_title(long_msg)
    assert t.endswith("…") and len(t) <= 25
    assert ConversationStore.auto_title("", fallback="默认") == "默认"
    assert ConversationStore.auto_title("简短问题") == "简短问题"


def test_share_and_get_shared(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("会话")
    s.add_message(c["id"], "user", "你好")
    share = s.share(c["id"], ttl_hours=24)
    assert share is not None and share["token"]
    pub = s.get_shared(share["token"])
    assert pub is not None and pub["id"] == c["id"]
    assert s.revoke_share(share["token"]) is True
    assert s.get_shared(share["token"]) is None


def test_summarize(tmp_path: Path) -> None:
    s = _store(tmp_path)
    c = s.create("t")
    s.add_message(c["id"], "user", "第一个问题")
    s.add_message(c["id"], "assistant", "第一个回答")
    s.add_message(c["id"], "user", "第二个问题")
    sm = s.summarize(c["id"])
    assert "第一个问题" in sm and "结尾" in sm

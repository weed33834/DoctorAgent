# mypy: ignore-errors
"""Tests for the M0-M13 long-tail tools: browser automation, adapters, group chat."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from doctoragent.agent.adapters import BuiltinRuntimeAdapter, create_adapter
from doctoragent.orchestration.group_chat import (
    GroupChatAgent,
    GroupChatManager,
    build_group_chat_agents,
)
from doctoragent.tools.browser_tool import BrowserTool


# ── adapters (M3.20) ──────────────────────────────────────────────────


def test_builtin_adapter_sync() -> None:
    a = BuiltinRuntimeAdapter(lambda text: "echo:" + text)
    import asyncio

    assert asyncio.run(a.run([{"role": "user", "content": "hi"}]) ) == "echo:hi"


def test_builtin_adapter_async() -> None:
    async def fn(text: str) -> str:
        return "async:" + text

    a = BuiltinRuntimeAdapter(fn)
    import asyncio

    assert asyncio.run(a.run([{"role": "user", "content": "x"}]) ) == "async:x"


def test_create_adapter_fallback() -> None:
    a = create_adapter("langgraph", lambda t: "ok")
    assert isinstance(a, BuiltinRuntimeAdapter)
    assert a.name == "builtin"


def test_create_adapter_openai_and_claude() -> None:
    assert create_adapter("openai_agents").name == "openai_agents"
    assert create_adapter("claude_sdk").name == "claude_sdk"


# ── group chat (M6.4) ─────────────────────────────────────────────────


def _speaker(name: str) -> Any:
    def fn(prompt: str, context: dict[str, Any]) -> str:
        return f"{name} 回应: {prompt[-10:]}"
    return fn


def test_group_chat_runs_turns() -> None:
    mgr = GroupChatManager(max_turns=6)
    agents = [
        GroupChatAgent("医生", "expert", _speaker("医生")),
        GroupChatAgent("药师", "reviewer", _speaker("药师")),
    ]
    import asyncio

    result = asyncio.run(mgr.run("用药方案讨论", agents))
    assert result["turns"] == 6
    assert result["transcript"][0]["speaker"] == "医生"
    assert result["transcript"][1]["speaker"] == "药师"


def test_group_chat_stop_prefix() -> None:
    def fn(prompt: str, context: dict[str, Any]) -> str:
        return "[STOP] 结论"

    mgr = GroupChatManager(max_turns=10)
    import asyncio

    result = asyncio.run(
        mgr.run("t", [GroupChatAgent("a", "r", fn), GroupChatAgent("b", "r", _speaker("b"))])
    )
    assert result["stopped"] is True
    assert result["turns"] == 1


def test_build_group_chat_agents() -> None:
    agents = build_group_chat_agents(
        [{"name": "A", "role": "expert"}, {"name": "B", "role": "reviewer"}],
        _speaker("x"),
    )
    assert [a.name for a in agents] == ["A", "B"]


# ── browser tool (M4.8 / M12.10) ──────────────────────────────────────


def test_browser_tool_unavailable_without_playwright() -> None:
    tool = BrowserTool()
    with patch("doctoragent.tools.browser_tool._playwright_available", return_value=False):
        import asyncio

        res = asyncio.run(tool.execute(action="navigate", url="http://x"))
        assert res.success is False
        assert "Playwright" in (res.error or "")


def test_browser_tool_definition() -> None:
    d = BrowserTool().definition
    assert d.name == "browser_action"
    assert d.category == "browser"


@pytest.mark.asyncio
async def test_browser_tool_dispatch_exception_is_failure() -> None:
    tool = BrowserTool()
    with patch("doctoragent.tools.browser_tool._playwright_available", return_value=True):
        with patch.object(tool, "_dispatch", side_effect=RuntimeError("browser crashed")):
            res = await tool.execute(action="navigate", url="http://x")
    assert res.success is False
    assert "browser crashed" in (res.error or "")

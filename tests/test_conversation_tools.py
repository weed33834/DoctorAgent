# mypy: ignore-errors
"""Tests for conversation-driven clinical management tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from doctoragent.model.tools import ToolRegistry
from doctoragent.tools.conversation_tools import register_conversation_tools
from doctoragent.workspace_config import WorkspaceConfig


class _State:
    def __init__(self, ws: WorkspaceConfig) -> None:
        self.workspace_config = ws
        self.clinical_role = "general"
        self.pricing = None
        self.cost_tracker = None
        self.config = None


def _make_registry(ws: WorkspaceConfig) -> ToolRegistry:
    reg = ToolRegistry()
    register_conversation_tools(reg, _State(ws))
    return reg


def test_conversation_tools_registered() -> None:
    with TemporaryDirectory() as d:
        ws = WorkspaceConfig(Path(d) / "w.db")
        reg = _make_registry(ws)
        for n in ("switch_role", "system_status", "create_knowledge_base",
                  "list_knowledge_bases", "import_document"):
            assert reg.get(n) is not None, f"missing tool {n}"


def test_switch_role() -> None:
    with TemporaryDirectory() as d:
        ws = WorkspaceConfig(Path(d) / "w.db")
        reg = _make_registry(ws)
        r = asyncio.run(reg.get("switch_role").execute(code="cardiology"))
        assert r.success
        assert r.data["role"] == "cardiology"
        assert r.data["name"] == "心内科医生"
        assert ws.get_setting("clinical_role") == "cardiology"
        r2 = asyncio.run(reg.get("switch_role").execute(code="nope"))
        assert r2.success is False


def test_system_status() -> None:
    with TemporaryDirectory() as d:
        ws = WorkspaceConfig(Path(d) / "w.db")
        reg = _make_registry(ws)
        r = asyncio.run(reg.get("system_status").execute())
        assert r.success
        assert r.data["role"]["code"] == "general"
        assert r.data["builtin_knowledge_docs"] >= 10

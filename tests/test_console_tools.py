# mypy: ignore-errors
"""Tests for console conversation tools (do everything via chat)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from doctoragent.model.tools import ToolRegistry
from doctoragent.tools.console_tools import register_console_tools


class _State:
    def __init__(self) -> None:
        self.workspace_config = None
        self.pricing = None
        self.cost_tracker = None
        self.enterprise_service = None
        self.threat_service = None
        self.task_center = None
        self.kb_manager = None
        self.config = None


class _Agent:
    memory = None
    connection_manager = None
    task_store = None


def _reg() -> ToolRegistry:
    reg = ToolRegistry()
    register_console_tools(reg, _State(), _Agent())
    return reg


def test_console_tools_registered() -> None:
    reg = _reg()
    expected = {
        "list_documents", "search_vault", "list_models", "compare_models",
        "cost_report", "config_view", "config_set", "list_connections",
        "enterprise_summary", "list_users", "create_user", "list_api_keys",
        "memory_view", "memory_clear", "security_status", "run_redteam",
        "health_status", "seed_knowledge", "knowledge_list", "task_list",
    }
    present = {t.name for t in reg.list_tools()}
    assert expected.issubset(present), f"missing: {expected - present}"


def test_health_and_knowledge() -> None:
    reg = _reg()
    h = asyncio.run(reg.get("health_status").execute())
    assert h.success and h.data["status"] == "ok"
    k = asyncio.run(reg.get("knowledge_list").execute())
    assert k.success and len(k.data["topics"]) >= 10


def test_console_tools_graceful_when_services_absent() -> None:
    """Unconfigured services must return a graceful error, not crash."""
    reg = _reg()
    for name in ("cost_report", "enterprise_summary", "security_status", "task_list"):
        r = asyncio.run(reg.get(name).execute())
        # either success (with empty/graceful data) or a clean error
        assert r.success or (r.error and isinstance(r.error, str))

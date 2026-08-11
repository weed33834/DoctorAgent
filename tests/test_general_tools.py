# mypy: ignore-errors
"""Tests for general-purpose agent tools (web/time/calc)."""

from __future__ import annotations

import asyncio

from doctoragent.model.tools import ToolRegistry
from doctoragent.tools.general_tools import register_general_tools


def _reg() -> ToolRegistry:
    r = ToolRegistry()
    register_general_tools(r)
    return r


def test_registered() -> None:
    names = {t.name for t in _reg().list_tools()}
    assert {"web_search", "web_fetch", "current_time", "calculate"}.issubset(names)


def test_current_time() -> None:
    r = asyncio.run(_reg().get("current_time").execute())
    assert r.success and r.data["date"]
    assert len(r.data["date"]) == 10


def test_calculate_safe() -> None:
    r = asyncio.run(_reg().get("calculate").execute(expression="(70*1.5)/100"))
    assert r.success and r.data["result"] == 1.05
    # malicious / unsupported input must be rejected
    bad = asyncio.run(_reg().get("calculate").execute(expression="__import__('os')"))
    assert bad.success is False


def test_web_fetch_example() -> None:
    r = asyncio.run(_reg().get("web_fetch").execute(url="https://example.com"))
    assert r.success
    assert "Example" in (r.data.get("title") or "")


def test_web_fetch_rejects_bad_url() -> None:
    r = asyncio.run(_reg().get("web_fetch").execute(url="javascript:alert(1)"))
    assert r.success is False

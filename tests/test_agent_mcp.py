# mypy: ignore-errors
"""Tests for the MCP server bridge (build_mcp_server / run_mcp_server)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.agent.mcp_server import _convert_tool_to_mcp, build_mcp_server
from doctoragent.model.tools import ToolDefinition, ToolParameter, ToolRegistry, ToolResult


def _make_tool_registry() -> ToolRegistry:
    """Build a ToolRegistry with a couple of dummy tools for tests."""
    registry = ToolRegistry()

    class _DummyTool:
        def __init__(self, name: str, desc: str, category: str = "general") -> None:
            self._def = ToolDefinition(
                name=name,
                description=desc,
                parameters=[
                    ToolParameter(name="query", type="string", description="a query"),
                    ToolParameter(
                        name="limit", type="integer", description="max", required=False
                    ),
                ],
                category=category,
            )

        @property
        def definition(self) -> ToolDefinition:
            return self._def

        async def execute(self, **kwargs: Any) -> ToolResult:
            return ToolResult(success=True, data=kwargs, tool_name=self._def.name)

    from doctoragent.model.tools import Tool

    class _SearchTool(Tool):
        @property
        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                name="search_documents",
                description="Search for documents in the vault.",
                parameters=[
                    ToolParameter(
                        name="query", type="string", description="Natural language query"
                    ),
                    ToolParameter(
                        name="top_k", type="integer", description="results",
                        required=False, default=5,
                    ),
                ],
                category="retrieval",
            )

        async def execute(self, query: str, top_k: int = 5) -> ToolResult:
            return ToolResult(
                success=True, data={"query": query, "top_k": top_k},
                tool_name="search_documents",
            )

    class _MemoryTool(Tool):
        @property
        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                name="memory",
                description="Store or recall information from long-term memory.",
                parameters=[
                    ToolParameter(
                        name="action", type="string", description="store/recall",
                        enum=["store", "recall"],
                    ),
                    ToolParameter(
                        name="content", type="string", description="content",
                        required=False,
                    ),
                ],
                category="memory",
            )

        async def execute(self, action: str, content: str | None = None) -> ToolResult:
            return ToolResult(
                success=True, data={"action": action, "content": content},
                tool_name="memory",
            )

    registry.register(_SearchTool())
    registry.register(_MemoryTool())
    return registry


def _make_agent(registry: ToolRegistry | None = None) -> Any:
    """Build a mock agent exposing a tool_registry."""
    agent = MagicMock()
    agent.tool_registry = registry or _make_tool_registry()
    return agent


class TestConvertToolToMcp:
    """_convert_tool_to_mcp schema translation."""

    def test_basic_conversion(self):
        td = ToolDefinition(
            name="search_documents",
            description="Search documents.",
            parameters=[
                ToolParameter(name="query", type="string", description="query"),
                ToolParameter(
                    name="limit", type="integer", description="max", required=False
                ),
            ],
        )
        spec = _convert_tool_to_mcp(td)
        assert spec["name"] == "search_documents"
        assert spec["description"] == "Search documents."
        assert spec["inputSchema"]["type"] == "object"
        assert "query" in spec["inputSchema"]["properties"]
        assert spec["inputSchema"]["required"] == ["query"]

    def test_enum_preserved(self):
        td = ToolDefinition(
            name="memory",
            description="memory tool",
            parameters=[
                ToolParameter(
                    name="action", type="string", description="act",
                    enum=["store", "recall"],
                ),
            ],
        )
        spec = _convert_tool_to_mcp(td)
        assert spec["inputSchema"]["properties"]["action"]["enum"] == ["store", "recall"]


class TestBuildMcpServerUnavailable:
    """When the mcp package is not importable, build_mcp_server raises."""

    def test_raises_import_error(self, monkeypatch: pytest.MonkeyPatch):
        # Force `from mcp import types; from mcp.server import Server` to fail.
        monkeypatch.setitem(sys.modules, "mcp", None)
        monkeypatch.setitem(sys.modules, "mcp.server", None)
        monkeypatch.setitem(sys.modules, "mcp.types", None)
        agent = _make_agent()
        with pytest.raises(ImportError, match="pip install mcp"):
            build_mcp_server(agent)

    def test_raises_when_no_tool_registry(self):
        agent = MagicMock()
        del agent.tool_registry
        # MagicMock returns a new MagicMock for .tools; force it to None.
        agent.tools = None
        with pytest.raises(ValueError, match="no tool_registry"):
            build_mcp_server(agent)


class TestBuildMcpServerAvailable:
    """When mcp is installed, build_mcp_server returns a configured Server."""

    def test_returns_server_with_tools(self):
        agent = _make_agent()
        server = build_mcp_server(agent)
        # Server is the mcp.server.Server instance.
        from mcp.server import Server

        assert isinstance(server, Server)
        # mcp 1.x exposes the server name via ``Server.name`` (earlier 1.x
        # releases used a ``server_info`` Implementation; both are gone in
        # current 1.x/2.x — ``name`` is the stable attribute).
        assert server.name == "doctoragent"
        # The list_tools handler must be registered.
        assert server.on_list_tools is not None
        assert server.on_call_tool is not None

    def test_list_tools_returns_registered_tools(self):
        agent = _make_agent()
        server = build_mcp_server(agent)
        result = asyncio.run(server.on_list_tools(None))  # type: ignore[arg-type]
        tool_names = [t.name for t in result.tools]
        assert "search_documents" in tool_names
        assert "memory" in tool_names

    @pytest.mark.asyncio
    async def test_call_tool_executes_and_returns_text(self):
        agent = _make_agent()
        server = build_mcp_server(agent)

        class _Ctx:
            name = "search_documents"
            arguments = {"query": "contracts", "top_k": 3}

        result = await server.on_call_tool(_Ctx())  # type: ignore[arg-type]
        assert result.is_error is False
        assert len(result.content) == 1
        assert "contracts" in result.content[0].text

    @pytest.mark.asyncio
    async def test_call_tool_unknown_returns_error(self):
        agent = _make_agent()
        server = build_mcp_server(agent)

        class _Ctx:
            name = "does_not_exist"
            arguments = {}

        result = await server.on_call_tool(_Ctx())  # type: ignore[arg-type]
        assert result.is_error is True
        assert "does_not_exist" in result.content[0].text

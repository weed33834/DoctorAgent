# mypy: ignore-errors
"""Tests for the MCP client bridge (connect external MCP servers / import tools)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from doctoragent.agent.mcp_client import (
    MCPClient,
    build_remote_tool,
    import_mcp_tools,
)
from doctoragent.model.tools import ToolRegistry


def _make_remote_tool_descriptor() -> dict[str, Any]:
    return {
        "name": "remote_search",
        "description": "Search a remote system",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "the query"},
                "limit": {"type": "integer", "description": "max results"},
            },
            "required": ["q"],
        },
    }


def _make_client_with_session() -> tuple[MCPClient, MagicMock]:
    client = MCPClient("fake", transport="stdio", command="true")
    session = AsyncMock()
    client._session = session
    return client, session


def test_build_remote_tool_definition() -> None:
    client = MCPClient("fake", transport="stdio", command="true")
    tool = build_remote_tool(client, _make_remote_tool_descriptor())
    d = tool.definition
    assert d.name == "remote_search"
    assert d.category == "mcp_remote"
    assert [p.name for p in d.parameters] == ["q", "limit"]
    assert d.parameters[0].required is True
    assert d.parameters[1].required is False


def test_build_remote_tool_name_override() -> None:
    client = MCPClient("fake", transport="stdio", command="true")
    tool = build_remote_tool(client, _make_remote_tool_descriptor(), name="ext_remote_search")
    assert tool.definition.name == "ext_remote_search"


@pytest.mark.asyncio
async def test_import_mcp_tools_registers_and_calls() -> None:
    client, session = _make_client_with_session()
    session.list_tools.return_value = MagicMock(
        tools=[MagicMock(name=_make_remote_tool_descriptor()["name"], **{})]
    )
    # Provide the descriptor through the mcp Tool object attributes.
    tool_mock = MagicMock()
    tool_mock.name = "remote_search"
    tool_mock.description = "Search a remote system"
    tool_mock.inputSchema = _make_remote_tool_descriptor()["inputSchema"]
    session.list_tools.return_value = MagicMock(tools=[tool_mock])

    callres = MagicMock()
    callres.isError = False
    callres.content = [MagicMock(text="remote result: ok")]
    session.call_tool.return_value = callres

    reg = ToolRegistry()
    names = await import_mcp_tools(client, reg, prefix="ext_")
    assert names == ["ext_remote_search"]
    assert reg.get("ext_remote_search") is not None

    result = await reg.execute("ext_remote_search", q="x")
    assert result.success is True
    assert result.data == {"result": "remote result: ok"}
    # The original (unprefixed) name is what is sent to the remote server.
    session.call_tool.assert_awaited_with("remote_search", {"q": "x"})


@pytest.mark.asyncio
async def test_mcp_client_call_tool_error() -> None:
    client, session = _make_client_with_session()
    callres = MagicMock()
    callres.isError = True
    callres.content = [MagicMock(text="bad thing happened")]
    session.call_tool.return_value = callres
    with pytest.raises(ValueError, match="failed"):
        await client.call_tool("remote_search", {"q": "x"})


@pytest.mark.asyncio
async def test_mcp_client_missing_command_raises() -> None:
    client = MCPClient("fake", transport="stdio")  # no command
    with pytest.raises(ValueError, match="command"):
        await client.connect()


@pytest.mark.asyncio
async def test_mcp_client_missing_url_raises() -> None:
    client = MCPClient("fake", transport="http")  # no url
    with pytest.raises(ValueError, match="url"):
        await client.connect()


@pytest.mark.asyncio
async def test_mcp_client_close_idempotent() -> None:
    client, session = _make_client_with_session()
    await client.close()
    await client.close()  # second close is a no-op

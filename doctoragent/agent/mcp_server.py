"""MCP (Model Context Protocol) server bridge for the DoctorAgent agent.

Exposes the agent's registered tools (``search_documents``, ``list_files``,
``get_file_details``, ``analyze_document``, ``memory``, ...) over MCP so
external MCP-compatible clients (Claude Desktop, IDE integrations, other
agents) can invoke them through a standard protocol.

The doctoragent :class:`~doctoragent.model.tools.ToolDefinition` uses the OpenAI
JSON-Schema tool format; :func:`build_mcp_server` converts each definition
to an MCP :class:`mcp.types.Tool` (``inputSchema`` ← ``parameters``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from doctoragent.model.tools import ToolDefinition

logger = logging.getLogger(__name__)


def _convert_tool_to_mcp(tool_def: ToolDefinition) -> dict[str, Any]:
    """Convert a doctoragent :class:`ToolDefinition` to an MCP tool dict.

    doctoragent uses the OpenAI tool schema (``{"type": "function",
    "function": {"name", "description", "parameters": {...}}}``); MCP
    wants ``{"name", "description", "inputSchema": {...}}`` where
    ``inputSchema`` is the parameters object.
    """
    openai_schema = tool_def.to_json_schema()
    function_block = openai_schema.get("function", {})
    parameters = function_block.get("parameters", {"type": "object", "properties": {}})
    return {
        "name": function_block.get("name", tool_def.name),
        "description": function_block.get("description", tool_def.description),
        "inputSchema": parameters,
    }


def build_mcp_server(agent: Any) -> Any:
    """Build an MCP :class:`mcp.server.Server` exposing the agent's tools.

    Pulls each :class:`~doctoragent.model.tools.ToolDefinition` from
    ``agent.tool_registry`` (or ``agent.tools``), converts it to the MCP
    tool schema, and registers ``list_tools`` / ``call_tool`` handlers
    that delegate back to the agent's registry.

    Raises:
        ValueError: if the agent has no ``tool_registry`` or ``tools``.
        ImportError: if the ``mcp`` package is not installed.
    """
    registry = getattr(agent, "tool_registry", None) or getattr(agent, "tools", None)
    if registry is None:
        raise ValueError("agent has no tool_registry / tools to expose via MCP")

    try:
        from mcp import types
        from mcp.server import Server
    except ImportError as exc:  # pragma: no cover - exercised in test via monkeypatch
        raise ImportError(
            "The 'mcp' package is required to build an MCP server. Install it with: pip install mcp"
        ) from exc

    tool_defs: list[ToolDefinition] = registry.list_tools()
    mcp_tools = [_convert_tool_to_mcp(td) for td in tool_defs]

    server = Server("doctoragent")

    async def _list_tools(_ctx: Any) -> types.ListToolsResult:  # noqa: ANN001
        tools = [types.Tool(**spec) for spec in mcp_tools]
        return types.ListToolsResult(tools=tools)

    async def _call_tool(ctx: Any) -> types.CallToolResult:  # noqa: ANN001
        name = getattr(ctx, "name", "") or ""
        arguments = getattr(ctx, "arguments", None) or {}
        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(arguments) if arguments else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        try:
            result = await registry.execute(name, **arguments)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to MCP client
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Tool '{name}' error: {exc}")],
                is_error=True,
            )
        text = json.dumps(
            result.data if result.success else {"error": result.error or "tool failed"},
            ensure_ascii=False,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            is_error=not result.success,
        )

    server.on_list_tools = _list_tools  # type: ignore[method-assignment]
    server.on_call_tool = _call_tool  # type: ignore[method-assignment]
    return server


async def run_mcp_server(agent: Any, transport: str = "stdio") -> None:
    """Start the MCP server built from *agent* over the given transport.

    Only ``"stdio"`` and ``"sse"`` are supported. This is a long-running
    coroutine that only returns when the client disconnects.
    """
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required to run an MCP server. Install it with: pip install mcp"
        ) from exc

    server = build_mcp_server(agent)
    init_options = server.create_initialization_options()

    if transport == "stdio":
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    elif transport == "sse":
        from mcp.server.sse import sse_server  # type: ignore[import-not-found]

        async with sse_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    else:
        raise ValueError(f"Unsupported MCP transport: {transport!r}. Use 'stdio' or 'sse'.")

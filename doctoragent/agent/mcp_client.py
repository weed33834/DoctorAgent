"""MCP (Model Context Protocol) client bridge.

Complements the existing MCP *server* (``doctoragent/agent/mcp_server.py``)
with the client half of M4.16: connect to **external** MCP servers (stdio or
HTTP), discover their tools, and import them into the agent's tool registry so
the ReAct loop can call remote tools through the standard protocol.

``MCPClient`` wraps a single MCP server connection:

* ``connect()`` / ``close()`` manage the session lifecycle.
* ``list_tools()`` returns the server's tool schemas.
* ``call_tool(name, arguments)`` invokes a remote tool and returns its text.

``import_mcp_tools`` converts the remote tools into
:class:`~doctoragent.model.tools.Tool` instances and registers them into a
:class:`~doctoragent.model.tools.ToolRegistry`, so they become callable by the
agent exactly like local tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class MCPClient:
    """A client for a single external MCP server (stdio or HTTP transport)."""

    def __init__(
        self,
        name: str,
        *,
        transport: str = "stdio",
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        http_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.transport = transport.lower()
        self.command = command
        self.args = args or []
        self.env = env
        self.url = url
        self.http_headers = http_headers or {}
        self.timeout = timeout
        self._session: Any = None
        self._readers: list[Any] = []
        self._writers: list[Any] = []
        self._lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open a session to the MCP server and cache it."""
        if self._session is not None:
            return
        async with self._lock:
            if self._session is not None:
                return
            if self.transport == "http":
                await self._connect_http()
            else:
                await self._connect_stdio()

    async def _connect_stdio(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "MCP stdio transport requires the 'mcp' extra "
                "(pip install doctoragent[mcp])"
            ) from exc
        if not self.command:
            raise ValueError("stdio transport requires a 'command'")
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        read, write = await stdio_client(params).__aenter__()
        self._readers.append(read)
        self._writers.append(write)
        self._session = await ClientSession(read, write).__aenter__()
        await self._session.initialize()

    async def _connect_http(self) -> None:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import (
                streamable_http_client as _shc,
            )
        except ImportError:
            try:  # older/newer naming variant
                from mcp.client.streamable_http import streamablehttp_client as _shc  # type: ignore[no-redef]
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "MCP HTTP transport requires the 'mcp' extra "
                    "(pip install doctoragent[mcp])"
                ) from exc
        if not self.url:
            raise ValueError("http transport requires a 'url'")
        ctx = _shc(self.url, headers=self.http_headers, timeout=self.timeout)
        streams = await ctx.__aenter__()
        self._readers.append(streams[0])
        self._writers.append(streams[1])
        self._session = await ClientSession(*streams).__aenter__()
        await self._session.initialize()

    async def close(self) -> None:
        """Tear down the session and underlying transports."""
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                logger.debug("MCP session close failed for %s: %s", self.name, exc)
        for w in self._writers:
            try:
                await w.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._writers.clear()
        self._readers.clear()

    # ── tool discovery & invocation ────────────────────────────────

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's tool descriptors (name/description/schema)."""
        await self.connect()
        assert self._session is not None
        result = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": getattr(t, "inputSchema", None) or {},
            }
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Invoke a remote tool and return its rendered text result.

        Raises ``ValueError`` when the server signals an error, so the agent's
        ReAct loop can surface the message back to the model.
        """
        await self.connect()
        assert self._session is not None
        result = await self._session.call_tool(name, arguments or {})
        text = _render_call_result(result)
        if getattr(result, "isError", False):
            raise ValueError(f"MCP tool {name!r} failed: {text or 'unknown error'}")
        return text

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


def _render_call_result(result: Any) -> str:
    """Extract text from an MCP CallToolResult."""
    chunks: list[str] = []
    for part in getattr(result, "content", []) or []:
        text = getattr(part, "text", None)
        if text is not None:
            chunks.append(str(text))
        else:
            structured = getattr(part, "structuredContent", None)
            if structured is not None:
                chunks.append(json.dumps(structured, ensure_ascii=False, default=str))
    return "\n".join(chunks)


# ── import into the doctoragent tool registry ───────────────────────────


def build_remote_tool(
    client: MCPClient, descriptor: dict[str, Any], *, name: str | None = None
) -> Any:
    """Build a :class:`~doctoragent.model.tools.Tool` that delegates to *client*.

    The returned object adapts a remote MCP tool to doctoragent's
    :class:`~doctoragent.model.tools.Tool` interface so it can be registered
    in a :class:`~doctoragent.model.tools.ToolRegistry` and called by the
    ReAct loop. The ``mcp`` extra is only required at connect time, so this
    factory stays importable without it.
    """
    from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter

    original_name = descriptor["name"]
    name = name or original_name
    description = descriptor.get("description") or ""
    schema = descriptor.get("inputSchema") or {}
    parameters: list[ToolParameter] = []
    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    for pname, pdef in properties.items():
        parameters.append(
            ToolParameter(
                name=pname,
                type=_json_type_to_str(pdef.get("type", "string")),
                description=(pdef.get("description") or "") if isinstance(pdef, dict) else "",
                required=pname in required,
                enum=pdef.get("enum") if isinstance(pdef, dict) else None,
            )
        )

    definition = ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        category="mcp_remote",
    )

    class _RemoteTool(Tool):
        @property
        def definition(self) -> ToolDefinition:
            return definition

        async def execute(self, **kwargs: Any) -> Any:
            from doctoragent.model.tools import ToolResult

            try:
                text = await client.call_tool(original_name, kwargs)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, error=str(exc), tool_name=name)
            return ToolResult(success=True, data={"result": text}, tool_name=name)

    return _RemoteTool()


def _json_type_to_str(t: Any) -> str:
    mapping = {"string": "string", "integer": "integer", "number": "float",
               "boolean": "boolean", "array": "list", "object": "object",
               "null": "string"}
    return mapping.get(str(t).lower(), "string")


async def import_mcp_tools(
    client: MCPClient,
    registry: Any,
    *,
    prefix: str = "",
) -> list[str]:
    """Discover a remote server's tools and register them into *registry*.

    Args:
        client: an open/connectable :class:`MCPClient`.
        registry: a :class:`~doctoragent.model.tools.ToolRegistry`.
        prefix: optional name prefix (e.g. ``"pubmed_"``) to namespace remote
            tools and avoid collisions.

    Returns:
        The list of registered tool names.
    """
    registered: list[str] = []
    for descriptor in await client.list_tools():
        effective_name = descriptor["name"]
        if prefix:
            effective_name = f"{prefix}{effective_name}"
        tool = build_remote_tool(client, descriptor, name=effective_name)
        if registry.get(effective_name) is None:
            registry.register(tool)
            registered.append(effective_name)
    return registered

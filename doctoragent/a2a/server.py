"""A2A JSON-RPC 2.0 task server.

Exposes DoctorAgent to other agents over the A2A protocol:

* ``GET  /.well-known/agent.json`` → Agent Card (discovery) — see
  :func:`doctoragent.a2a.build_agent_card`.
* ``POST /a2a/rpc`` → ``task/send``, ``task/get``, ``task/cancel``,
  ``agents/list``.

Task lifecycle: ``submitted → working → completed | failed | canceled``.

Usage::

    from doctoragent.a2a.server import A2AServer

    async def handle(message: dict, metadata: dict) -> Any:
        # route the message into the agent pipeline, return a text/JSON result
        return "…"

    server = A2AServer(
        name="DoctorAgent",
        description="Clinical decision-support agent",
        url="http://127.0.0.1:8000",
        handler=handle,
    )
    card = server.card.to_dict()          # → /.well-known/agent.json body
    resp = await server.handle_rpc({...}) # → JSON-RPC 2.0 response
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from doctoragent.a2a.models import A2AArtifact, A2ATask, AgentCard, TaskStatus

logger = logging.getLogger(__name__)

# (message, metadata) -> result. The result may be a scalar, a dict, or a
# ``{"error": ...}`` dict to signal a failed task.
TaskHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]

# JSON-RPC 2.0 standard error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_APPLICATION = -32000


class A2AError(Exception):
    """Application-level A2A error surfaced as a JSON-RPC ``-32000`` error."""


class A2AServer:
    """In-process A2A task server with a full task store."""

    def __init__(
        self,
        name: str,
        description: str,
        url: str = "",
        *,
        handler: TaskHandler | None = None,
        auth_type: str = "none",
        version: str = "1.0.0",
    ) -> None:
        self.name = name
        self.description = description
        self.card = AgentCard(
            name=name,
            description=description,
            url=url,
            auth_type=auth_type,
            version=version,
        )
        self.handler = handler
        self._tasks: dict[str, A2ATask] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # ── Agent Card (discovery) ───────────────────────────────────────

    def card_dict(self) -> dict[str, Any]:
        """The Agent Card body served at ``/.well-known/agent.json``."""
        return self.card.to_dict()

    def add_skill(self, skill: dict[str, Any]) -> None:
        """Attach a capability descriptor to the Agent Card."""
        self.card.skills.append(skill)

    # ── JSON-RPC 2.0 dispatcher ─────────────────────────────────────

    async def handle_rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a single JSON-RPC 2.0 request and return its response."""
        request_id = payload.get("id")
        method = payload.get("method", "")
        params = payload.get("params") or {}

        if payload.get("jsonrpc") != "2.0":
            return self._error(request_id, ERR_INVALID_REQUEST, "Invalid Request")

        try:
            if method == "task/send":
                result = await self._send(params)
            elif method == "task/get":
                result = await self._get(params)
            elif method == "task/cancel":
                result = await self._cancel(params)
            elif method == "task/list":
                result = {"tasks": [t.to_dict() for t in self._tasks.values()]}
            elif method == "agents/list":
                result = {"agents": [self.card_dict()]}
            elif method == "ping":
                result = {"status": "ok"}
            else:
                return self._error(request_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")
        except A2AError as exc:
            return self._error(request_id, ERR_APPLICATION, str(exc))
        except Exception as exc:  # noqa: BLE001 — map any handler failure to a clean RPC error
            logger.exception("A2A method %s failed", method)
            return self._error(request_id, ERR_INTERNAL, f"Internal error: {exc}")

        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ── method handlers ─────────────────────────────────────────────

    async def _send(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message") or {}
        metadata = params.get("metadata") or {}
        task = A2ATask.new(message=message, metadata=metadata)
        # Respect a client-provided id if given (idempotent task creation).
        client_id = params.get("id")
        if client_id:
            task.id = client_id
            if client_id in self._tasks:
                return self._tasks[client_id].to_dict()
        self._tasks[task.id] = task
        if self.handler is not None:
            task.status = TaskStatus.WORKING
            background = asyncio.create_task(self._run_handler(task.id))
            self._background.add(background)
            background.add_done_callback(self._background.discard)
        return task.to_dict()

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self._require_task(params.get("id", ""))
        return task.to_dict()

    async def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self._require_task(params.get("id", ""))
        if task.status in (TaskStatus.SUBMITTED, TaskStatus.WORKING, TaskStatus.INPUT_REQUIRED):
            task.status = TaskStatus.CANCELED
        return task.to_dict()

    async def _run_handler(self, task_id: str) -> None:
        """Execute the handler for a task and record the outcome."""
        task = self._tasks.get(task_id)
        if task is None or self.handler is None:
            return
        try:
            result = await self.handler(task.message or {}, task.metadata or {})
            if isinstance(result, dict) and result.get("error"):
                task.status = TaskStatus.FAILED
                task.metadata = {**(task.metadata or {}), "error": result["error"]}
            else:
                task.status = TaskStatus.COMPLETED
                task.artifacts.append(
                    A2AArtifact(
                        parts=[{"type": "text", "text": _stringify(result)}],
                        index=0,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — handler failure marks the task failed
            logger.exception("A2A handler failed for task %s", task_id)
            task.status = TaskStatus.FAILED
            task.metadata = {**(task.metadata or {}), "error": str(exc)}

    # ── helpers ─────────────────────────────────────────────────────

    def _require_task(self, task_id: str) -> A2ATask:
        task = self._tasks.get(task_id)
        if task is None:
            raise A2AError(f"Task {task_id!r} not found")
        return task

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    # ── introspection ───────────────────────────────────────────────

    def list_tasks(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._tasks.values()]

    def task_count(self) -> int:
        return len(self._tasks)

    async def close(self) -> None:
        """Cancel any in-flight background handlers."""
        for task in list(self._background):
            if not task.done():
                task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()


def _stringify(result: Any) -> str:
    """Render an A2A result as the text content of an artifact."""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        import json

        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except TypeError:
            return str(result)
    return str(result)


def build_default_handler(agent: Any) -> TaskHandler:
    """Return an A2A handler that routes a text message into the agent.

    The handler reads the message's first text part and passes it to the
    wrapped :class:`~doctoragent.orchestration.agent.AegisAgent` (or any
    object exposing an async ``run`` / ``ask`` method). It degrades to a
    descriptive error when the agent exposes no runnable interface.

    Args:
        agent: an object with an async ``run(message)`` or ``ask(message)``
            method, or ``None`` to use a stub handler.
    """
    run_fn = getattr(agent, "run", None) or getattr(agent, "ask", None)

    async def _handler(message: dict[str, Any], metadata: dict[str, Any]) -> Any:
        text = _extract_text(message)
        if run_fn is None:
            return {
                "error": (
                    "No runnable agent bound to this A2A server — configure a "
                    "handler or attach an AegisAgent."
                )
            }
        if not text:
            return {"error": "Empty message: no text part provided."}
        try:
            if asyncio.iscoroutinefunction(run_fn):
                return await run_fn(text)
            return run_fn(text)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    return _handler


def _extract_text(message: dict[str, Any]) -> str:
    """Pull the first ``text`` part out of an A2A message.

    Handles both the A2A canonical form ``{"role": ..., "parts": [...]}``
    and a simple ``{"text": ...}`` convenience form.
    """
    if not isinstance(message, dict):
        return ""
    if isinstance(message.get("text"), str) and message["text"]:
        return message["text"]
    parts = message.get("parts") or []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            return str(part["text"])
        if isinstance(part, str):
            return part
    return ""

"""A2A protocol client.

Discovers remote agents via their Agent Card (``/.well-known/agent.json``),
submits tasks over JSON-RPC 2.0, and polls them to completion.

This is the **L7 Orchestration Layer** half of the A2A story — it lets
DoctorAgent delegate subtasks to *other* agents (LangGraph ↔ Claude SDK ↔
OpenAI Agents SDK) instead of only being called by them.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx

from doctoragent._utils import NoCloseClient
from doctoragent.a2a.models import A2AArtifact, A2ATask, AgentCard, TaskStatus

# JSON-RPC 2.0 error codes we surface as client errors
_ERR_METHOD_NOT_FOUND = -32601


class A2AError(Exception):
    """Client-side A2A failure (discovery / RPC / timeout)."""


class A2AClient:
    """Client for talking to remote A2A agents."""

    def __init__(
        self,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        self.headers = dict(headers or {})
        # An optional externally-provided httpx client (e.g. bound to an ASGI
        # transport for tests, or a pooled client for production). When absent
        # a short-lived client is created per call.
        self._http_client = http_client
        self._agent_cache: dict[str, AgentCard] = {}

    # ── Agent Card discovery ────────────────────────────────────────

    async def discover_agent(self, base_url: str, *, refresh: bool = False) -> AgentCard:
        """Fetch and cache an Agent Card from ``{base_url}/.well-known/agent.json``."""
        base_url = base_url.rstrip("/")
        if not refresh and base_url in self._agent_cache:
            return self._agent_cache[base_url]

        url = f"{base_url}/.well-known/agent.json"
        try:
            async with self._make_client() as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except A2AError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap any transport failure
            raise A2AError(f"Agent card discovery failed for {base_url}: {exc}") from exc

        card = AgentCard(
            name=data.get("name", base_url),
            description=data.get("description", ""),
            url=data.get("url", base_url),
            skills=data.get("skills") or [],
            auth_type=data.get("auth_type", "none"),
            endpoints=data.get("endpoints") or ["/a2a/rpc"],
            version=data.get("version", "1.0.0"),
        )
        self._agent_cache[base_url] = card
        return card

    async def discover_agents(self, base_urls: list[str]) -> list[AgentCard]:
        """Discover multiple agents concurrently, skipping failures."""
        results = await asyncio.gather(
            *(self.discover_agent(u) for u in base_urls),
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, AgentCard)]

    # ── Task submission ─────────────────────────────────────────────

    async def send_task(
        self,
        agent_url: str,
        message: dict[str, Any],
        *,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> A2ATask:
        """Submit a task to a remote agent (``task/send``)."""
        tid = task_id or f"task-{uuid.uuid4().hex[:12]}"
        payload = {
            "jsonrpc": "2.0",
            "method": "task/send",
            "params": {"id": tid, "message": message, "metadata": metadata or {}},
            "id": f"send-{tid}",
        }
        data = await self._rpc_call(agent_url, payload)
        return _parse_task(data.get("result", data))

    async def get_task(self, agent_url: str, task_id: str) -> A2ATask:
        """Fetch a task's current state (``task/get``)."""
        payload = {
            "jsonrpc": "2.0",
            "method": "task/get",
            "params": {"id": task_id},
            "id": f"get-{task_id}",
        }
        data = await self._rpc_call(agent_url, payload)
        return _parse_task(data.get("result", data))

    async def cancel_task(self, agent_url: str, task_id: str) -> A2ATask:
        """Cancel a task (``task/cancel``)."""
        payload = {
            "jsonrpc": "2.0",
            "method": "task/cancel",
            "params": {"id": task_id},
            "id": f"cancel-{task_id}",
        }
        data = await self._rpc_call(agent_url, payload)
        return _parse_task(data.get("result", data))

    async def send_and_wait(
        self,
        agent_url: str,
        message: dict[str, Any],
        *,
        poll_interval: float = 1.0,
        max_wait: float = 120.0,
    ) -> A2ATask:
        """Submit a task and poll until a terminal state (long-task helper)."""
        task = await self.send_task(agent_url, message)
        deadline = time.monotonic() + max_wait
        while task.status in (TaskStatus.SUBMITTED, TaskStatus.WORKING, TaskStatus.INPUT_REQUIRED):
            if time.monotonic() > deadline:
                raise A2AError(
                    f"Task {task.id} did not finish within {max_wait}s (status={task.status.value})"
                )
            await asyncio.sleep(poll_interval)
            task = await self.get_task(agent_url, task.id)
        return task

    # ── internals ───────────────────────────────────────────────────

    async def _rpc_call(self, agent_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        card = await self.discover_agent(agent_url)
        endpoint = card.endpoints[0] if card.endpoints else "/a2a/rpc"
        url = agent_url.rstrip("/") + endpoint
        headers = {"Content-Type": "application/json", **self.headers}
        try:
            async with self._make_client() as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code >= 400:
                    raise A2AError(
                        f"A2A RPC call to {url} failed with HTTP {resp.status_code}: "
                        f"{resp.text[:300]}"
                    )
                resp.raise_for_status()
                data = resp.json()
        except A2AError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap transport errors
            raise A2AError(f"A2A RPC call to {url} failed: {exc}") from exc

        if data.get("error"):
            raise A2AError(
                f"A2A server error [{data['error'].get('code')}]: {data['error'].get('message')}"
            )
        return data

    def _make_client(self) -> httpx.AsyncClient | NoCloseClient:
        """Return the injected client (no-op context manager) or a new one."""
        if self._http_client is not None:
            return NoCloseClient(self._http_client)
        return httpx.AsyncClient(timeout=self.timeout, headers=self.headers)

    def clear_cache(self) -> None:
        """Drop the Agent Card cache (e.g. after a remote agent updates)."""
        self._agent_cache.clear()


def _parse_task(result: dict[str, Any]) -> A2ATask:
    """Parse a JSON-RPC ``result`` into an :class:`A2ATask`."""
    raw_status = result.get("status", "submitted")
    try:
        status = TaskStatus(raw_status)
    except ValueError:
        status = TaskStatus.SUBMITTED
    artifacts = [
        A2AArtifact(parts=a.get("parts", []), metadata=a.get("metadata", {}), index=i)
        for i, a in enumerate(result.get("artifacts", []) or [])
    ]
    return A2ATask(
        id=result.get("id", ""),
        status=status,
        message=result.get("message"),
        artifacts=artifacts,
        metadata=result.get("metadata", {}) or {},
    )

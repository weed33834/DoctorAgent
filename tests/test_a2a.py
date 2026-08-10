# mypy: ignore-errors
"""Tests for the A2A protocol suite (models / server / client)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from doctoragent.a2a import A2AClient, A2AServer, TaskStatus
from doctoragent.a2a.models import AgentCard
from doctoragent.a2a.server import _extract_text


# ── models ─────────────────────────────────────────────────────────────


def test_task_status_values() -> None:
    assert TaskStatus.SUBMITTED.value == "submitted"
    assert TaskStatus.COMPLETED.value == "completed"
    assert TaskStatus.FAILED.value == "failed"
    assert TaskStatus.CANCELED.value == "canceled"


def test_agent_card_to_dict() -> None:
    card = AgentCard(name="Doc", description="d", url="http://x", endpoints=["/a2a/rpc"])
    d = card.to_dict()
    assert d["name"] == "Doc"
    assert d["endpoints"] == ["/a2a/rpc"]
    assert d["auth_type"] == "none"


def test_task_text_joins_artifacts() -> None:
    from doctoragent.a2a.models import A2AArtifact, A2ATask

    task = A2ATask(
        id="t1",
        status=TaskStatus.COMPLETED,
        artifacts=[
            A2AArtifact(parts=[{"type": "text", "text": "hello"}], index=0),
            A2AArtifact(parts=[{"type": "text", "text": "world"}], index=1),
        ],
    )
    assert task.text == "hello\nworld"


# ── server ─────────────────────────────────────────────────────────────


async def _handler(message: dict[str, Any], metadata: dict[str, Any]) -> Any:
    return f"processed:{_extract_text(message)}"


class TestA2AServer:
    @pytest.fixture
    def server(self) -> A2AServer:
        return A2AServer(
            name="Doc",
            description="desc",
            url="http://x",
            handler=_handler,
        )

    @pytest.mark.asyncio
    async def test_card_dict(self, server: A2AServer) -> None:
        card = server.card_dict()
        assert card["name"] == "Doc"
        assert card["auth_type"] == "none"

    @pytest.mark.asyncio
    async def test_ping(self, server: A2AServer) -> None:
        resp = await server.handle_rpc({"jsonrpc": "2.0", "method": "ping", "params": {}, "id": 1})
        assert resp["result"] == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_send_and_get_task(self, server: A2AServer) -> None:
        resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "method": "task/send",
                "params": {
                    "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
                },
                "id": 2,
            }
        )
        task_id = resp["result"]["id"]
        # Allow the background handler to finish.
        for _ in range(50):
            state = await server.handle_rpc(
                {"jsonrpc": "2.0", "method": "task/get", "params": {"id": task_id}, "id": 3}
            )
            if state["result"]["status"] == TaskStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.02)
        assert state["result"]["status"] == TaskStatus.COMPLETED.value
        assert state["result"]["artifacts"][0]["parts"][0]["text"] == "processed:hi"

    @pytest.mark.asyncio
    async def test_cancel_task(self, server: A2AServer) -> None:
        resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "method": "task/send",
                "params": {"message": {"parts": [{"type": "text", "text": "x"}]}},
                "id": 4,
            }
        )
        task_id = resp["result"]["id"]
        cancelled = await server.handle_rpc(
            {"jsonrpc": "2.0", "method": "task/cancel", "params": {"id": task_id}, "id": 5}
        )
        assert cancelled["result"]["status"] == TaskStatus.CANCELED.value

    @pytest.mark.asyncio
    async def test_unknown_method(self, server: A2AServer) -> None:
        resp = await server.handle_rpc(
            {"jsonrpc": "2.0", "method": "nope", "params": {}, "id": 6}
        )
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_missing_task(self, server: A2AServer) -> None:
        resp = await server.handle_rpc(
            {"jsonrpc": "2.0", "method": "task/get", "params": {"id": "nope"}, "id": 7}
        )
        assert resp["error"]["code"] == -32000

    @pytest.mark.asyncio
    async def test_handler_failure_marks_failed(self) -> None:
        async def bad(_m: dict[str, Any], _meta: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

        srv = A2AServer(name="D", description="d", handler=bad)
        resp = await srv.handle_rpc(
            {
                "jsonrpc": "2.0",
                "method": "task/send",
                "params": {"message": {"parts": [{"type": "text", "text": "x"}]}},
                "id": 8,
            }
        )
        task_id = resp["result"]["id"]
        for _ in range(50):
            state = await srv.handle_rpc(
                {"jsonrpc": "2.0", "method": "task/get", "params": {"id": task_id}, "id": 9}
            )
            if state["result"]["status"] != TaskStatus.WORKING.value:
                break
            await asyncio.sleep(0.02)
        assert state["result"]["status"] == TaskStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_agents_list(self, server: A2AServer) -> None:
        resp = await server.handle_rpc(
            {"jsonrpc": "2.0", "method": "agents/list", "params": {}, "id": 10}
        )
        assert resp["result"]["agents"][0]["name"] == "Doc"


# ── client (against an in-process FastAPI server) ──────────────────────


class TestA2AClient:
    @pytest.mark.asyncio
    async def test_client_send_and_wait(self) -> None:
        """Run an A2A server on a FastAPI app and drive it via the client."""
        import httpx
        from fastapi import FastAPI

        server = A2AServer(name="Doc", description="d", url="http://test", handler=_handler)
        app = FastAPI()

        @app.get("/.well-known/agent.json")
        async def card() -> dict[str, Any]:
            return server.card_dict()

        @app.post("/a2a/rpc")
        async def rpc(payload: dict[str, Any]) -> dict[str, Any]:
            return await server.handle_rpc(payload)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            a2a = A2AClient(timeout=5.0, http_client=ac)
            card_obj = await a2a.discover_agent("http://test")
            assert card_obj.name == "Doc"
            task = await a2a.send_and_wait(
                "http://test",
                {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
                poll_interval=0.02,
                max_wait=5.0,
            )
            assert task.status == TaskStatus.COMPLETED
            assert task.text == "processed:hello"


# ── FastAPI create_app integration ──────────────────────────────────────


def test_create_app_a2a_endpoints_real() -> None:
    """The A2A endpoints are reachable on the production create_app app."""
    try:
        from unittest.mock import MagicMock

        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app
        from doctoragent.config import AegisConfig
    except ImportError:  # pragma: no cover — FastAPI optional
        pytest.skip("FastAPI not installed")

    config = AegisConfig()
    agent = MagicMock()

    async def run(text: str) -> str:
        return f"answer:{text}"

    agent.run = run
    app = create_app(config, agent)
    client = TestClient(app)

    card = client.get("/.well-known/agent.json")
    assert card.status_code == 200
    assert card.json()["name"] == config.a2a.agent_name

    resp = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "method": "task/send",
            "params": {"message": {"parts": [{"type": "text", "text": "hi"}]}},
            "id": 1,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["id"]

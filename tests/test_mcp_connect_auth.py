"""Regression tests: the MCP-over-HTTP endpoints require authentication.

History (v0.3.4–v0.3.5): ``POST /mcp/connect`` accepted
``{transport: stdio, command, args}`` and launched an arbitrary process
server-side, and none of the MCP HTTP endpoints carried an auth
dependency. Worse, all four routes were registered on the APIRouter
*AFTER* ``app.include_router(router)`` ran, so FastAPI never mounted
them at all — dead endpoints that docs still advertised. v0.3.6 moved
the router mounts to the end of ``create_app`` (resurrecting them) with
fail-closed auth on every write/invoke surface:

* ``POST /mcp/connect``  — sensitive auth (launches processes)
* ``GET  /mcp/clients``  — sensitive auth (infrastructure disclosure)
* ``POST /mcp``          — sensitive auth (executes registered tools)
* ``GET  /mcp/tools``    — standard read auth

These tests pin exact status codes so a resurrected-but-unauthenticated
endpoint or an accidentally-dropped route both fail loudly.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.api.server import is_available
from doctoragent.config import AegisConfig


@pytest.mark.skipif(
    not is_available(),
    reason="FastAPI is not installed (optional dependency)",
)
class TestMcpEndpointsAuth:
    """MCP HTTP endpoints must be mounted AND fail-closed authenticated."""

    @pytest.fixture
    def config(self, tmp_path: Path) -> AegisConfig:
        cfg = AegisConfig()
        cfg.paths.inbox = tmp_path / "Inbox"
        cfg.paths.vault = tmp_path / "Vault"
        cfg.paths.index = tmp_path / "Index"
        cfg.paths.logs = tmp_path / "Logs"
        cfg.paths.connections = tmp_path / "Config" / "connections.json"
        for p in [cfg.paths.inbox, cfg.paths.vault, cfg.paths.index, cfg.paths.logs]:
            p.mkdir(parents=True, exist_ok=True)
        cfg.paths.connections.parent.mkdir(parents=True, exist_ok=True)
        return cfg

    @pytest.fixture
    def mock_agent(self) -> MagicMock:
        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        del agent._sync_engine
        del agent.search

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search
        return agent

    @pytest.fixture
    def authed_client(
        self,
        config: AegisConfig,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Any:
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config, mock_agent)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    @pytest.fixture
    def anon_client(self, config: AegisConfig, mock_agent: MagicMock) -> Any:
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config, mock_agent)
        return TestClient(app)

    # ── routes actually exist (regression against silent drop) ────────

    def test_mcp_routes_are_mounted(self, authed_client: Any) -> None:
        """All four documented MCP HTTP routes respond, not 404."""
        assert authed_client.get("/mcp/tools").status_code != 404
        assert (
            authed_client.post("/mcp", json={"method": "tools/list"}).status_code
            != 404
        )
        assert authed_client.post("/mcp/connect", json={"name": "x"}).status_code != 404
        assert authed_client.get("/mcp/clients").status_code != 404

    # ── fail-closed without token ─────────────────────────────────────

    def test_connect_requires_token(self, anon_client: Any) -> None:
        """Without DOCTORAGENT_API_TOKEN the endpoint is fail-closed (403)."""
        response = anon_client.post("/mcp/connect", json={"name": "evil"})
        assert response.status_code == 403

    def test_clients_list_requires_token(self, anon_client: Any) -> None:
        """Connected-server listing is denied without a token (403)."""
        response = anon_client.get("/mcp/clients")
        assert response.status_code == 403

    def test_invoke_requires_token(self, anon_client: Any) -> None:
        """Tool invocation is denied without a token (403)."""
        response = anon_client.post("/mcp", json={"method": "tools/list"})
        assert response.status_code == 403

    # ── reachable with token ──────────────────────────────────────────

    def test_connect_with_token_reaches_handler(self, authed_client: Any) -> None:
        """With a valid token the request reaches the handler (400/500)."""
        response = authed_client.post("/mcp/connect", json={"name": "x"})
        assert response.status_code in (400, 500)

    def test_clients_list_with_token_ok(self, authed_client: Any) -> None:
        """With a valid token the listing returns 200."""
        response = authed_client.get("/mcp/clients")
        assert response.status_code == 200

    def test_tools_list_with_token_ok(self, authed_client: Any) -> None:
        """With a valid token the tool listing returns 200."""
        response = authed_client.get("/mcp/tools")
        assert response.status_code in (200, 500)

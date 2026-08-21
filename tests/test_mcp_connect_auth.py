"""Regression tests: /mcp/connect and /mcp/clients require sensitive auth.

Both endpoints were previously registered without any authentication
dependency. ``POST /mcp/connect`` accepts ``{transport: stdio, command,
args}`` and launches an arbitrary process server-side, which made it an
unauthenticated remote-code-execution vector on deployments without a
token. Both endpoints are now fail-closed sensitive endpoints.
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
    """MCP client endpoints must be fail-closed sensitive endpoints."""

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

    def test_connect_requires_token(
        self,
        anon_client: Any,
    ) -> None:
        """Without DOCTORAGENT_API_TOKEN the endpoint is fail-closed (403)."""
        response = anon_client.post("/mcp/connect", json={"name": "evil"})
        assert response.status_code == 403

    def test_clients_list_requires_token(self, anon_client: Any) -> None:
        """Connected-server listing is denied without a token (403)."""
        response = anon_client.get("/mcp/clients")
        assert response.status_code == 403

    def test_connect_with_token_passes_auth(self, authed_client: Any) -> None:
        """With a valid token the request reaches the handler (not 401/403)."""
        response = authed_client.post("/mcp/connect", json={"name": "x"})
        assert response.status_code not in (401, 403)

    def test_clients_list_with_token_ok(self, authed_client: Any) -> None:
        """With a valid token the listing returns 200."""
        response = authed_client.get("/mcp/clients")
        assert response.status_code == 200

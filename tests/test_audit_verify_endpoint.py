"""Tests for the GET /audit/verify endpoint (HMAC integrity check).

Fix 4 exposes the audit-log ``verify()`` as an endpoint and runs it on
startup so tampered records are detected instead of being silently read.
"""

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.api.server import is_available
from doctoragent.config import AegisConfig
from doctoragent.security.audit_log import AuditLogger


@pytest.mark.skipif(
    not is_available(),
    reason="FastAPI is not installed (optional dependency)",
)
class TestAuditVerifyEndpoint:
    """GET /audit/verify exposes HMAC verification of the audit log."""

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
    def audit_logger(self, config: AegisConfig) -> AuditLogger:
        return AuditLogger(config, hmac_key=b"x" * 32)

    @pytest.fixture
    def mock_agent(self, audit_logger: AuditLogger) -> MagicMock:
        # Wire a *real* AuditLogger so verify() returns genuine results.
        agent = MagicMock()
        agent.audit_logger = audit_logger
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = os.urandom(32)
        # Mirror the api-server test fixture: no sync engine, async search.
        del agent._sync_engine
        del agent.search

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search
        return agent

    @pytest.fixture
    def client(
        self,
        config: AegisConfig,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Any:
        # /audit/verify is a sensitive (fail-closed) endpoint, so a token is
        # required to reach the handler.
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config, mock_agent)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    def test_verify_returns_ok_when_log_intact(
        self,
        client: Any,
        audit_logger: AuditLogger,
    ) -> None:
        """An untampered log verifies cleanly with no mismatches."""
        audit_logger.log("file_ingested", {"task_id": "1"})
        audit_logger.log("encrypted", {"task_id": "1"})

        response = client.get("/audit/verify")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["mismatches"] == []

    def test_verify_reports_tampered_records(
        self,
        client: Any,
        audit_logger: AuditLogger,
        config: AegisConfig,
    ) -> None:
        """A modified payload is detected and the offending line is returned."""
        audit_logger.log("file_ingested", {"task_id": "1"})

        log_path = config.paths.logs / "audit.log.ndjson"
        tampered = log_path.read_text().replace('"task_id": "1"', '"task_id": "2"')
        log_path.write_text(tampered)

        response = client.get("/audit/verify")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["mismatches"] == [1]

    def test_verify_requires_token(
        self,
        config: AegisConfig,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without DOCTORAGENT_API_TOKEN the sensitive endpoint is fail-closed (403)."""
        monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config, mock_agent)
        client = TestClient(app)
        response = client.get("/audit/verify")
        assert response.status_code == 403

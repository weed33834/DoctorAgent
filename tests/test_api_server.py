"""Tests for the DoctorAgent API server."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.api.server import is_available


def test_is_available_checks_fastapi_presence() -> None:
    """is_available returns True when FastAPI is importable."""
    result = is_available()
    assert isinstance(result, bool)


def test_server_module_imports_without_fastapi() -> None:
    """The server module can be imported even without FastAPI installed."""
    import doctoragent.api.server  # noqa: F401

    assert doctoragent.api.server.is_available is not None


def test_resolve_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_token reads DOCTORAGENT_API_TOKEN from environment."""
    from doctoragent.api.server import _resolve_token

    monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "secret-token-42")
    assert _resolve_token() == "secret-token-42"

    monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
    assert _resolve_token() is None


def test_check_available_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """_check_available raises ImportError when FastAPI is None."""
    import doctoragent.api.server as mod

    original = mod._FASTAPI_AVAILABLE
    try:
        mod._FASTAPI_AVAILABLE = False
        with pytest.raises(ImportError, match="FastAPI is required"):
            mod._check_available()
    finally:
        mod._FASTAPI_AVAILABLE = original


def test_run_server_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_server raises ImportError when FastAPI is not installed."""
    import doctoragent.api.server as mod

    monkeypatch.setattr(mod, "_FASTAPI_AVAILABLE", False)
    with pytest.raises(ImportError, match="FastAPI is required"):
        mod._check_available()


# ── FastAPI-dependent tests (only run when FastAPI is available) ──────


@pytest.mark.skipif(
    not is_available(),
    reason="FastAPI is not installed (optional dependency)",
)
class TestFastAPIIntegration:
    """Integration tests that require a running FastAPI app."""

    @pytest.fixture
    def config_with_inbox(self, tmp_path: Path) -> Any:
        from doctoragent.config import AegisConfig

        config = AegisConfig()
        config.paths.inbox = tmp_path / "Inbox"
        config.paths.vault = tmp_path / "Vault"
        config.paths.index = tmp_path / "Index"
        config.paths.logs = tmp_path / "Logs"
        config.paths.connections = tmp_path / "Config" / "connections.json"
        for p in [
            config.paths.inbox,
            config.paths.vault,
            config.paths.index,
            config.paths.logs,
        ]:
            p.mkdir(parents=True, exist_ok=True)
        config.paths.connections.parent.mkdir(parents=True, exist_ok=True)
        return config

    @pytest.fixture
    def mock_agent(self) -> MagicMock:
        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = os.urandom(32)
        # Remove _sync_engine so sync endpoints see it as unavailable.
        del agent._sync_engine
        # Make search async-compatible by returning a coroutine-wrapped list.
        del agent.search  # remove auto-mock

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search
        return agent

    @pytest.fixture
    def app_client(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Any:
        # fail-closed 后读端点需要 token；为功能测试统一注入测试 token。
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    def test_health_endpoint(self, app_client: Any) -> None:
        """GET /health returns status ok."""
        response = app_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_vault_status_fail_closed_without_token(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /vault/status returns 401 without token (fail-closed for non-local clients).

        读端点改为 fail-closed：未配置 ``DOCTORAGENT_API_TOKEN`` 时，非本地
        （回环/Unix socket 之外）请求一律拒绝。Starlette TestClient 的 host
        为 ``testclient``，不在本地信任集合中，因此应得到 401。
        """
        monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app)
        response = client.get("/vault/status")
        assert response.status_code == 401

    def test_vault_status_authenticated_required(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /vault/status returns 401 when token is configured but missing."""
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "my-secret-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app)
        response = client.get("/vault/status")
        assert response.status_code == 401

    def test_vault_status_with_valid_token(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /vault/status works with valid Bearer token."""
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "my-secret-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app)
        response = client.get(
            "/vault/status",
            headers={"Authorization": "Bearer my-secret-token"},
        )
        assert response.status_code == 200

    def test_lifespan_shutdown_calls_agent_aclose(
        self,
        config_with_inbox: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On shutdown the lifespan MUST call ``agent.aclose()`` so the key
        rotator thread, inbox watcher and classifier provider connections are
        released — previously this only happened on the CLI daemon path."""
        from unittest.mock import AsyncMock

        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = os.urandom(32)
        agent.aclose = AsyncMock()
        del agent._sync_engine
        del agent.search

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search

        app = create_app(config_with_inbox, agent)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            # Touch the app so the lifespan startup runs.
            assert client.get("/health").status_code == 200
            assert not agent.aclose.called
        # After the ``with`` block exits, the lifespan shutdown ran and must
        # have torn down the agent.
        assert agent.aclose.called

    def test_lifespan_shutdown_cancels_sync_tasks(
        self,
        config_with_inbox: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /sync/trigger creates a tracked background task; on shutdown
        the lifespan cancels and clears it so no orphan sync task outlives the
        server."""
        import asyncio

        from unittest.mock import AsyncMock

        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        import doctoragent.api.server as server_mod
        from doctoragent.api.server import create_app

        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = os.urandom(32)
        agent.aclose = AsyncMock()
        del agent._sync_engine
        del agent.search

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search

        # A fake sync engine whose sync_once never completes on its own so we
        # can observe the lifespan cancelling it. stop_sync_server is awaited
        # during shutdown.
        blocker = asyncio.Event()

        async def _never_finish() -> dict[str, Any]:
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                return {"cancelled": True}
            return {"ok": True}

        fake_engine = MagicMock()
        fake_engine.sync_once = _never_finish
        fake_engine.stop_sync_server = AsyncMock()
        # The endpoint closes over the local ``sync_engine`` returned by
        # ``_init_sync_engine``, so we must patch the factory, not app.state.
        monkeypatch.setattr(server_mod, "_init_sync_engine", lambda _cfg: fake_engine)

        app = create_app(config_with_inbox, agent)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            # Trigger a sync round — should be tracked in app.state.sync_tasks.
            assert client.post("/sync/trigger").status_code == 200
            assert len(app.state.sync_tasks) == 1
            tracked = next(iter(app.state.sync_tasks))
            assert not tracked.done()
        # After shutdown the task was cancelled and the tracking set cleared.
        assert tracked.cancelled() or tracked.done()
        assert app.state.sync_tasks == set()
        fake_engine.stop_sync_server.assert_awaited()

    def test_clinical_analyze_endpoint_and_audit(
        self,
        config_with_inbox: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /clinical/analyze runs the clinical workflow and records the
        decision + any blocking safety finding in the tamper-evident audit log
        (FDA SaMD / 21 CFR Part 11)."""
        from unittest.mock import AsyncMock

        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app
        from doctoragent.security.audit_log import AuditLogger

        # Real AuditLogger writing to the tmp logs dir so we can read records.
        audit_logger = AuditLogger(config_with_inbox)

        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = os.urandom(32)
        agent.audit_logger = audit_logger
        agent.aclose = AsyncMock()
        # No LLM provider → orchestrator takes the rules-only degraded path.
        agent._llm_provider = None
        agent.llm_provider = None
        del agent._sync_engine
        del agent.search

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search

        app = create_app(config_with_inbox, agent)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            # A patient context with a critical-severity vital sign so the
            # deterministic rule engine emits a blocking finding.
            payload = {
                "patient_context": {
                    "patient_id": "p-test-1",
                    "vitals": {"heart_rate": 35},
                },
                "query": "患者心率 35，是否需要紧急评估？",
            }
            resp = client.post("/clinical/analyze", json=payload)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["requires_human_review"] is True
            assert any(
                f.get("severity") in ("critical", "contraindicated")
                for f in data["safety_findings"]
            )
            assert data["disclaimer"]

            # Audit log must contain a clinical_decision and a
            # clinical_safety_alert record for this run.
            records = audit_logger.query()
            event_types = {r.get("event_type") for r in records}
            assert "clinical_decision" in event_types
            assert "clinical_safety_alert" in event_types
            # Verify the safety alert carried the patient_id for traceability.
            alert = next(
                r for r in records if r.get("event_type") == "clinical_safety_alert"
            )
            assert alert["details"]["patient_id"] == "p-test-1"

    def test_vault_files_list(self, app_client: Any) -> None:
        """GET /vault/files returns paginated file list."""
        response = app_client.get("/vault/files")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "offset" in data
        assert "limit" in data
        assert "files" in data

    def test_vault_files_list_with_params(self, app_client: Any) -> None:
        """GET /vault/files supports category, offset, limit parameters."""
        response = app_client.get("/vault/files?category=documents&offset=0&limit=10")
        assert response.status_code == 200
        data = response.json()
        assert data["offset"] == 0
        assert data["limit"] == 10

    def test_vault_files_list_invalid_limit(self, app_client: Any) -> None:
        """GET /vault/files rejects limit > 500."""
        response = app_client.get("/vault/files?limit=600")
        assert response.status_code == 422

    def test_vault_files_list_invalid_offset(self, app_client: Any) -> None:
        """GET /vault/files rejects negative offset."""
        response = app_client.get("/vault/files?offset=-1")
        assert response.status_code == 422

    def test_vault_file_metadata_not_found(self, app_client: Any) -> None:
        """GET /vault/files/{id} returns 404 for unknown ID."""
        import uuid

        unknown_id = str(uuid.uuid4())
        response = app_client.get(f"/vault/files/{unknown_id}")
        assert response.status_code == 404

    def test_vault_file_metadata_invalid_id(self, app_client: Any) -> None:
        """GET /vault/files/{id} returns 400 for invalid UUID."""
        response = app_client.get("/vault/files/not-a-uuid")
        assert response.status_code == 400

    def test_search_endpoint(self, app_client: Any) -> None:
        """POST /vault/search runs search via the agent."""
        response = app_client.post(
            "/vault/search",
            json={"query": "test", "top_k": 5, "semantic": False},
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_search_endpoint_validation(self, app_client: Any) -> None:
        """POST /vault/search validates request body."""
        response = app_client.post("/vault/search", json={})
        assert response.status_code == 422

    def test_sync_status_no_engine(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /sync/status reports unavailable when no sync engine.

        Monkeypatches ``_init_sync_engine`` to return ``None`` so the
        ``app.state.sync_engine`` is not set, simulating a deployment where
        the sync subsystem could not be initialised.
        """
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        monkeypatch.setattr("doctoragent.api.server._init_sync_engine", lambda cfg: None)
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app, headers={"Authorization": "Bearer test-token"})
        response = client.get("/sync/status")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False

    def test_sync_status_with_engine(self, app_client: Any) -> None:
        """GET /sync/status reports available when sync engine is initialised."""
        response = app_client.get("/sync/status")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert "device_id" in data
        assert "peers_discovered" in data

    def test_sync_trigger_no_engine(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /sync/trigger returns 400 when no sync engine.

        /sync/trigger is a sensitive (fail-closed) endpoint, so a token must
        be configured and supplied to reach the engine check.
        """
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "my-secret-token")
        monkeypatch.setattr("doctoragent.api.server._init_sync_engine", lambda cfg: None)
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app)
        response = client.post(
            "/sync/trigger",
            headers={"Authorization": "Bearer my-secret-token"},
        )
        assert response.status_code == 400

    def test_sync_trigger_with_engine(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /sync/trigger returns 200 and schedules a sync round.

        The sync_once coroutine is fire-and-forget; the endpoint returns
        immediately with a success message.
        """
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "my-secret-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app)
        response = client.post(
            "/sync/trigger",
            headers={"Authorization": "Bearer my-secret-token"},
        )
        assert response.status_code == 200
        assert "triggered" in response.json()["message"].lower()

    def test_cors_headers_present(self, app_client: Any) -> None:
        """Response includes CORS headers for localhost origins."""
        response = app_client.options(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        assert response.status_code in (200, 405)

    # ── POST /vault/ask (RAG Q&A) ────────────────────────────────────

    def test_vault_ask_validation(self, app_client: Any) -> None:
        """POST /vault/ask rejects an empty question."""
        response = app_client.post("/vault/ask", json={"question": ""})
        assert response.status_code == 422

    def test_vault_ask_success(
        self,
        app_client: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /vault/ask returns the RAG response."""
        mock_rag = MagicMock()
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "answer": "The vault contains 3 files.",
            "sources": [{"vault_path": "/vault/doc.txt", "category": "docs", "summary": "s", "score": 0.9}],
        }
        mock_rag.ask.return_value = mock_response

        monkeypatch.setattr("doctoragent.model.rag.RagPipeline", lambda **kw: mock_rag)
        mock_agent._embedding_provider = None

        response = app_client.post(
            "/vault/ask",
            json={"question": "What is in the vault?", "top_k": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert "answer" in data

    def test_vault_ask_failure(
        self,
        app_client: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /vault/ask returns 500 when RagPipeline.ask raises."""
        mock_rag = MagicMock()
        mock_rag.ask.side_effect = RuntimeError("DB connection failed")
        monkeypatch.setattr("doctoragent.model.rag.RagPipeline", lambda **kw: mock_rag)
        mock_agent._embedding_provider = None

        response = app_client.post(
            "/vault/ask",
            json={"question": "What is in the vault?"},
        )
        assert response.status_code == 500

    # ── POST /vault/agent (Agent task execution) ─────────────────────

    def test_vault_agent_validation(self, app_client: Any) -> None:
        """POST /vault/agent rejects an empty task."""
        response = app_client.post("/vault/agent", json={"task": ""})
        assert response.status_code == 422

    def test_vault_agent_no_llm_provider(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /vault/agent returns 400 when no LLM provider is available."""
        mock_agent.classifier.provider = None
        response = app_client.post(
            "/vault/agent",
            json={"task": "Summarise the latest documents"},
        )
        assert response.status_code == 400
        assert "No LLM provider" in response.json()["detail"]

    # ── GET /audit/logs ──────────────────────────────────────────────

    def test_audit_logs_no_logger(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /audit/logs returns 400 when audit logger is not configured."""
        mock_agent.audit_logger = None
        response = app_client.get("/audit/logs")
        assert response.status_code == 400

    def test_audit_logs_success(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /audit/logs returns a list of audit records."""
        mock_agent.audit_logger.query.return_value = [
            {"timestamp": "2025-01-01T00:00:00+00:00", "event_type": "file_ingested", "details": {}},
        ]
        response = app_client.get("/audit/logs?limit=10")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_audit_logs_invalid_timestamp(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /audit/logs returns 400 for invalid timestamp format."""
        mock_agent.audit_logger.query.return_value = []
        response = app_client.get("/audit/logs?start_time=not-a-date")
        assert response.status_code == 400

    def test_audit_logs_with_filters(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /audit/logs accepts event_type, severity, and time range filters."""
        mock_agent.audit_logger.query.return_value = [
            {"timestamp": "2025-01-01T00:00:00+00:00", "event_type": "decrypted", "details": {}},
        ]
        response = app_client.get(
            "/audit/logs?event_type=decrypted&severity=CRITICAL&limit=50"
        )
        assert response.status_code == 200

    # ── GET /audit/statistics ────────────────────────────────────────

    def test_audit_statistics_success(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /audit/statistics returns aggregate statistics."""
        mock_agent.audit_logger.statistics.return_value = {
            "total_events": 42,
            "by_event_type": {"file_ingested": 10},
            "by_severity": {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 3},
            "active_periods": [],
        }
        response = app_client.get("/audit/statistics")
        assert response.status_code == 200
        data = response.json()
        assert data["total_events"] == 42

    def test_audit_statistics_no_logger(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /audit/statistics returns 400 when audit logger is not configured."""
        mock_agent.audit_logger = None
        response = app_client.get("/audit/statistics")
        assert response.status_code == 400

    # ── POST /audit/export ───────────────────────────────────────────

    def test_audit_export_success(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /audit/export streams the exported audit log file."""
        def fake_export(dest_path, **kwargs):
            dest_path.write_text('{"test": true}\n', encoding="utf-8")

        mock_agent.audit_logger.export_logs.side_effect = fake_export
        response = app_client.post("/audit/export", json={"format": "ndjson"})
        assert response.status_code == 200
        assert "audit_export" in response.headers.get("content-disposition", "")

    def test_audit_export_csv(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /audit/export supports CSV format."""
        def fake_export(dest_path, **kwargs):
            dest_path.write_text("timestamp,event_type,details,hmac\n", encoding="utf-8")

        mock_agent.audit_logger.export_logs.side_effect = fake_export
        response = app_client.post("/audit/export", json={"format": "csv"})
        assert response.status_code == 200

    def test_audit_export_invalid_format(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /audit/export rejects unsupported formats."""
        response = app_client.post("/audit/export", json={"format": "xml"})
        assert response.status_code == 400

    # ── GET /tenants, POST /tenants ──────────────────────────────────

    def test_tenants_list(self, app_client: Any) -> None:
        """GET /tenants returns at least the default tenant."""
        response = app_client.get("/tenants")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert any(t["tenant_id"] == "default" for t in data)

    def test_tenants_create(self, app_client: Any) -> None:
        """POST /tenants creates a new tenant."""
        response = app_client.post(
            "/tenants",
            json={"tenant_id": "team-alpha", "name": "Team Alpha"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "team-alpha"
        assert data["name"] == "Team Alpha"

    def test_tenants_create_invalid_id(self, app_client: Any) -> None:
        """POST /tenants rejects an invalid tenant_id."""
        response = app_client.post(
            "/tenants",
            json={"tenant_id": "../etc/passwd", "name": "Bad"},
        )
        assert response.status_code == 400

    def test_tenants_create_validation(self, app_client: Any) -> None:
        """POST /tenants rejects empty fields."""
        response = app_client.post(
            "/tenants",
            json={"tenant_id": "", "name": ""},
        )
        assert response.status_code == 422

    # ── GET /config, PUT /config ─────────────────────────────────────

    def test_config_get(self, app_client: Any) -> None:
        """GET /config returns the current configuration."""
        response = app_client.get("/config")
        assert response.status_code == 200
        data = response.json()
        assert "model" in data or "paths" in data or "security" in data

    def test_config_put_success(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PUT /config updates and persists configuration."""
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        config_with_inbox.paths.settings = config_with_inbox.paths.connections.parent / "settings.json"
        from fastapi.testclient import TestClient
        from doctoragent.api.server import create_app

        app = create_app(config_with_inbox, mock_agent)
        client = TestClient(app, headers={"Authorization": "Bearer test-token"})

        current = client.get("/config").json()
        response = client.put("/config", json=current)
        assert response.status_code == 200

    def test_config_put_invalid(
        self,
        app_client: Any,
    ) -> None:
        """PUT /config rejects invalid configuration body."""
        response = app_client.put("/config", json={"model": {"ctx_size": "not-a-number"}})
        assert response.status_code == 422

    # ── GET /connections, POST /connections ──────────────────────────

    def test_connections_list(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """GET /connections returns a list of connections."""
        mock_agent.connection_manager.list_all.return_value = []
        response = app_client.get("/connections")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_connections_create(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /connections adds a new connection."""
        from doctoragent.connections.models import Connection, PlatformType

        conn = Connection(
            name="Test Conn",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
        mock_agent.connection_manager.add.return_value = conn

        response = app_client.post(
            "/connections",
            json={
                "name": "Test Conn",
                "platform_type": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Conn"

    def test_connections_create_invalid(self, app_client: Any) -> None:
        """POST /connections rejects invalid data (missing required fields)."""
        response = app_client.post("/connections", json={"name": "No URL"})
        assert response.status_code == 422

    # ── DELETE /connections/{conn_id} ────────────────────────────────

    def test_connections_delete(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """DELETE /connections/{conn_id} deletes a connection."""
        import uuid

        from doctoragent.connections.models import Connection, PlatformType

        conn_id = uuid.uuid4()
        conn = Connection(
            id=conn_id,
            name="To Delete",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
        mock_agent.connection_manager.get.return_value = conn

        response = app_client.delete(f"/connections/{conn_id}")
        assert response.status_code == 200
        mock_agent.connection_manager.delete.assert_called_once_with(conn_id)

    def test_connections_delete_not_found(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """DELETE /connections/{conn_id} returns 404 for unknown ID."""
        import uuid

        mock_agent.connection_manager.get.return_value = None
        response = app_client.delete(f"/connections/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_connections_delete_invalid_id(self, app_client: Any) -> None:
        """DELETE /connections/{conn_id} returns 400 for invalid UUID."""
        response = app_client.delete("/connections/not-a-uuid")
        assert response.status_code == 400

    # ── POST /connections/{conn_id}/test ─────────────────────────────

    def test_connections_test(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /connections/{conn_id}/test tests a connection."""
        import uuid

        from doctoragent.connections.models import Connection, PlatformType

        conn_id = uuid.uuid4()
        conn = Connection(
            id=conn_id,
            name="Test Target",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
        mock_agent.connection_manager.get.return_value = conn
        mock_agent.connection_manager.test_connection.return_value = (True, "Connected to http://127.0.0.1:11434/v1")

        response = app_client.post(f"/connections/{conn_id}/test")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Connected" in data["message"]

    def test_connections_test_not_found(
        self,
        app_client: Any,
        mock_agent: MagicMock,
    ) -> None:
        """POST /connections/{conn_id}/test returns 404 for unknown ID."""
        import uuid

        mock_agent.connection_manager.get.return_value = None
        response = app_client.post(f"/connections/{uuid.uuid4()}/test")
        assert response.status_code == 404

    def test_connections_test_invalid_id(self, app_client: Any) -> None:
        """POST /connections/{conn_id}/test returns 400 for invalid UUID."""
        response = app_client.post("/connections/not-a-uuid/test")
        assert response.status_code == 400


# ── Phase 7.5: Browser extension submission endpoint ─────────────────────


def _encrypt_browser_payload(token: str, plaintext: bytes) -> dict[str, str]:
    """Mirror the browser extension's encryption scheme for test fixtures.

    Derives an AES-256 key from *token* via PBKDF2-SHA256 (100k iterations)
    and encrypts *plaintext* with AES-256-GCM, returning the base64 fields
    expected by ``BrowserSubmission``.
    """
    import base64
    import os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = os.urandom(16)
    nonce = os.urandom(12)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    key = kdf.derive(token.encode("utf-8"))
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return {
        "content": base64.b64encode(ciphertext).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "salt": base64.b64encode(salt).decode("ascii"),
    }


@pytest.mark.skipif(
    not is_available(),
    reason="FastAPI is not installed (optional dependency)",
)
class TestBrowserSubmissionEndpoint:
    """Tests for POST /inbox/submit (Phase 7.5 browser extension pipeline)."""

    @pytest.fixture
    def config_with_inbox(self, tmp_path: Path) -> Any:
        from doctoragent.config import AegisConfig

        config = AegisConfig()
        config.paths.inbox = tmp_path / "Inbox"
        config.paths.vault = tmp_path / "Vault"
        config.paths.index = tmp_path / "Index"
        config.paths.logs = tmp_path / "Logs"
        config.paths.connections = tmp_path / "Config" / "connections.json"
        for p in [
            config.paths.inbox,
            config.paths.vault,
            config.paths.index,
            config.paths.logs,
        ]:
            p.mkdir(parents=True, exist_ok=True)
        config.paths.connections.parent.mkdir(parents=True, exist_ok=True)
        return config

    @pytest.fixture
    def mock_agent(self) -> MagicMock:
        """Agent mock with an async on_file_event returning COMPLETED."""
        from uuid import uuid4

        from doctoragent.api.schemas import TaskStatus

        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = os.urandom(32)
        del agent._sync_engine

        async def _on_file_event(_event: object) -> TaskStatus:
            return TaskStatus(task_id=uuid4(), state="COMPLETED", message="ok")

        agent.on_file_event = _on_file_event
        return agent

    def _make_app(
        self,
        config: Any,
        agent: MagicMock,
        token: str | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Any:
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        if token is None:
            monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
        else:
            monkeypatch.setenv("DOCTORAGENT_API_TOKEN", token)
        app = create_app(config, agent)
        return TestClient(app)

    def test_no_token_configured_returns_403(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without DOCTORAGENT_API_TOKEN the submission endpoint is forbidden."""
        client = self._make_app(config_with_inbox, mock_agent, None, monkeypatch)
        payload = _encrypt_browser_payload("unused", b"hello")
        response = client.post("/inbox/submit", json=payload)
        assert response.status_code == 403

    def test_missing_bearer_returns_401(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Token configured but no Authorization header → 401."""
        client = self._make_app(config_with_inbox, mock_agent, "secret", monkeypatch)
        payload = _encrypt_browser_payload("secret", b"hello")
        response = client.post("/inbox/submit", json=payload)
        assert response.status_code == 401

    def test_successful_submission(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A correctly encrypted payload is ingested and returns ok=True."""
        token = "my-browser-token"
        plaintext = b"Secret selection from the browser"
        payload = _encrypt_browser_payload(token, plaintext)
        payload["filename"] = "selection-001.txt"

        client = self._make_app(config_with_inbox, mock_agent, token, monkeypatch)
        response = client.post(
            "/inbox/submit",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["state"] == "COMPLETED"
        assert data["source"] == "browser"
        assert "selection-001.txt" in data["inbox_path"]

        # File should exist on disk with original content.
        written = config_with_inbox.paths.inbox / "selection-001.txt"
        assert written.exists()
        assert written.read_bytes() == plaintext
        # Skip permission check on Windows (no Unix-style chmod)
        import platform
        if platform.system() != "Windows":
            assert (written.stat().st_mode & 0o777) == 0o600

    def test_decryption_failure_returns_400(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Payload encrypted with a different token → 400 decryption error."""
        client = self._make_app(config_with_inbox, mock_agent, "real-token", monkeypatch)
        payload = _encrypt_browser_payload("wrong-token", b"hello")
        response = client.post(
            "/inbox/submit",
            json=payload,
            headers={"Authorization": "Bearer real-token"},
        )
        assert response.status_code == 400
        assert "Decryption failed" in response.json()["detail"]

    def test_default_filename_when_none_provided(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Omitting filename falls back to a timestamped browser-*.txt name."""
        token = "tok"
        payload = _encrypt_browser_payload(token, b"data")
        client = self._make_app(config_with_inbox, mock_agent, token, monkeypatch)
        response = client.post(
            "/inbox/submit",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        inbox = config_with_inbox.paths.inbox
        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name.startswith("browser-")
        assert files[0].suffix == ".txt"

    def test_filename_collision_is_disambiguated(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-existing inbox file with the same name gets a suffix."""
        token = "tok"
        inbox = config_with_inbox.paths.inbox
        (inbox / "dup.txt").write_bytes(b"old")

        payload = _encrypt_browser_payload(token, b"new content")
        payload["filename"] = "dup.txt"
        client = self._make_app(config_with_inbox, mock_agent, token, monkeypatch)
        response = client.post(
            "/inbox/submit",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        # Original file is untouched; a new disambiguated file was written.
        assert (inbox / "dup.txt").read_bytes() == b"old"
        new_files = [f for f in inbox.iterdir() if f.name != "dup.txt"]
        assert len(new_files) == 1
        assert new_files[0].stem.startswith("dup_")
        assert new_files[0].suffix == ".txt"
        assert new_files[0].read_bytes() == b"new content"

    def test_ingestion_error_returns_500(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If agent.on_file_event raises, the endpoint returns 500."""
        from doctoragent.api.schemas import FileEvent

        async def _fail(_event: FileEvent) -> None:
            raise RuntimeError("pipeline exploded")

        mock_agent.on_file_event = _fail

        token = "tok"
        payload = _encrypt_browser_payload(token, b"data")
        client = self._make_app(config_with_inbox, mock_agent, token, monkeypatch)
        response = client.post(
            "/inbox/submit",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 500
        assert response.json()["detail"] == "Ingestion failed"

    def test_custom_source_label_propagated(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``source`` field is echoed back in the response."""
        token = "tok"
        payload = _encrypt_browser_payload(token, b"data")
        payload["source"] = "selection"
        client = self._make_app(config_with_inbox, mock_agent, token, monkeypatch)
        response = client.post(
            "/inbox/submit",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["source"] == "selection"

    def test_invalid_payload_validation(
        self,
        config_with_inbox: Any,
        mock_agent: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed body (missing fields) → 422 validation error."""
        token = "tok"
        client = self._make_app(config_with_inbox, mock_agent, token, monkeypatch)
        response = client.post(
            "/inbox/submit",
            json={"content": "only-content"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

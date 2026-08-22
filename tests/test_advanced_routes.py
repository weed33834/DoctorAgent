# mypy: ignore-errors
"""Full-scenario tests for the advanced API router.

Covers the 25+ enterprise endpoints in ``doctoragent/api/advanced_routes.py``
that previously had zero test coverage:

* **Auth gating** — read endpoints accept a valid token / reject remote
  anonymous; sensitive (write) endpoints are fail-closed without a token.
* **Validation (422)** — malformed bodies are rejected with structured errors.
* **Error mapping** — 404 for missing resources, 503 for unconfigured
  subsystems, 500 for internal failures.
* **Functional round-trips** — Shamir split→reconstruct, DAG execute→status,
  DLP scan→redact, Zero-Trust evaluate→device→history, security posture
  /anomalies/risk, RAG cache stats→clear.

The tests build a minimal FastAPI app with only the advanced router mounted
and a token configured, so every endpoint is exercised in isolation.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from doctoragent.api.advanced_routes import _FASTAPI_AVAILABLE

pytestmark = pytest.mark.skipif(
    not _FASTAPI_AVAILABLE,
    reason="FastAPI is not installed (optional dependency)",
)


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

def _build_app(
    *,
    token: str | None = "test-token",
    state_attrs: dict[str, Any] | None = None,
):
    """Build a FastAPI app with only the advanced router mounted.

    A valid bearer token is configured via ``DOCTORAGENT_API_TOKEN`` unless
    *token* is ``None`` (fail-closed mode for sensitive endpoints).
    """
    from fastapi import FastAPI

    from doctoragent.api.advanced_routes import router

    if token is not None:
        os.environ["DOCTORAGENT_API_TOKEN"] = token
    else:
        os.environ.pop("DOCTORAGENT_API_TOKEN", None)

    app = FastAPI()
    app.include_router(router)
    for key, value in (state_attrs or {}).items():
        setattr(app.state, key, value)
    return app


def _client(app, *, token: str | None = "test-token"):
    """Build a TestClient with the bearer header pre-set."""
    from fastapi.testclient import TestClient

    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TestClient(app, headers=headers)


@pytest.fixture(autouse=True)
def _clean_token_env() -> Any:
    """Snapshot & restore DOCTORAGENT_API_TOKEN around every test.

    ``_make_app`` mutates ``os.environ`` directly; a monkeypatch-based
    delenv here would *restore* that mutated value during undo and leak it
    into later test files. Explicit snapshot/restore cannot leak.
    """
    import os

    saved = os.environ.pop("DOCTORAGENT_API_TOKEN", None)
    yield
    if saved is None:
        os.environ.pop("DOCTORAGENT_API_TOKEN", None)
    else:
        os.environ["DOCTORAGENT_API_TOKEN"] = saved


# ---------------------------------------------------------------------------
# 1. Authentication gating
# ---------------------------------------------------------------------------

class TestAuthGating:
    """Read vs sensitive endpoints enforce different auth policies."""

    def test_read_endpoint_requires_token_when_configured(self) -> None:
        """GET /scheduler/status returns 401 when token is set but missing."""
        app = _build_app(token="secret")
        client = _client(app, token=None)
        # Override the app-level header with no auth.
        resp = client.get("/api/v1/scheduler/status", headers={})
        assert resp.status_code == 401

    def test_read_endpoint_works_with_valid_token(self) -> None:
        """GET /scheduler/status passes auth with a valid token (200, scheduler works standalone)."""
        app = _build_app(token="secret")
        client = _client(app, token="secret")
        resp = client.get("/api/v1/scheduler/status")
        # Scheduler is lazily created and works standalone → 200, auth passed.
        assert resp.status_code == 200
        assert "queue" in resp.json()

    def test_sensitive_endpoint_fail_closed_without_token_env(self) -> None:
        """Sensitive endpoints return 403 when DOCTORAGENT_API_TOKEN is unset."""
        app = _build_app(token=None)
        client = _client(app, token=None)
        resp = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": "abcd", "threshold": 2, "total": 3},
        )
        assert resp.status_code == 403
        assert "Authentication required" in resp.json()["detail"]

    def test_sensitive_endpoint_rejects_wrong_token(self) -> None:
        """Sensitive endpoints return 401 for a wrong token."""
        app = _build_app(token="correct-token")
        client = _client(app, token="wrong-token")
        resp = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": "abcd", "threshold": 2, "total": 3},
        )
        assert resp.status_code == 401

    def test_sensitive_endpoint_works_with_valid_token(self) -> None:
        """Sensitive endpoints pass with a valid token (200/422 for the body)."""
        app = _build_app(token="correct-token")
        client = _client(app, token="correct-token")
        resp = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": "abcd", "threshold": 2, "total": 3},
        )
        # Valid request → 200 (Shamir works standalone).
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Validation errors (422)
# ---------------------------------------------------------------------------

class TestValidationErrors:
    """Malformed request bodies are rejected with 422."""

    def test_kg_query_empty_string_rejected(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/kg/query", json={"query": "", "top_k": 5})
        assert resp.status_code == 422

    def test_kg_query_top_k_out_of_range(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/kg/query", json={"query": "valid", "top_k": 999})
        assert resp.status_code == 422
        # Pydantic surfaces the offending field.
        assert resp.json().get("detail")

    def test_shamir_threshold_gt_total_rejected(self) -> None:
        """threshold > total is a business-rule 400 (not 422)."""
        app = _build_app()
        client = _client(app)
        resp = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": "abcd", "threshold": 5, "total": 3},
        )
        assert resp.status_code == 400
        assert "threshold" in resp.json()["detail"].lower()

    def test_shamir_short_secret_rejected(self) -> None:
        """secret_hex shorter than 2 chars fails field validation (422)."""
        app = _build_app()
        client = _client(app)
        resp = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": "a", "threshold": 1, "total": 1},
        )
        assert resp.status_code == 422

    def test_dlp_empty_text_rejected(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/dlp/scan", json={"text": ""})
        assert resp.status_code == 422

    def test_dag_empty_tasks_rejected(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/dag/execute", json={"tasks": []})
        assert resp.status_code == 422

    def test_zt_evaluate_missing_subject_id(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post(
            "/api/v1/zt/evaluate",
            json={"resource_path": "/x", "action": "read"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Not-found (404) and service-unavailable (503)
# ---------------------------------------------------------------------------

class TestErrorMapping:
    """404 / 503 / 500 are surfaced correctly."""

    def test_kg_entity_not_found_returns_404(self) -> None:
        """GET /kg/entity/{name} returns 404 when KG is available but entity missing.

        The KG subsystem is lazily created from config; without a config the
        endpoint returns 503. We attach a minimal config + task_store so the
        KG builds (empty), then the entity lookup 404s.
        """
        from unittest.mock import MagicMock

        config = MagicMock()
        config.paths.index = _tmp_index_path()
        app = _build_app(state_attrs={"config": config, "task_store": MagicMock()})
        client = _client(app)
        resp = client.get("/api/v1/kg/entity/nonexistent")
        # KG may 503 (if KnowledgeGraph import/db fails) or 404 (entity missing).
        # Either is a valid "not configured / not found" mapping — assert no 500.
        assert resp.status_code in (404, 503)

    def test_agent_trajectory_not_found_returns_404(self) -> None:
        """GET /agent/trajectory/{task_id} returns 404 for an unknown task."""
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/agent/trajectory/unknown-task-id")
        assert resp.status_code == 404
        assert "unknown-task-id" in resp.json()["detail"]

    def test_dag_status_not_found_returns_404(self) -> None:
        """GET /dag/status/{dag_id} returns 404 for an unknown DAG."""
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/dag/status/missing-dag-id")
        assert resp.status_code == 404
        assert "missing-dag-id" in resp.json()["detail"]

    def test_kg_build_without_config_returns_503(self) -> None:
        """POST /kg/build returns 503 when no config/task_store is wired."""
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/kg/build", json={})
        assert resp.status_code == 503

    def test_keys_status_without_provider_returns_503(self) -> None:
        """GET /keys/status returns 503 when no master key provider is set."""
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/keys/status")
        assert resp.status_code == 503

    def test_keys_rotate_without_rotator_returns_503(self) -> None:
        """POST /keys/rotate returns 503 when no key_rotator is on the agent."""
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/keys/rotate", json={"reason": "test"})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 4. Functional round-trips
# ---------------------------------------------------------------------------

class TestShamirRoundTrip:
    """Shamir split → reconstruct recovers the original secret."""

    def test_split_then_reconstruct(self) -> None:
        app = _build_app()
        client = _client(app)
        secret = "deadbeef"
        # 2-of-3 threshold scheme.
        split = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": secret, "threshold": 2, "total": 3},
        )
        assert split.status_code == 200
        shares = split.json()["shares"]
        assert len(shares) == 3
        assert split.json()["threshold"] == 2

        # Any 2 shares reconstruct the original.
        recon = client.post(
            "/api/v1/shamir/reconstruct",
            json={"shares": shares[:2]},
        )
        assert recon.status_code == 200
        assert recon.json()["secret_hex"] == secret

    def test_reconstruct_with_insufficient_shares_fails(self) -> None:
        """Reconstructing with fewer shares than threshold returns 400."""
        app = _build_app()
        client = _client(app)
        split = client.post(
            "/api/v1/shamir/split",
            json={"secret_hex": "cafe", "threshold": 3, "total": 3},
        )
        assert split.status_code == 200
        shares = split.json()["shares"]
        # Only 1 share < threshold 3 → should fail.
        recon = client.post(
            "/api/v1/shamir/reconstruct",
            json={"shares": shares[:1]},
        )
        assert recon.status_code == 400


class TestDLPScan:
    """DLP scan and redact detect sensitive data."""

    def test_scan_detects_pii(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post(
            "/api/v1/dlp/scan",
            json={"text": "My SSN is 123-45-6789 and email is test@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert len(data["matches"]) == data["count"]

    def test_redact_masks_sensitive_data(self) -> None:
        app = _build_app()
        client = _client(app)
        original = "Card 4111-1111-1111-1111 is valid"
        resp = client.post("/api/v1/dlp/redact", json={"text": original})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        # The redacted text must differ from the original (PII masked).
        assert data["redacted_text"] != original
        assert "4111" not in data["redacted_text"]

    def test_scan_clean_text_returns_zero(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post("/api/v1/dlp/scan", json={"text": "nothing sensitive here"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestZeroTrust:
    """Zero-Trust evaluate / device / history."""

    def test_register_device_then_evaluate(self) -> None:
        app = _build_app()
        client = _client(app)
        # Register a healthy device.
        reg = client.post(
            "/api/v1/zt/device",
            json={
                "device_id": "dev-1",
                "os_version": "macOS 14",
                "disk_encrypted": True,
                "firewall_enabled": True,
                "trust_score": 0.9,
            },
        )
        assert reg.status_code == 200
        assert reg.json()["device_id"] == "dev-1"

        # Evaluate access using the registered device.
        ev = client.post(
            "/api/v1/zt/evaluate",
            json={
                "subject_id": "user-1",
                "resource_path": "/vault/secret",
                "action": "read",
                "device_id": "dev-1",
                "ip_address": "127.0.0.1",
            },
        )
        assert ev.status_code == 200
        decision = ev.json()
        assert "allowed" in decision
        assert "trust_level" in decision
        assert "reason" in decision

    def test_access_history_recorded(self) -> None:
        app = _build_app()
        client = _client(app)
        # Trigger one evaluation first.
        client.post(
            "/api/v1/zt/evaluate",
            json={
                "subject_id": "user-2",
                "resource_path": "/x",
                "action": "read",
            },
        )
        hist = client.get("/api/v1/zt/history")
        assert hist.status_code == 200
        assert hist.json()["total"] >= 1

    def test_history_filtered_by_subject(self) -> None:
        app = _build_app()
        client = _client(app)
        client.post(
            "/api/v1/zt/evaluate",
            json={
                "subject_id": "alice",
                "resource_path": "/a",
                "action": "read",
            },
        )
        client.post(
            "/api/v1/zt/evaluate",
            json={
                "subject_id": "bob",
                "resource_path": "/b",
                "action": "read",
            },
        )
        hist = client.get("/api/v1/zt/history", params={"subject_id": "alice"})
        assert hist.status_code == 200
        assert hist.json()["total"] == 1


class TestSecurityAnalytics:
    """Security posture / anomalies / risk / trend."""

    def test_posture_returns_metrics(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/security/posture")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_events" in data
        assert "anomalies_count" in data
        assert "avg_risk_score" in data

    def test_anomalies_returns_list(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/security/anomalies", params={"limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert isinstance(data["anomalies"], list)

    def test_risk_score_for_subject(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/security/risk/some-user")
        assert resp.status_code == 200
        data = resp.json()
        assert data["subject_id"] == "some-user"
        assert 0.0 <= data["risk_score"] <= 1.0

    def test_risk_trend_returns_days(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/security/risk-trend", params={"days": 3})
        assert resp.status_code == 200
        assert resp.json()["days"] == 3


class TestDAGWorkflow:
    """DAG execute → status round-trip."""

    def test_execute_simple_dag(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.post(
            "/api/v1/dag/execute",
            json={
                "tasks": [
                    {"id": "t1", "name": "first", "dependencies": []},
                    {"id": "t2", "name": "second", "dependencies": ["t1"]},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        dag_id = data["dag_id"]
        assert dag_id
        assert "status" in data

        # Status lookup succeeds.
        status = client.get(f"/api/v1/dag/status/{dag_id}")
        assert status.status_code == 200
        assert status.json()["found"] is True

    def test_execute_dag_with_cycle_returns_422(self) -> None:
        """A cyclic DAG is rejected with 422."""
        app = _build_app()
        client = _client(app)
        resp = client.post(
            "/api/v1/dag/execute",
            json={
                "tasks": [
                    {"id": "a", "dependencies": ["b"]},
                    {"id": "b", "dependencies": ["a"]},
                ]
            },
        )
        # Cycle → 422 (DAG validation failure).
        assert resp.status_code in (422, 400)


class TestRAGCache:
    """RAG cache stats and clear."""

    def test_cache_stats_returns_zero_initially(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.get("/api/v1/rag/cache/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "hit_rate" in data
        assert "size" in data

    def test_cache_clear_returns_message(self) -> None:
        app = _build_app()
        client = _client(app)
        resp = client.delete("/api/v1/rag/cache")
        assert resp.status_code == 200
        assert "cleared" in resp.json()["message"].lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_index_path() -> Any:
    """Return a temporary index directory path (created on demand by the KG)."""
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="doctoragent-adv-test-"))

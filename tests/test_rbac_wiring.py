"""Tests: RBAC admin gate is actually wired onto management endpoints.

v0.3.7 wires ``require_role(Role.ADMIN)`` onto the highest-risk management
endpoints (tenant creation, master-key rotation, enterprise settings /
maintenance / API-key creation). Semantics:

* OIDC-authenticated user → per-user role check (existing behaviour).
* Static ``DOCTORAGENT_API_TOKEN`` → service account, allowed (a shared
  token cannot carry individual roles; documented trade-off).
* Local/unauthenticated requests → 403 fail-closed.

The auth dependencies mark ``request.state.auth_method`` so the RBAC layer
can distinguish a *verified static token* from an anonymous local request.
"""

from __future__ import annotations

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
class TestRbacWiring:
    """Admin-gated endpoints enforce the role dependency."""

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
    def anon_client(self, config: AegisConfig, mock_agent: MagicMock) -> Any:
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        return TestClient(create_app(config, mock_agent))

    @pytest.fixture
    def token_client(
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

    # ── POST /api/v1/tenants (admin) ──────────────────────────────────

    def test_tenants_create_denied_anonymous(self, anon_client: Any) -> None:
        response = anon_client.post(
            "/api/v1/tenants", json={"tenant_id": "t1", "name": "T1"}
        )
        assert response.status_code == 403

    def test_tenants_create_allowed_for_service_account(
        self, token_client: Any
    ) -> None:
        response = token_client.post(
            "/api/v1/tenants", json={"tenant_id": "t1", "name": "T1"}
        )
        # Reaches the handler: 200 on success or 4xx validation — never 403.
        assert response.status_code != 403

    # ── POST /api/v1/keys/rotate (admin) ──────────────────────────────

    def test_keys_rotate_denied_anonymous(self, anon_client: Any) -> None:
        response = anon_client.post("/api/v1/keys/rotate", json={"reason": "x"})
        assert response.status_code in (403, 503)

    def test_keys_rotate_passes_rbac_with_token(self, token_client: Any) -> None:
        response = token_client.post("/api/v1/keys/rotate", json={"reason": "x"})
        assert response.status_code != 403

    # ── enterprise admin endpoints ─────────────────────────────────────

    def test_enterprise_settings_put_denied_anonymous(self, anon_client: Any) -> None:
        response = anon_client.put(
            "/api/v1/enterprise/settings", json={"key": "value"}
        )
        # 401 (TestClient source host is non-loopback → remote) or 403
        # (local anonymous) — either way the write is denied.
        assert response.status_code in (401, 403)

    def test_enterprise_settings_put_allowed_service_account(
        self, token_client: Any
    ) -> None:
        response = token_client.put(
            "/api/v1/enterprise/settings", json={"k": "v"}
        )
        # Enterprise service may be unavailable in bare fixtures (503);
        # what must NOT happen is an RBAC 403 for the service account.
        assert response.status_code != 403

    def test_enterprise_apikeys_post_denied_anonymous(self, anon_client: Any) -> None:
        response = anon_client.post(
            "/api/v1/enterprise/apikeys", json={"label": "k"}
        )
        assert response.status_code in (401, 403)


class TestRequireRoleSemantics:
    """Unit tests for the require_role closure itself."""

    @staticmethod
    def _request(auth_method: str | None = None, roles: list[str] | None = None) -> Any:
        class _State:
            pass

        state = _State()
        if auth_method is not None:
            state.auth_method = auth_method
        if roles is not None:
            state.user = type("U", (), {"roles": roles})()

        class _Req:
            pass

        req = _Req()
        req.state = state
        return req

    @pytest.mark.asyncio
    async def test_oidc_user_with_role_passes(self) -> None:
        pytest.importorskip("fastapi")
        from doctoragent.api.auth.rbac import Role, require_role

        dep = require_role(Role.ADMIN)
        result = await dep(self._request(roles=["admin"]))
        assert result is not None

    @pytest.mark.asyncio
    async def test_oidc_user_without_role_denied(self) -> None:
        pytest.importorskip("fastapi")
        import fastapi

        from doctoragent.api.auth.rbac import Role, require_role

        dep = require_role(Role.ADMIN)
        with pytest.raises(fastapi.HTTPException) as excinfo:
            await dep(self._request(roles=["viewer"]))
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_static_token_is_service_account(self) -> None:
        pytest.importorskip("fastapi")
        from doctoragent.api.auth.rbac import Role, require_role

        dep = require_role(Role.ADMIN)
        result = await dep(self._request(auth_method="static_token"))
        assert result is None

    @pytest.mark.asyncio
    async def test_local_anonymous_denied(self) -> None:
        pytest.importorskip("fastapi")
        import fastapi

        from doctoragent.api.auth.rbac import Role, require_role

        dep = require_role(Role.ADMIN)
        with pytest.raises(fastapi.HTTPException) as excinfo:
            await dep(self._request(auth_method="local"))
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_auth_marker_denied(self) -> None:
        pytest.importorskip("fastapi")
        import fastapi

        from doctoragent.api.auth.rbac import Role, require_role

        dep = require_role(Role.ADMIN)
        with pytest.raises(fastapi.HTTPException) as excinfo:
            await dep(self._request())
        assert excinfo.value.status_code == 403

"""Tests for the enterprise RBAC authorization layer (``doctoragent.api.auth``)."""

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

import pytest

from doctoragent.api.auth import (
    OIDCAuthenticator,
    Permission,
    RBACAuthorizer,
    Role,
    require_role,
)
from doctoragent.api.auth import oidc as oidc_module

_FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


# ── Default policy matrix ────────────────────────────────────────────────────


class TestRBACDefaultPolicy:
    """The default permission matrix must match the documented contract."""

    @pytest.fixture
    def authorizer(self) -> RBACAuthorizer:
        return RBACAuthorizer()

    def test_admin_can_admin(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("admin", "vault", "admin") is True

    def test_admin_can_everything(self, authorizer: RBACAuthorizer) -> None:
        """ADMIN holds the wildcard permission: every action is allowed."""
        for action in ("read", "write", "delete", "admin", "audit"):
            assert authorizer.check("admin", "vault", action) is True

    def test_viewer_cannot_write(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("viewer", "vault", "write") is False

    def test_viewer_can_read(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("viewer", "vault", "read") is True

    def test_auditor_can_audit(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("auditor", "vault", "audit") is True

    def test_auditor_cannot_delete(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("auditor", "vault", "delete") is False

    def test_auditor_can_read(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("auditor", "vault", "read") is True

    def test_editor_can_read_and_write(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("editor", "vault", "read") is True
        assert authorizer.check("editor", "vault", "write") is True

    def test_editor_cannot_delete_or_admin(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("editor", "vault", "delete") is False
        assert authorizer.check("editor", "vault", "admin") is False

    def test_role_is_case_insensitive(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("ADMIN", "vault", "admin") is True
        assert authorizer.check("Viewer", "vault", "read") is True

    def test_unknown_role_is_denied(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check("superuser", "vault", "read") is False

    def test_none_role_is_denied(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.check(None, "vault", "read") is False  # type: ignore[arg-type]

    def test_get_permissions_admin(self, authorizer: RBACAuthorizer) -> None:
        perms = authorizer.get_permissions("admin")
        assert set(perms) == set(Permission)

    def test_get_permissions_auditor(self, authorizer: RBACAuthorizer) -> None:
        perms = authorizer.get_permissions("auditor")
        assert set(perms) == {Permission.READ, Permission.AUDIT}

    def test_get_permissions_viewer(self, authorizer: RBACAuthorizer) -> None:
        perms = authorizer.get_permissions("viewer")
        assert set(perms) == {Permission.READ}

    def test_get_permissions_unknown_role(self, authorizer: RBACAuthorizer) -> None:
        assert authorizer.get_permissions("ghost") == []

    def test_resource_does_not_affect_decision(self, authorizer: RBACAuthorizer) -> None:
        """The default policy is role+action only; the resource is irrelevant."""
        assert authorizer.check("viewer", "vault", "read") is True
        assert authorizer.check("viewer", "audit-log", "read") is True
        assert authorizer.check("viewer", "config", "write") is False


# ── Static fallback (casbin disabled) ────────────────────────────────────────


class TestRBACStaticFallback:
    """When casbin is unavailable the static matrix must produce identical results."""

    def test_static_fallback_matches_casbin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the fallback path and build a fresh authorizer so its enforcer
        # is None.
        monkeypatch.setattr(
            "doctoragent.api.auth.rbac._CASBIN_AVAILABLE", False, raising=True
        )
        from doctoragent.api.auth.rbac import RBACAuthorizer as FreshAuthorizer

        static = FreshAuthorizer()
        assert static._enforcer is None

        matrix = {
            ("admin", "vault", "admin"): True,
            ("admin", "vault", "delete"): True,
            ("viewer", "vault", "write"): False,
            ("viewer", "vault", "read"): True,
            ("auditor", "vault", "audit"): True,
            ("auditor", "vault", "delete"): False,
            ("editor", "vault", "write"): True,
            ("editor", "vault", "delete"): False,
        }
        for (role, resource, action), expected in matrix.items():
            assert static.check(role, resource, action) is expected, (role, resource, action)

    def test_static_fallback_unknown_action_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "doctoragent.api.auth.rbac._CASBIN_AVAILABLE", False, raising=True
        )
        from doctoragent.api.auth.rbac import RBACAuthorizer as FreshAuthorizer

        static = FreshAuthorizer()
        # Non-admin role + unknown action → denied (not a valid Permission).
        assert static.check("viewer", "vault", "teleport") is False
        # ADMIN has wildcard → unknown action allowed for admin only.
        assert static.check("admin", "vault", "teleport") is True


# ── Custom policy ────────────────────────────────────────────────────────────


class TestRBACCustomPolicy:
    """A caller-supplied policy overrides the default matrix."""

    def test_custom_policy_overrides_default(self) -> None:
        custom = {
            Role.VIEWER: frozenset({Permission.READ, Permission.DELETE}),
        }
        authorizer = RBACAuthorizer(policy=custom)
        # Viewer now can delete (custom grants it)…
        assert authorizer.check("viewer", "vault", "delete") is True
        # …but ADMIN is no longer in the policy map, so it is denied.
        assert authorizer.check("admin", "vault", "admin") is False


# ── require_role FastAPI dependency ──────────────────────────────────────────


def _make_request(user: object | None) -> SimpleNamespace:
    """Build a minimal stand-in for a Starlette Request."""
    return SimpleNamespace(state=SimpleNamespace(user=user))


class TestRequireRoleDependency:
    """require_role produces a FastAPI dependency that gates on request.state.user."""

    def test_matching_role_returns_user(self) -> None:
        dep = require_role(Role.ADMIN)
        user = SimpleNamespace(roles=["admin"], sub="u1")
        request = _make_request(user)
        result = asyncio.run(dep(request))
        assert result is user

    def test_matching_role_among_many(self) -> None:
        dep = require_role(Role.EDITOR, Role.AUDITOR)
        user = SimpleNamespace(roles=["viewer", "editor"], sub="u2")
        request = _make_request(user)
        assert asyncio.run(dep(request)) is user

    def test_non_matching_role_raises_403(self) -> None:
        from fastapi import HTTPException

        dep = require_role(Role.ADMIN)
        user = SimpleNamespace(roles=["viewer"], sub="u3")
        request = _make_request(user)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(dep(request))
        assert exc.value.status_code == 403

    def test_no_user_raises_403(self) -> None:
        from fastapi import HTTPException

        dep = require_role(Role.ADMIN)
        request = _make_request(None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(dep(request))
        assert exc.value.status_code == 403

    def test_user_without_roles_raises_403(self) -> None:
        from fastapi import HTTPException

        dep = require_role(Role.AUDITOR)
        user = SimpleNamespace(roles=[])
        request = _make_request(user)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(dep(request))
        assert exc.value.status_code == 403

    def test_role_matching_is_case_insensitive(self) -> None:
        dep = require_role(Role.ADMIN)
        user = SimpleNamespace(roles=["ADMIN"], sub="u4")
        request = _make_request(user)
        assert asyncio.run(dep(request)) is user


# ── OIDC authenticator unit tests (role mapping + graceful import) ───────────


class TestOIDCRoleMapping:
    """Role-claim mapping follows the documented defaults."""

    def test_empty_claims_defaults_to_viewer(self) -> None:
        assert OIDCAuthenticator._map_roles({}) == [Role.VIEWER.value]

    def test_roles_list_claim(self) -> None:
        assert OIDCAuthenticator._map_roles({"roles": ["admin", "editor"]}) == [
            "admin",
            "editor",
        ]

    def test_role_string_claim(self) -> None:
        assert OIDCAuthenticator._map_roles({"role": "auditor"}) == ["auditor"]

    def test_realm_access_keycloak_style(self) -> None:
        claims = {"realm_access": {"roles": ["viewer", "admin"]}}
        assert OIDCAuthenticator._map_roles(claims) == ["viewer", "admin"]

    def test_unknown_roles_dropped(self) -> None:
        # 'superuser' is not a local Role → dropped; 'admin' retained.
        assert OIDCAuthenticator._map_roles({"roles": ["superuser", "admin"]}) == [
            "admin"
        ]

    def test_roles_deduplicated(self) -> None:
        claims = {"roles": ["admin"], "role": "admin", "realm_access": {"roles": ["admin"]}}
        assert OIDCAuthenticator._map_roles(claims) == ["admin"]


class TestOIDCGracefulImport:
    """OIDCAuthenticator raises a clear ImportError when authlib is missing."""

    def test_import_error_when_authlib_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(oidc_module, "_AUTHLIB_AVAILABLE", False, raising=True)
        monkeypatch.setattr(
            oidc_module, "_is_authlib_available", lambda: False, raising=True
        )
        with pytest.raises(ImportError, match="doctoragent\\[auth\\]"):
            OIDCAuthenticator(issuer_url="https://idp.example.com", client_id="c")

    def test_empty_issuer_rejected(self) -> None:
        with pytest.raises(ValueError, match="issuer_url"):
            OIDCAuthenticator(issuer_url="", client_id="c")


# ── Server-level OIDC + RBAC integration ─────────────────────────────────────


def _build_oidc_fixtures() -> tuple:
    """Build a self-contained RSA key, JWKS, discovery doc, and token mint.

    Returns ``(issuer, audience, jwks, discovery, make_token)`` where
    ``make_token(roles)`` returns a signed JWT.
    """
    import time
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from authlib.jose import RSAKey
        from authlib.jose import jwt as _jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

    issuer = "https://idp.example.com"
    audience = "doctoragent-client"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    rsa_key = RSAKey.import_key(priv_pem)
    public_jwk = rsa_key.as_dict(is_private=False)
    jwks = {"keys": [public_jwk]}
    discovery = {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/jwks",
        "authorization_endpoint": f"{issuer}/auth",
    }

    def make_token(roles: list[str], sub: str = "user-1") -> str:
        header = {"alg": "RS256", "kid": public_jwk.get("kid")}
        payload = {
            "sub": sub,
            "email": f"{sub}@example.com",
            "iss": issuer,
            "aud": audience,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "roles": roles,
        }
        token = _jwt.encode(header, payload, rsa_key)
        return token.decode() if isinstance(token, bytes) else token

    return issuer, audience, jwks, discovery, make_token


@pytest.mark.skipif(
    not _FASTAPI_AVAILABLE,
    reason="FastAPI is not installed (optional dependency)",
)
class TestOIDCServerIntegration:
    """End-to-end OIDC + RBAC behaviour through the FastAPI app."""

    @pytest.fixture
    def oidc_app_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ):
        """Build an app with OIDC configured and httpx discovery mocked."""
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from doctoragent.api import server as srv
        from doctoragent.config import AegisConfig

        issuer, audience, jwks, discovery, make_token = _build_oidc_fixtures()
        monkeypatch.setenv("DOCTORAGENT_OIDC_ISSUER", issuer)
        monkeypatch.setenv("DOCTORAGENT_OIDC_CLIENT_ID", audience)
        monkeypatch.setenv("DOCTORAGENT_OIDC_AUDIENCE", audience)
        monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
        srv._reset_oidc_state()

        config = AegisConfig()
        config.paths.inbox = tmp_path / "Inbox"
        config.paths.vault = tmp_path / "Vault"
        config.paths.index = tmp_path / "Index"
        config.paths.logs = tmp_path / "Logs"
        config.paths.connections = tmp_path / "Config" / "connections.json"
        for p in [config.paths.inbox, config.paths.vault, config.paths.index, config.paths.logs]:
            p.mkdir(parents=True, exist_ok=True)
        config.paths.connections.parent.mkdir(parents=True, exist_ok=True)

        from unittest.mock import MagicMock

        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = b"\x00" * 32
        del agent._sync_engine
        del agent.search

        async def _search(*a, **k):
            return []

        agent.search = _search

        class _FakeResp:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        def _fake_get(url, **kw):
            if url.endswith(".well-known/openid-configuration"):
                return _FakeResp(discovery)
            if url.endswith("/jwks"):
                return _FakeResp(jwks)
            raise AssertionError(f"unexpected url: {url}")

        with patch("doctoragent.api.auth.oidc.httpx.get", side_effect=_fake_get):
            app = srv.create_app(config, agent)
            client = TestClient(app)
            yield client, make_token

    def test_admin_can_access_admin_roles(self, oidc_app_client) -> None:
        client, make_token = oidc_app_client
        resp = client.get(
            "/api/v1/admin/roles",
            headers={"Authorization": f"Bearer {make_token(['admin'])}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["roles"] == [
            "admin", "editor", "viewer", "auditor",
            "clinician", "pharmacist", "nurse",
        ]
        assert data["current_roles"] == ["admin"]

    def test_viewer_forbidden_from_admin_roles(self, oidc_app_client) -> None:
        client, make_token = oidc_app_client
        resp = client.get(
            "/api/v1/admin/roles",
            headers={"Authorization": f"Bearer {make_token(['viewer'])}"},
        )
        assert resp.status_code == 403, resp.text

    def test_oidc_token_unlocks_existing_read_endpoint(
        self, oidc_app_client
    ) -> None:
        client, make_token = oidc_app_client
        resp = client.get(
            "/api/v1/vault/status",
            headers={"Authorization": f"Bearer {make_token(['admin'])}"},
        )
        assert resp.status_code == 200, resp.text

    def test_missing_token_returns_401(self, oidc_app_client) -> None:
        client, _ = oidc_app_client
        resp = client.get("/api/v1/vault/status")
        assert resp.status_code == 401, resp.text

    def test_invalid_token_returns_401(self, oidc_app_client) -> None:
        client, _ = oidc_app_client
        resp = client.get(
            "/api/v1/vault/status",
            headers={"Authorization": "Bearer not.a.real.jwt"},
        )
        assert resp.status_code == 401, resp.text

    def test_oidc_configured_but_authlib_missing_returns_503(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from unittest.mock import MagicMock

        from fastapi.testclient import TestClient

        from doctoragent.api import server as srv
        from doctoragent.api.auth import oidc as oidc_mod
        from doctoragent.config import AegisConfig

        monkeypatch.setenv("DOCTORAGENT_OIDC_ISSUER", "https://idp.example.com")
        monkeypatch.setenv("DOCTORAGENT_OIDC_CLIENT_ID", "c")
        monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
        monkeypatch.setattr(oidc_mod, "_AUTHLIB_AVAILABLE", False, raising=True)
        monkeypatch.setattr(
            oidc_mod, "_is_authlib_available", lambda: False, raising=True
        )
        srv._reset_oidc_state()

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

        agent = MagicMock()
        agent.task_store.list_recent.return_value = []
        agent.task_store.list_vault_files.return_value = []
        agent.task_store.get.return_value = None
        agent.master_key_provider = MagicMock()
        agent.master_key_provider.get_key.return_value = b"\x00" * 32
        del agent._sync_engine
        del agent.search

        async def _search(*a, **k):
            return []

        agent.search = _search

        app = srv.create_app(config, agent)
        client = TestClient(app)
        # OIDC-protected endpoint → 503 (authlib missing).
        resp = client.get(
            "/api/v1/vault/status",
            headers={"Authorization": "Bearer any-token"},
        )
        assert resp.status_code == 503, resp.text
        # Unauthenticated endpoints still work — server did not crash.
        assert client.get("/health").status_code == 200

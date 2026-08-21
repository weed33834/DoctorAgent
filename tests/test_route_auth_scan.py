"""Regression gate: every unsafe (POST/PUT/DELETE/PATCH) route must carry an
auth dependency.

This is a *structural* test: it walks FastAPI's dependency tree for each
registered route and fails when an unsafe route has no authentication
dependency anywhere in its tree. It exists because ``POST /mcp/connect``
and ``/a2a/rpc`` previously shipped with zero auth dependencies — an
unauthenticated remote-code-execution vector that no endpoint-level test
happened to cover.

Recognised auth dependencies:

* ``_auth_dependency`` / ``_sensitive_auth_dependency``  (api/server.py)
* ``_cds_auth_dependency``                               (cds_hooks router)
* ``_dependency``                                        (rbac.require_role)

The allowlist contains routes that are public **by design**; each entry
must carry a justification comment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.api.server import is_available
from doctoragent.config import AegisConfig

AUTH_DEPENDENCY_NAMES = {
    "_auth_dependency",
    "_sensitive_auth_dependency",
    "_cds_auth_dependency",
    "_dependency",  # closure returned by rbac.require_role()
}

# Unsafe methods that must never be reachable without authentication.
UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Routes public by design:
PUBLIC_UNSAFE_ROUTES: dict[str, str] = {
    # Login exchanges credentials for a token; it cannot require one.
    "/api/v1/enterprise/auth/login": "credential exchange endpoint",
}


def _has_auth_dependency(dependant: Any) -> bool:
    """Recursively check a route's dependency tree for an auth dependency."""
    if getattr(dependant.call, "__name__", "") in AUTH_DEPENDENCY_NAMES:
        return True
    return any(_has_auth_dependency(sub) for sub in dependant.dependencies)


@pytest.mark.skipif(
    not is_available(),
    reason="FastAPI is not installed (optional dependency)",
)
class TestAllUnsafeRoutesAuthenticated:
    """Structural gate: no unsafe route without an auth dependency."""

    @pytest.fixture
    def app(self) -> Any:
        from doctoragent.api.server import create_app

        cfg = AegisConfig()
        tmp = Path(tempfile.mkdtemp())
        for name in ("inbox", "vault", "index", "logs"):
            path: Path = getattr(cfg.paths, name)
            path.mkdir(parents=True, exist_ok=True)
        cfg.paths.connections.parent.mkdir(parents=True, exist_ok=True)
        agent = MagicMock()
        del agent._sync_engine
        del agent.search

        async def _search(*args: object, **kwargs: object) -> list[Any]:
            return []

        agent.search = _search
        return create_app(cfg, agent)

    def test_unsafe_routes_have_auth(self, app: Any) -> None:
        offenders: list[str] = []
        checked = 0
        for route in app.routes:
            methods = getattr(route, "methods", None)
            if not methods or not (set(methods) & UNSAFE_METHODS):
                continue
            checked += 1
            if _has_auth_dependency(route.dependant):
                continue
            path = route.path
            if path in PUBLIC_UNSAFE_ROUTES:
                continue
            offenders.append(f"{sorted(set(methods) & UNSAFE_METHODS)} {path}")
        assert checked > 50, (
            "route enumeration unexpectedly small — create_app changed?"
        )
        assert offenders == [], (
            f"unsafe routes WITHOUT any auth dependency "
            f"(fix by adding _sensitive_auth_dependency or allowlist "
            f"with justification): {offenders}"
        )

    def test_known_rce_endpoints_are_protected(self, app: Any) -> None:
        """Explicit pins for the two historical unauthenticated endpoints."""
        protected_paths = {"/mcp/connect": False, "/a2a/rpc": False}
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            if "POST" in methods and route.path in protected_paths:
                protected_paths[route.path] = _has_auth_dependency(route.dependant)
        assert protected_paths["/mcp/connect"] is True
        assert protected_paths["/a2a/rpc"] is True

    def test_a2a_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secure-by-default: remote task submission requires explicit opt-in."""
        monkeypatch.delenv("DOCTORAGENT_A2A__ENABLED", raising=False)
        cfg = AegisConfig()
        assert cfg.a2a.enabled is False

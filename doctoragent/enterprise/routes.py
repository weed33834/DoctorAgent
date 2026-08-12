"""Enterprise / organization FastAPI routes (M14 A/B/C/E/F/G/K).

Exposes the enterprise platform over HTTP. The router reads its
:class:`~doctoragent.enterprise.service.EnterpriseService` off
``request.app.state.enterprise_service`` (created in ``doctoragent.api.server``)
so a single mount reuses the same store/audit wiring.

Auth policy mirrors the API server: admin endpoints require a valid token
(via the shared bearer/OIDC guard); the ``/enterprise/auth/*`` endpoints are
themselves the login flow and are intentionally unauthenticated.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPBearer

from doctoragent.api.auth._guards import (
    is_local_request as _is_local_request,
)
from doctoragent.api.auth._guards import (
    oidc_is_configured as _oidc_is_configured,
)
from doctoragent.api.auth._guards import (
    resolve_token as _resolve_token,
)
from doctoragent.api.auth._guards import (
    verify_bearer as _verify_bearer,
)

_bearer_scheme = HTTPBearer(auto_error=False)


def _service_unavailable(name: str) -> HTTPException:
    return HTTPException(status_code=503, detail=f"{name} is not configured")


async def _auth_dependency(
    request: Request,  # type: ignore[name-defined]
    credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
) -> Any:
    """Auth dependency for read/admin endpoints (same policy as the API server)."""
    if _oidc_is_configured():
        from doctoragent.api.server import _authenticate_oidc

        return await _authenticate_oidc(request, credentials)
    expected = _resolve_token()
    if expected is not None:
        provided = getattr(credentials, "credentials", None)
        if not _verify_bearer(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid or missing authentication token")
        return provided
    if not _is_local_request(request):
        raise HTTPException(
            status_code=401, detail="DOCTORAGENT_API_TOKEN not set; remote access denied"
        )
    return None


def _svc(request: Request) -> Any:
    svc = getattr(request.app.state, "enterprise_service", None)
    if svc is None:
        raise _service_unavailable("Enterprise service")
    return svc


def get_router() -> APIRouter:
    """Build the enterprise router."""
    router = APIRouter(prefix="/api/v1/enterprise", tags=["Enterprise"])

    # ── organizations (A) ───────────────────────────────────────────

    @router.get("/orgs", summary="List organizations")
    async def list_orgs(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        orgs = _svc(request).list_orgs()
        return {"total": len(orgs), "items": [o.model_dump() for o in orgs]}

    @router.post("/orgs", summary="Create an organization")
    async def create_org(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        org = _svc(request).create_org(
            payload.get("name", "").strip(),
            domain=payload.get("domain", ""),
            plan=payload.get("plan", "free"),
        )
        return org.model_dump()

    @router.get("/orgs/{org_id}", summary="Get an organization")
    async def get_org(
        org_id: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        org = _svc(request).get_org(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="org not found")
        return org.model_dump()

    # ── departments (A) ─────────────────────────────────────────────

    @router.get("/orgs/{org_id}/departments", summary="List departments (tree)")
    async def list_departments(
        org_id: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        depts = _svc(request).list_departments(org_id)
        return {"total": len(depts), "items": [d.model_dump() for d in depts]}

    @router.post("/orgs/{org_id}/departments", summary="Create a department")
    async def create_department(
        org_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        dept = _svc(request).create_department(
            org_id, payload.get("name", "").strip(), parent_id=payload.get("parent_id")
        )
        return dept.model_dump()

    @router.post("/departments/{dept_id}/move", summary="Move a department under a new parent")
    async def move_department(
        dept_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            dept = _svc(request).move_department(dept_id, payload.get("parent_id"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return dept.model_dump()

    # ── users (B) ───────────────────────────────────────────────────

    @router.get("/orgs/{org_id}/users", summary="List users with filters")
    async def list_users(
        org_id: str,
        request: Request,
        dept_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        role: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        status: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        users = _svc(request).list_users(org_id, dept_id=dept_id, role=role, status=status)
        return {
            "total": len(users),
            "items": [u.model_dump(exclude={"password_hash", "mfa_secret"}) for u in users],
        }

    @router.post("/orgs/{org_id}/users", summary="Create a user account")
    async def create_user(
        org_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.enterprise.models import UserRole

        role = UserRole(payload.get("role", "member"))
        try:
            user = _svc(request).create_user(
                org_id,
                payload.get("email", ""),
                payload.get("password", ""),
                display_name=payload.get("display_name", ""),
                dept_id=payload.get("dept_id"),
                role=role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return user.model_dump(exclude={"password_hash", "mfa_secret"})

    @router.post("/orgs/{org_id}/users/import", summary="Bulk import users (CSV rows)")
    async def import_users(
        org_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        rows = payload.get("rows") or []
        return _svc(request).bulk_import_users(org_id, rows)

    @router.put("/users/{user_id}/status", summary="Enable / disable / lock a user")
    async def set_user_status(
        user_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.enterprise.models import AccountStatus

        user = _svc(request).set_user_status(
            user_id, AccountStatus(payload.get("status", "active"))
        )
        return user.model_dump(exclude={"password_hash", "mfa_secret"})

    @router.put("/users/{user_id}/role", summary="Assign a role to a user")
    async def set_user_role(
        user_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.enterprise.models import UserRole

        user = _svc(request).set_user_role(user_id, UserRole(payload.get("role", "member")))
        return user.model_dump(exclude={"password_hash", "mfa_secret"})

    @router.get("/users/{user_id}/login-events", summary="Login audit events for a user")
    async def user_login_events(
        user_id: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        events = _svc(request).store.list_login_events(user_id=user_id)
        return {"total": len(events), "items": [e.model_dump() for e in events]}

    # ── authentication (B/C) ────────────────────────────────────────

    @router.post(
        "/auth/login", summary="Authenticate (password, with lockout)", include_in_schema=True
    )
    async def login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # type: ignore[name-defined]  # noqa: B008
        client_ip = request.client.host if request.client else ""
        user, result = _svc(request).authenticate(
            payload.get("org_id", "default"),
            payload.get("email", ""),
            payload.get("password", ""),
            ip=client_ip,
            user_agent=request.headers.get("user-agent", ""),
        )
        return {
            "result": result,
            "user": user.model_dump(exclude={"password_hash", "mfa_secret"}) if user else None,
        }

    @router.post("/auth/mfa/enroll", summary="Start MFA (TOTP) enrollment")
    async def mfa_enroll(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _svc(request).mfa_enroll(payload.get("user_id", ""))

    @router.post("/auth/mfa/verify", summary="Confirm MFA enrollment or login code")
    async def mfa_verify(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        svc = _svc(request)
        if payload.get("action") == "login":
            ok = svc.mfa_verify_login(payload.get("user_id", ""), payload.get("code", ""))
        else:
            ok = svc.mfa_verify_enroll(payload.get("user_id", ""), payload.get("code", ""))
        return {"ok": ok}

    # ── audit export (E) ────────────────────────────────────────────

    @router.get("/audit/export", summary="Export audit / login events as CSV")
    async def audit_export(
        request: Request,
        org_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(1000, ge=1, le=10000),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> Response:
        events = _svc(request).store.list_login_events(org_id=org_id, limit=limit)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["at", "email", "org_id", "ip", "result", "detail"])
        for e in events:
            writer.writerow([e.at, e.email, e.org_id, e.ip, e.result, e.detail])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=login_events.csv"},
        )

    # ── budget / quota / overlimit (F) ──────────────────────────────

    @router.put("/governance/budget", summary="Set a budget for a scope")
    async def set_budget(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        b = _svc(request).set_budget(
            payload.get("scope", "org"),
            payload.get("scope_id", ""),
            float(payload.get("amount_usd", 0)),
            alert_threshold=float(payload.get("alert_threshold", 0.8)),
            hard_limit=bool(payload.get("hard_limit", False)),
        )
        return b.model_dump()

    @router.post("/governance/overlimit", summary="Evaluate spend against budget")
    async def check_overlimit(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _svc(request).check_overlimit(
            payload.get("scope", "org"),
            payload.get("scope_id", ""),
            float(payload.get("current_usd", 0)),
        )

    @router.put("/governance/quota", summary="Set usage quotas for a scope")
    async def set_quota(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        q = _svc(request).set_quota(
            payload.get("scope", "org"),
            payload.get("scope_id", ""),
            tokens_per_day=int(payload.get("tokens_per_day", -1)),
            calls_per_day=int(payload.get("calls_per_day", -1)),
            storage_mb=int(payload.get("storage_mb", -1)),
            concurrent=int(payload.get("concurrent", -1)),
        )
        return q.model_dump()

    # ── settings / announcements / maintenance (K) ─────────────────

    @router.get("/settings", summary="List system settings")
    async def list_settings(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        items = _svc(request).list_settings()
        return {"items": [s.model_dump() for s in items]}

    @router.put("/settings", summary="Set system settings (key-value)")
    async def set_settings(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        _svc(request).set_settings(payload)
        return {"ok": True}

    @router.get("/announcements", summary="List announcements")
    async def list_announcements(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        items = _svc(request).list_announcements()
        return {"total": len(items), "items": [a.model_dump() for a in items]}

    @router.post("/announcements", summary="Create an announcement")
    async def create_announcement(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        a = _svc(request).create_announcement(
            payload.get("title", ""),
            payload.get("content", ""),
            level=payload.get("level", "info"),
            pinned=bool(payload.get("pinned", False)),
        )
        return a.model_dump()

    @router.get("/maintenance", summary="Get maintenance state")
    async def get_maintenance(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _svc(request).get_maintenance().model_dump()

    @router.put("/maintenance", summary="Set maintenance mode")
    async def set_maintenance(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return (
            _svc(request)
            .set_maintenance(
                bool(payload.get("enabled", False)),
                message=payload.get("message", ""),
                readonly=bool(payload.get("readonly", False)),
            )
            .model_dump()
        )

    # ── API keys (G) ────────────────────────────────────────────────

    @router.get("/apikeys", summary="List API keys")
    async def list_api_keys(
        request: Request,
        org_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _svc(request).list_api_keys(org_id)
        return {"total": len(items), "items": [k.model_dump() for k in items]}

    @router.post("/apikeys", summary="Create an API key")
    async def create_api_key(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _svc(request).create_api_key(
            payload.get("org_id", "default"),
            payload.get("label", ""),
            scopes=payload.get("scopes"),
        )

    @router.get("/status", summary="Enterprise platform summary")
    async def enterprise_status(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        svc = _svc(request)
        orgs = svc.list_orgs()
        return {
            "orgs": len(orgs),
            "users": svc.store.count_users(),
            "announcements": len(svc.list_announcements()),
            "maintenance": svc.get_maintenance().enabled,
            "api_keys": len(svc.list_api_keys()),
        }

    return router

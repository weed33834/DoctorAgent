"""Routes for interop (M27), AI security (M25) and disaster recovery (M29).

Mounted on the API server; reads services off ``request.app.state``
(``interop_service``, ``threat_service``, ``disaster_service``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
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


async def _auth_dependency(
    request: Request,  # type: ignore[name-defined]
    credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
) -> Any:
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


def _get(request: Request, name: str) -> Any:
    svc = getattr(request.app.state, name, None)
    if svc is None:
        raise HTTPException(status_code=503, detail=f"{name} is not configured")
    return svc


def get_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Security & Interop & DR"])

    # ── AI security / red team (M25) ────────────────────────────────

    @router.get("/security/overview", summary="AI security threat overview")
    async def security_overview(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "threat_service").overview()

    @router.post("/security/threat-cases", summary="Add a threat case")
    async def add_threat_case(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return _get(request, "threat_service").store.add_threat_case(
                payload.get("name", ""),
                payload.get("threat_type", "prompt_injection"),
                payload.get("attack_vector", ""),
                severity=payload.get("severity", "medium"),
                tags=payload.get("tags"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/security/threat-cases", summary="List threat cases")
    async def list_threat_cases(
        request: Request,
        threat_type: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _get(request, "threat_service").store.list_threat_cases(threat_type)
        return {"total": len(items), "items": items}

    @router.get("/security/rules", summary="List injection detection rules")
    async def list_rules(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "threat_service").store.list_rules()}

    @router.post("/security/scan", summary="Scan input for injection/jailbreak")
    async def scan_input(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        svc = _get(request, "threat_service")
        return svc.scan_input(
            payload.get("text", ""),
            user_id=payload.get("user_id", ""),
            session_id=payload.get("session_id", ""),
        )

    @router.get("/security/events", summary="Security event ledger")
    async def list_events(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _get(request, "threat_service").store.list_events(limit)
        return {"total": len(items), "items": items}

    @router.post("/security/redteam/run", summary="Run a red-team drill")
    async def redteam_run(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "threat_service").run_redteam(
            payload.get("name", "redteam"), cases=payload.get("cases")
        )

    @router.get("/security/redteam", summary="List red-team runs")
    async def list_redteam(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "threat_service").store.list_redteam()}

    # ── interoperability (M27) ──────────────────────────────────────

    @router.get("/interop/overview", summary="Interop overview")
    async def interop_overview(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "interop_service").overview()

    @router.get("/interop/directory", summary="External agent directory")
    async def interop_directory(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        items = _get(request, "interop_service").store.list_agents()
        return {"total": len(items), "items": [a.model_dump() for a in items]}

    @router.post("/interop/directory/register", summary="Register an external agent")
    async def register_agent(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.interop.models import TrustLevel

        agent = _get(request, "interop_service").register_agent(
            payload.get("name", ""),
            payload.get("url", ""),
            description=payload.get("description", ""),
            capabilities=payload.get("capabilities"),
            trust_level=TrustLevel(payload.get("trust_level", "limited")),
            auth_type=payload.get("auth_type", "none"),
        )
        return agent.model_dump()

    @router.get("/interop/policies", summary="List interop policies")
    async def list_policies(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        items = _get(request, "interop_service").store.list_policies()
        return {"total": len(items), "items": [p.model_dump() for p in items]}

    @router.post("/interop/policies", summary="Add an interop policy")
    async def add_policy(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.interop.models import TrustLevel

        p = _get(request, "interop_service").add_policy(
            payload.get("name", "policy"),
            allow_agents=payload.get("allow_agents"),
            deny_actions=payload.get("deny_actions"),
            require_trust=TrustLevel(payload.get("require_trust", "trusted")),
            rate_limit_per_min=int(payload.get("rate_limit_per_min", 60)),
        )
        return p.model_dump()

    @router.post("/interop/check-access", summary="Evaluate an interop policy check")
    async def check_access(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.interop.models import TrustLevel

        return _get(request, "interop_service").check_access(
            payload.get("agent", ""),
            payload.get("action", ""),
            TrustLevel(payload.get("trust", "trusted")),
        )

    @router.get("/interop/tasks", summary="A2A task monitor")
    async def list_interop_tasks(
        request: Request,
        direction: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(100, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _get(request, "interop_service").store.list_tasks(direction=direction, limit=limit)
        return {"total": len(items), "items": [t.model_dump() for t in items]}

    # ── disaster recovery (M29) ─────────────────────────────────────

    @router.get("/dr/metrics", summary="Continuity dashboard metrics")
    async def dr_metrics(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "disaster_service").metrics()

    @router.get("/dr/backups", summary="List backup jobs")
    async def list_backups(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        items = _get(request, "disaster_service").store.list_backup_jobs()
        return {"total": len(items), "items": items}

    @router.post("/dr/backups", summary="Register a backup job")
    async def add_backup(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "disaster_service").register_backup_job(
            payload.get("name", ""),
            payload.get("scope", "database"),
            backup_type=payload.get("backup_type", "full"),
            schedule=payload.get("schedule", "0 2 * * 0"),
            retention_days=int(payload.get("retention_days", 30)),
        )

    @router.post("/dr/backups/{job_id}/run", summary="Execute a backup job")
    async def run_backup(
        job_id: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        try:
            return _get(request, "disaster_service").execute_backup(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/dr/plans", summary="List DR plans")
    async def list_dr_plans(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "disaster_service").store.list_plans()}

    @router.post("/dr/plans", summary="Create a DR plan")
    async def create_dr_plan(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "disaster_service").create_dr_plan(
            payload.get("name", ""),
            int(payload.get("rto_target_s", 60)),
            int(payload.get("rpo_target_s", 300)),
            tier=int(payload.get("tier", 3)),
            scenarios=payload.get("scenarios"),
        )

    @router.post("/dr/drills", summary="Run a continuity drill (measures RTO/RPO)")
    async def run_drill(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "disaster_service").run_drill(
            payload.get("name", "drill"),
            payload.get("plan_id", ""),
            scenario=payload.get("scenario", "failover"),
        )

    @router.get("/dr/drills", summary="List drill history")
    async def list_drills(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "disaster_service").store.list_drills()}

    @router.post("/dr/fault-inject", summary="Fault-injection laboratory")
    async def fault_inject(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return _get(request, "disaster_service").fault_inject(payload.get("mode", "data_loss"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router

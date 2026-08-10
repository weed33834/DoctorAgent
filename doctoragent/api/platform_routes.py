"""Platform routes: data governance (M20), model pricing (M21), semantic cache
(M23) and cost dashboard (M21). Mounted on the API server; reads its services
off ``request.app.state``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

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
from fastapi.security import HTTPBearer

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
        raise HTTPException(status_code=401, detail="DOCTORAGENT_API_TOKEN not set; remote access denied")
    return None


def _get(request: Request, name: str) -> Any:
    svc = getattr(request.app.state, name, None)
    if svc is None:
        raise HTTPException(status_code=503, detail=f"{name} is not configured")
    return svc


def get_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Platform"])

    # ── error catalog (M19 docs-support) ────────────────────────────

    @router.get("/errors", summary="Error code catalog")
    async def error_catalog(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        from doctoragent.api.error_catalog import catalog

        items = catalog()
        return {"total": len(items), "items": items}

    # ── data governance (M20) ───────────────────────────────────────

    @router.post("/governance/assets", summary="Register a data asset (auto-classify)")
    async def register_asset(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.governance.models import AssetType

        svc = _get(request, "governance_service")
        asset = svc.register_asset(
            payload.get("name", ""),
            AssetType(payload.get("asset_type", "document")),
            org_id=payload.get("org_id", "default"),
            source=payload.get("source", ""),
            description=payload.get("description", ""),
            content=payload.get("content", ""),
            row_count=int(payload.get("row_count", 0)),
            size_bytes=int(payload.get("size_bytes", 0)),
        )
        return asset.model_dump()

    @router.get("/governance/assets", summary="List data assets")
    async def list_assets(
        request: Request,
        asset_type: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        sensitivity: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        svc = _get(request, "governance_service")
        items = svc.store.list_assets(asset_type=asset_type, sensitivity=sensitivity)
        return {"total": len(items), "items": [a.model_dump() for a in items]}

    @router.get("/governance/assets/{asset_id}", summary="Get a data asset with lineage")
    async def get_asset(
        asset_id: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        svc = _get(request, "governance_service")
        asset = svc.store.get_asset(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return {
            **asset.model_dump(),
            "lineage": svc.store.get_lineage(asset_id),
            "quality": [q.model_dump() for q in svc.store.quality_for(asset_id)],
        }

    @router.get("/governance/summary", summary="Data catalog summary")
    async def governance_summary(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        return _get(request, "governance_service").catalog_summary()

    @router.post("/governance/lineage", summary="Record a lineage edge")
    async def add_lineage(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        svc = _get(request, "governance_service")
        edge = svc.record_lineage(payload.get("upstream", ""), payload.get("downstream", ""),
                                  payload.get("transform", ""))
        return edge.model_dump()

    @router.post("/governance/assets/{asset_id}/quality", summary="Add a quality check")
    async def add_quality(
        asset_id: str, request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.governance.models import QualityCheck

        svc = _get(request, "governance_service")
        q = svc.store.add_quality(QualityCheck(
            id="", asset_id=asset_id, check_type=payload.get("check_type", "accuracy"),
            score=float(payload.get("score", 0.0)), status=payload.get("status", "pass"),
            detail=payload.get("detail", ""), created_at="",
        ))
        return q.model_dump()

    @router.post("/governance/rules", summary="Add a classification rule")
    async def add_rule(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.governance.models import DataSensitivity

        svc = _get(request, "governance_service")
        rule = svc.add_classification_rule(
            payload.get("name", "rule"),
            DataSensitivity(payload.get("sensitivity", "confidential")),
            payload.get("keywords") or [],
        )
        return rule.model_dump()

    # ── model pricing (M21) ─────────────────────────────────────────

    @router.get("/pricing/models", summary="List model price table")
    async def list_prices(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        return _get(request, "pricing").list_prices()

    @router.post("/pricing/compare", summary="Compare models on cost/context (比价器)")
    async def compare_models(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        models = payload.get("models") or []
        result = _get(request, "pricing").compare(models)
        return {"models": result}

    @router.post("/pricing/estimate", summary="Estimate cost for a token usage")
    async def estimate_cost(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        pricing = _get(request, "pricing")
        cost = pricing.cost_per_1k(
            payload.get("model", ""),
            int(payload.get("input_tokens", 0)),
            int(payload.get("output_tokens", 0)),
        )
        return {"model": payload.get("model", ""), "estimated_cost_usd": round(cost, 6)}

    # ── semantic cache (M23) ────────────────────────────────────────

    @router.get("/cache/stats", summary="Semantic cache stats")
    async def cache_stats(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        cache = _get(request, "semantic_cache")
        return cache.stats()

    @router.post("/cache", summary="Prime the semantic cache (query → response)")
    async def cache_put(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        cache = _get(request, "semantic_cache")
        cache.put(payload.get("query", ""), payload.get("response", ""))
        return {"ok": True}

    @router.delete("/cache", summary="Clear the semantic cache")
    async def cache_clear(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        cache = _get(request, "semantic_cache")
        cache.clear()
        return {"ok": True}

    # ── cost dashboard (M21) ────────────────────────────────────────

    @router.get("/cost/overview", summary="Cost dashboard overview")
    async def cost_overview(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        tracker = _get(request, "cost_tracker")
        summary = tracker.get_summary() if hasattr(tracker, "get_summary") else {}
        pricing = _get(request, "pricing")
        return {
            "summary": summary,
            "model_count": len(pricing.list_prices()),
            "semantic_cache": _get(request, "semantic_cache").stats(),
        }

    @router.get("/cost/daily", summary="Daily cost trend")
    async def cost_daily(
        request: Request,
        days: int = Query(7, ge=1, le=90),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        tracker = _get(request, "cost_tracker")
        rows = tracker.get_daily_costs(days) if hasattr(tracker, "get_daily_costs") else []
        return {"days": rows}

    return router

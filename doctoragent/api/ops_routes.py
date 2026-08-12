"""Routes for multimodal (M26), data pipeline (M28), knowledge bases (M14 D),
task center (M14 K) and usage analytics (M14 J / M24).

Reads services off ``request.app.state`` (``multimodal_service``,
``pipeline_service``, ``kb_manager``, ``task_center``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


from doctoragent.api.auth._guards import (  # noqa: E402
    is_local_request as _is_local_request,
)
from doctoragent.api.auth._guards import (  # noqa: E402
    oidc_is_configured as _oidc_is_configured,
)
from doctoragent.api.auth._guards import (  # noqa: E402
    resolve_token as _resolve_token,
)
from doctoragent.api.auth._guards import (  # noqa: E402
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
    router = APIRouter(prefix="/api/v1", tags=["Ops"])

    # ── multimodal (M26) ────────────────────────────────────────────

    @router.get("/multimodal/summary", summary="Multimodal asset library summary")
    async def mm_summary(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "multimodal_service").store.summary()

    @router.get("/multimodal/assets", summary="List multimodal assets")
    async def mm_list(
        request: Request,
        modality: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _get(request, "multimodal_service").store.list_assets(modality=modality)
        return {"total": len(items), "items": items}

    @router.post("/multimodal/assets", summary="Register a multimodal asset")
    async def mm_add(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return _get(request, "multimodal_service").ingest(
                payload.get("name", ""),
                payload.get("modality", "text"),
                path=payload.get("path", ""),
                mime=payload.get("mime", ""),
                metadata=payload.get("metadata"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/multimodal/search", summary="Cross-modal search")
    async def mm_search(
        request: Request,
        query: str = Query(..., min_length=1),  # type: ignore[name-defined]  # noqa: B008
        modality: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "multimodal_service").search(query, modality=modality)

    # ── data pipeline (M28) ─────────────────────────────────────────

    @router.get("/pipeline/overview", summary="Data pipeline overview")
    async def pipe_overview(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "pipeline_service").overview()

    @router.post("/pipeline/sources", summary="Register a data source")
    async def add_source(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "pipeline_service").store.add_source(
            payload.get("name", ""),
            payload.get("source_type", "file"),
            endpoint=payload.get("endpoint", ""),
            config=payload.get("config"),
        )

    @router.get("/pipeline/sources", summary="List data sources")
    async def list_sources(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "pipeline_service").store.list_sources()}

    @router.post("/pipeline/pipelines", summary="Define a pipeline (ordered nodes)")
    async def add_pipeline(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "pipeline_service").store.add_pipeline(
            payload.get("name", ""),
            payload.get("nodes") or [],
            source_id=payload.get("source_id", ""),
            schedule=payload.get("schedule", ""),
        )

    @router.get("/pipeline/pipelines", summary="List pipeline definitions")
    async def list_pipelines(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "pipeline_service").store.list_pipelines()}

    @router.post("/pipeline/rules", summary="Add a transform rule")
    async def add_rule(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "pipeline_service").store.add_transform_rule(
            payload.get("name", ""),
            payload.get("match", ""),
            payload.get("action", "lowercase"),
            payload.get("params"),
        )

    @router.get("/pipeline/rules", summary="List transform rules")
    async def list_rules(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return {"items": _get(request, "pipeline_service").store.list_transform_rules()}

    @router.post("/pipeline/pipelines/{pid}/run", summary="Run a pipeline on a batch")
    async def run_pipeline(
        pid: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return _get(request, "pipeline_service").run_pipeline(pid, payload.get("batch"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/pipeline/runs", summary="Pipeline run history")
    async def list_runs(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return {"items": _get(request, "pipeline_service").store.list_runs(limit)}

    @router.get("/pipeline/quality", summary="Data quality center")
    async def pipeline_quality(
        request: Request,
        pipeline_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return {"items": _get(request, "pipeline_service").store.list_quality(pipeline_id)}

    # ── knowledge bases (M14 D) ─────────────────────────────────────

    @router.get("/kb/summary", summary="Knowledge base summary")
    async def kb_summary(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "kb_manager").summary()

    @router.get("/kb", summary="List knowledge bases")
    async def list_kbs(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        return {"items": _get(request, "kb_manager").list()}

    @router.post("/kb", summary="Create a knowledge base")
    async def create_kb(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "kb_manager").create(
            payload.get("name", ""),
            description=payload.get("description", ""),
            embedding_model=payload.get("embedding_model", "default"),
            chunk_size=int(payload.get("chunk_size", 500)),
            chunk_overlap=int(payload.get("chunk_overlap", 50)),
            visibility=payload.get("visibility", "private"),
            owner=payload.get("owner", ""),
        )

    @router.put("/kb/{kb_id}", summary="Update a knowledge base")
    async def update_kb(
        kb_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        kb = _get(request, "kb_manager").update(kb_id, **payload)
        if kb is None:
            raise HTTPException(status_code=404, detail="kb not found")
        return kb

    @router.post("/kb/{kb_id}/test", summary="Test knowledge base retrieval")
    async def test_kb(
        kb_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return _get(request, "kb_manager").test_retrieval(kb_id, payload.get("query", ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── task center (M14 K) ─────────────────────────────────────────

    @router.get("/tasks", summary="Task center: list tasks")
    async def list_tasks(
        request: Request,
        status: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return {"items": _get(request, "task_center").list(status=status)}

    @router.post("/tasks", summary="Create a task")
    async def create_task(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return _get(request, "task_center").create(
                payload.get("task_type", "custom"),
                payload.get("name", ""),
                payload.get("params"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/tasks/{task_id}/retry", summary="Retry a failed task")
    async def retry_task(
        task_id: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        task = _get(request, "task_center").retry(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @router.get("/tasks/summary", summary="Task center summary")
    async def task_summary(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _get(request, "task_center").summary()

    # ── usage analytics (M14 J / M24) ───────────────────────────────

    @router.get("/analytics/overview", summary="Platform usage analytics")
    async def analytics_overview(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        agg: dict[str, Any] = {"timestamp": _now()}
        for name in (
            "threat_service",
            "interop_service",
            "disaster_service",
            "pipeline_service",
            "task_center",
            "kb_manager",
            "multimodal_service",
            "enterprise_service",
        ):
            svc = getattr(request.app.state, name, None)
            if svc is None:
                continue
            try:
                if name == "threat_service":
                    agg["security"] = svc.overview()
                elif name == "interop_service":
                    agg["interop"] = svc.overview()
                elif name == "disaster_service":
                    agg["dr"] = svc.metrics()
                elif name == "pipeline_service":
                    agg["pipeline"] = svc.overview()
                elif name == "task_center":
                    agg["tasks"] = svc.summary()
                elif name == "kb_manager":
                    agg["knowledge_bases"] = svc.summary()
                elif name == "multimodal_service":
                    agg["multimodal"] = svc.store.summary()
                elif name == "enterprise_service":
                    agg["enterprise"] = {
                        "orgs": len(svc.list_orgs()),
                        "users": svc.store.count_users(),
                    }
            except Exception:  # noqa: BLE001 — best-effort analytics
                pass
        return agg

    return router

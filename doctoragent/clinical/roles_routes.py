"""Clinical specialty roles + built-in knowledge routes.

Lets the user switch the agent's clinical persona (specialty) and seed/view the
built-in medical knowledge base. Reads state off ``request.app.state``:
``clinical_role`` (current role code) and ``config``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
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


def get_router() -> APIRouter:
    from doctoragent.clinical.roles import default_role, get_role, list_roles

    router = APIRouter(prefix="/api/v1/clinical", tags=["Clinical Roles"])

    @router.get("/roles", summary="List built-in clinical specialty roles")
    async def roles_list(_auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        items = [r.to_dict() for r in list_roles()]
        return {"total": len(items), "items": items}

    @router.post("/roles/{code}/activate", summary="Activate a clinical specialty role")
    async def roles_activate(
        code: str,
        request: Request,
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        role = get_role(code)
        if role is None:
            raise HTTPException(status_code=404, detail=f"unknown role {code!r}")
        request.app.state.clinical_role = code
        # Persist across restart via workspace settings (best-effort).
        ws = getattr(request.app.state, "workspace_config", None)
        if ws is not None:
            try:
                ws.set_settings({"clinical_role": code})
            except Exception:  # noqa: BLE001
                pass
        return {"activated": role.code, "name": role.name, "prompt": role.prompt}

    @router.get("/status", summary="Current clinical role + knowledge status")
    async def status(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        from doctoragent.clinical.knowledge import KNOWLEDGE_DOCS

        code = getattr(request.app.state, "clinical_role", None) or "general"
        role = get_role(code) or default_role()
        ws = getattr(request.app.state, "workspace_config", None)
        seeded = 0
        if ws is not None:
            try:
                seeded = int(ws.get_setting("knowledge_seeded", "0"))
            except Exception:  # noqa: BLE001
                pass
        return {
            "role": role.to_dict(),
            "knowledge_builtin": len(KNOWLEDGE_DOCS),
            "knowledge_seeded_docs": seeded,
        }

    @router.get("/knowledge", summary="List built-in clinical knowledge docs")
    async def knowledge_list(_auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        from doctoragent.clinical.knowledge import list_knowledge

        items = list_knowledge()
        return {"total": len(items), "items": items}

    @router.post("/knowledge/seed", summary="Seed built-in knowledge into the Vault")
    async def knowledge_seed(
        request: Request,
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.clinical.knowledge import seed_knowledge

        cfg = getattr(request.app.state, "config", None)
        vault = getattr(getattr(cfg, "paths", None), "vault", None)
        if vault is None:
            raise HTTPException(status_code=503, detail="vault path not configured")
        n = seed_knowledge(Path(vault))
        # Mark seeded for status reporting.
        ws = getattr(request.app.state, "workspace_config", None)
        if ws is not None:
            try:
                ws.set_settings({"knowledge_seeded": str(n)})
            except Exception:  # noqa: BLE001
                pass
        return {"seeded_docs": n, "target": str(Path(vault) / "临床知识")}

    return router

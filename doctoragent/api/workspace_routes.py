"""Workspace routes: sandboxed code execution, conversation-management
(prompts / skills / experts, shared with the management UI) and document export
(Markdown / PDF / DOCX).

Reads the shared :class:`~doctoragent.workspace_config.WorkspaceConfig` off
``request.app.state.workspace_config``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
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
    router = APIRouter(prefix="/api/v1", tags=["Workspace"])

    # ── sandboxed code execution ────────────────────────────────────

    @router.post("/sandbox/run", summary="Run Python code in the sandbox (may return a chart)")
    async def sandbox_run(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        from doctoragent.security.sandbox import SandboxManager
        import sys

        code = payload.get("code", "")
        if not code.strip():
            raise HTTPException(status_code=400, detail="'code' is required")
        sandbox = SandboxManager(enable_strong_isolation=False)
        work = Path(sandbox.work_dir)
        (work / "code.py").write_text(code, encoding="utf-8")
        python = sys.executable or "python3"
        result = sandbox.run_sandboxed([python, "-u", "code.py"],
                                       timeout=float(payload.get("timeout", 30)))
        data: dict[str, Any] = {
            "returncode": result.returncode,
            "stdout": result.stdout[-6000:],
            "stderr": result.stderr[-4000:],
            "timed_out": result.timed_out,
            "isolation": result.isolation_level,
        }
        # Capture a generated image if present.
        for f in sorted(work.iterdir()):
            if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".gif"}:
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".svg": "image/svg+xml", ".gif": "image/gif"}.get(f.suffix.lower(), "application/octet-stream")
                import base64

                data["image"] = f"data:{mime};base64,{base64.b64encode(f.read_bytes()).decode()}"
                break
        sandbox.close()
        return data

    # ── conversation-management: prompts ────────────────────────────

    @router.get("/workspace/prompts", summary="List prompt templates")
    async def list_prompts(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        items = _get(request, "workspace_config").list_prompts()
        return {"total": len(items), "items": items}

    @router.post("/workspace/prompts", summary="Create / update a prompt template")
    async def upsert_prompt(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "workspace_config").upsert_prompt(
            payload.get("name", ""), payload.get("template", ""),
            description=payload.get("description", ""), variables=payload.get("variables"),
        )

    # ── conversation-management: skills ─────────────────────────────

    @router.get("/workspace/skills", summary="List custom skills")
    async def list_skills(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        items = _get(request, "workspace_config").list_skills()
        return {"total": len(items), "items": items}

    @router.post("/workspace/skills", summary="Register a custom skill")
    async def register_skill(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "workspace_config").register_skill(
            payload.get("name", ""), payload.get("description", ""),
            triggers=payload.get("triggers"), code=payload.get("code", ""),
        )

    # ── conversation-management: experts ────────────────────────────

    @router.get("/workspace/experts", summary="List custom experts")
    async def list_experts(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        items = _get(request, "workspace_config").list_experts()
        return {"total": len(items), "items": items}

    @router.post("/workspace/experts", summary="Create a custom expert")
    async def create_expert(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _get(request, "workspace_config").create_expert(
            payload.get("name", ""), payload.get("title", ""), payload.get("system_prompt", ""),
            tools=payload.get("tools"),
        )

    @router.get("/workspace/summary", summary="Workspace configuration summary")
    async def workspace_summary(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        return _get(request, "workspace_config").summary()

    # ── document export (chat → md / pdf / docx) ────────────────────

    @router.post("/doc/export", summary="Export chat messages to Markdown / PDF / DOCX")
    async def doc_export(
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> Response:
        from doctoragent.docgen import export_messages

        fmt = (payload.get("format") or "md").lower().lstrip(".")
        messages = payload.get("messages") or []
        title = payload.get("title") or "对话"
        if fmt not in ("md", "pdf", "docx"):
            raise HTTPException(status_code=400, detail="format must be md|pdf|docx")
        if not messages:
            raise HTTPException(status_code=400, detail="'messages' is required")
        out = Path(tempfile.gettempdir()) / f"doctoragent-export-{title[:30]}.{fmt}"
        try:
            export_messages(messages, fmt, out)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"export failed: {exc}") from exc
        media = {"md": "text/markdown", "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
        return Response(
            content=out.read_bytes(),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="doctoragent-{title[:30]}.{fmt}"'},
        )

    return router

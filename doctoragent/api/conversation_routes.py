"""Conversation management API (server-side persistence + feedback + fork).

Reads :class:`~doctoragent.conversations.ConversationStore` off
``request.app.state.conversation_store``.
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


def _store(request: Request) -> Any:
    s = getattr(request.app.state, "conversation_store", None)
    if s is None:
        raise HTTPException(status_code=503, detail="conversation store not configured")
    return s


def _tenant(request: Request) -> str:
    """Resolve the caller's tenant scope.

    OIDC users carry ``tenant_id`` on their identity; every other auth path
    (static service-account token, local) maps to the ``default`` tenant.
    """
    user = getattr(getattr(request, "state", None), "user", None)
    tid = getattr(user, "tenant_id", None)
    return str(tid) if tid else "default"


def get_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/conversations", tags=["Conversations"])

    @router.post("", summary="Create a conversation (auto-title from first message)")
    async def create_conversation(
        request: Request,
        payload: dict[str, Any] = Body(default={}),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        store = _store(request)
        title = payload.get("title") or ""
        first = payload.get("first_message") or ""
        if not title and first:
            title = store.auto_title(first)
        tenant = _tenant(request)
        conv = store.create(title or "新对话", payload.get("meta"), tenant_id=tenant)
        if first:
            store.add_message(conv["id"], "user", first, tenant_id=tenant)
        return store.get(conv["id"]) or conv

    @router.get("", summary="List / search conversations")
    async def list_conversations(
        request: Request,
        q: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(50, ge=1, le=200),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _store(request).list(q or "", limit=limit, tenant_id=_tenant(request))
        return {"total": len(items), "items": items}

    @router.get("/{cid}", summary="Get a conversation with messages")
    async def get_conversation(
        cid: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        conv = _store(request).get_for_tenant(cid, _tenant(request))
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @router.post("/{cid}/messages", summary="Add a message to a conversation")
    async def add_message(
        cid: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        msg = _store(request).add_message(
            cid,
            payload.get("role", "user"),
            payload.get("content", ""),
            tenant_id=_tenant(request),
        )
        if msg is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return msg

    @router.patch("/{cid}", summary="Rename a conversation")
    async def rename_conversation(
        cid: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        ok = _store(request).rename(cid, payload.get("title", ""), tenant_id=_tenant(request))
        if not ok:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"ok": True}

    @router.post("/{cid}/fork", summary="Fork (branch) a conversation")
    async def fork_conversation(
        cid: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        conv = _store(request).fork(cid, payload.get("title", ""), tenant_id=_tenant(request))
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @router.post("/messages/{mid}/feedback", summary="Record like/dislike feedback")
    async def message_feedback(
        mid: str,
        request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        rating = int(payload.get("rating", 0))
        if rating not in (-1, 0, 1):
            raise HTTPException(status_code=400, detail="rating must be -1, 0 or 1")
        ok = _store(request).feedback(mid, rating, payload.get("comment", ""))
        if not ok:
            raise HTTPException(status_code=404, detail="message not found")
        return {"ok": True}

    @router.delete("/{cid}", summary="Delete a conversation")
    async def delete_conversation(
        cid: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        ok = _store(request).delete(cid, tenant_id=_tenant(request))
        if not ok:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"ok": True}

    @router.get("/stats/overview", summary="Conversation store stats")
    async def conversation_stats(
        request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        return _store(request).stats()

    @router.post("/{cid}/share", summary="Create a share link for a conversation")
    async def share_conversation(
        cid: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        share = _store(request).share(
            cid, ttl_hours=int(payload.get("ttl_hours", 168)), tenant_id=_tenant(request)
        )
        if share is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        share["url"] = f"/#/shared/{share['token']}"
        return share

    @router.post("/shares/{token}/revoke", summary="Revoke a share link")
    async def revoke_share(
        token: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        ok = _store(request).revoke_share(token)
        if not ok:
            raise HTTPException(status_code=404, detail="share token not found")
        return {"ok": True}

    @router.get("/shared/{token}", summary="View a shared conversation (public, no auth)")
    async def shared_conversation(token: str, request: Request) -> dict[str, Any]:
        conv = _store(request).get_shared(token)
        if conv is None:
            raise HTTPException(status_code=404, detail="share link invalid or expired")
        return conv

    @router.post("/{cid}/summarize", summary="Summarize a conversation")
    async def summarize_conversation(
        cid: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        store = _store(request)
        tenant = _tenant(request)
        conv = store.get_for_tenant(cid, tenant)
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        summary = store.summarize(cid, tenant_id=tenant)
        # 若配置了 LLM，尝试用模型精炼摘要
        llm = getattr(request.app.state, "llm_provider", None)
        if llm is not None and hasattr(llm, "chat_completion"):
            try:
                msgs_text = "\n".join(
                    f"{m['role']}: {m['content'][:200]}" for m in conv["messages"]
                )
                r = await llm.chat_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": "请用不超过3句话中文总结这段对话。只输出摘要。",
                        },
                        {"role": "user", "content": msgs_text},
                    ]
                )
                if isinstance(r, str) and r.strip():
                    summary = r.strip()
            except Exception:  # noqa: BLE001
                pass
        return {"conversation_id": cid, "summary": summary}

    return router

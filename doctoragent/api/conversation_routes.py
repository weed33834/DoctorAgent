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
        raise HTTPException(status_code=401, detail="DOCTORAGENT_API_TOKEN not set; remote access denied")
    return None


def _store(request: Request) -> Any:
    s = getattr(request.app.state, "conversation_store", None)
    if s is None:
        raise HTTPException(status_code=503, detail="conversation store not configured")
    return s


def get_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/conversations", tags=["Conversations"])

    @router.post("", summary="Create a conversation")
    async def create_conversation(
        request: Request,
        payload: dict[str, Any] = Body(default={}),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        return _store(request).create(payload.get("title", "新对话"), payload.get("meta"))

    @router.get("", summary="List / search conversations")
    async def list_conversations(
        request: Request,
        q: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(50, ge=1, le=200),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        items = _store(request).list(q or "", limit=limit)
        return {"total": len(items), "items": items}

    @router.get("/{cid}", summary="Get a conversation with messages")
    async def get_conversation(
        cid: str, request: Request, _auth: Any = Depends(_auth_dependency)
    ) -> dict[str, Any]:
        conv = _store(request).get(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @router.post("/{cid}/messages", summary="Add a message to a conversation")
    async def add_message(
        cid: str, request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        msg = _store(request).add_message(cid, payload.get("role", "user"), payload.get("content", ""))
        if msg is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return msg

    @router.patch("/{cid}", summary="Rename a conversation")
    async def rename_conversation(
        cid: str, request: Request,
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        ok = _store(request).rename(cid, payload.get("title", ""))
        if not ok:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"ok": True}

    @router.post("/{cid}/fork", summary="Fork (branch) a conversation")
    async def fork_conversation(
        cid: str, request: Request,
        payload: dict[str, Any] = Body(default={}),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),
    ) -> dict[str, Any]:
        conv = _store(request).fork(cid, payload.get("title", ""))
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @router.post("/messages/{mid}/feedback", summary="Record like/dislike feedback")
    async def message_feedback(
        mid: str, request: Request,
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
        ok = _store(request).delete(cid)
        if not ok:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"ok": True}

    @router.get("/stats/overview", summary="Conversation store stats")
    async def conversation_stats(request: Request, _auth: Any = Depends(_auth_dependency)) -> dict[str, Any]:
        return _store(request).stats()

    return router

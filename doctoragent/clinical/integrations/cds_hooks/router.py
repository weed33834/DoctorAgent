"""FastAPI router mounting the CDS Hooks 2.0 endpoints.

Exposes:

* ``GET /cds-services`` → discovery document (``CDSService`` list).
* ``POST /cds-services/{service_id}`` → invoke the named service.

Authentication
-------------
CDS Hooks 2.0 says the EHR MAY send a bearer token via the standard
``Authorization: Bearer …`` header. We reuse the same static-token /
OIDC policy the rest of the API uses (env ``DOCTORAGENT_API_TOKEN`` or
``DOCTORAGENT_OIDC_*``) so a single server can serve both internal API
clients and an EHR.

When neither env var is set the endpoints are local-only (matching
``_is_local_request``), so a developer running locally without a token
can still exercise the integration from the same machine.

The router is mounted with **no URL prefix** (the spec mandates the
``/cds-services`` path verbatim) — it is included by the API server
alongside ``API_V1_PREFIX`` so existing versioned clients are unaffected.

Collaborator injection
----------------------
The clinical workflow needs an LLM provider, an optional FHIR client and
the tamper-evident audit logger. These are read off the FastAPI app state
(set by ``create_app``) under fixed attribute names so the router stays
decoupled from the server's internals:

* ``app.state.cds_llm_provider``
* ``app.state.cds_fhir_client``
* ``app.state.cds_audit_logger``

When an attribute is missing the service degrades gracefully (rules-only,
no extra FHIR reads, no audit trail) — exactly the behaviour the existing
``/clinical/analyze`` endpoint exhibits when the agent is missing pieces.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Optional FastAPI dependency. Importing this module is safe even when
# FastAPI is absent (the router attribute is set to ``None``).
_FASTAPI_AVAILABLE = False
try:
    from fastapi import (
        APIRouter,
        Depends,
        HTTPException,
        Request,
    )
    from fastapi.security import HTTPBearer

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover — FastAPI optional
    pass


# --------------------------------------------------------------------------- #
# Error-response helper (kept local to avoid a circular import on server.py)
# --------------------------------------------------------------------------- #
_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
}


def _error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    descriptions = {
        400: "Bad request (invalid CDS Hook payload).",
        401: "Authentication required or token invalid.",
        403: "Forbidden (sensitive operation denied).",
        404: "CDS service id not found in discovery document.",
        422: "Validation error (request body failed Pydantic validation).",
        500: "Internal server error.",
    }
    return {
        code: {
            "description": descriptions.get(code, "Error"),
            "content": {"application/json": {"schema": _ERROR_SCHEMA}},
        }
        for code in codes
    }


# --------------------------------------------------------------------------- #
# Auth guard — same semantics as server.py / advanced_routes.py
# --------------------------------------------------------------------------- #
# The loopback set + local-request check are shared via
# ``doctoragent.api.auth._guards`` so all three routers (API server, advanced
# enterprise router, CDS Hooks router) enforce one policy.
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

if _FASTAPI_AVAILABLE:
    _bearer_scheme = HTTPBearer(auto_error=False)

    async def _cds_auth_dependency(
        request: Request,  # type: ignore[name-defined]
        credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
    ) -> None:
        """Auth guard for CDS Hooks endpoints.

        Mirrors the policy implemented by ``doctoragent.api.server``:

        * When ``DOCTORAGENT_OIDC_ISSUER`` is set the bearer token MUST be a valid
          OIDC JWT (delegated to the server's OIDC authenticator). CDS Hooks
          2.0 mandates SMART-on-FHIR bearer tokens for EHR-launched services,
          which are exactly the JWTs OIDC validates here.
        * Otherwise, when ``DOCTORAGENT_API_TOKEN`` is set, a static bearer token
          comparison is used (constant-time).
        * When neither is set, only local (loopback / Unix socket) callers
          are accepted — matching the read-endpoint policy of the rest of
          the API. This keeps a default developer install secure-by-default.
        """
        # OIDC mode: delegate to the shared authenticator via lazy import.
        if _oidc_is_configured():
            from doctoragent.api.server import _authenticate_oidc

            await _authenticate_oidc(request, credentials)
            return

        expected = _resolve_token()
        if expected:
            provided = getattr(credentials, "credentials", None)
            if not _verify_bearer(provided, expected):
                raise HTTPException(  # type: ignore[misc]
                    status_code=401,
                    detail="CDS Hooks: missing or invalid bearer token",
                )
            return
        # No token configured → local-only.
        if not _is_local_request(request):
            raise HTTPException(  # type: ignore[misc]
                status_code=401,
                detail="CDS Hooks: server requires DOCTORAGENT_API_TOKEN for "
                "remote callers (set it or call from localhost)",
            )


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
if _FASTAPI_AVAILABLE:
    from doctoragent.clinical.integrations.cds_hooks._models import CDSHookRequest
    from doctoragent.clinical.integrations.cds_hooks.service import (
        CDSHookService,
        discover_services,
    )

    router = APIRouter(tags=["CDS Hooks"])

    @router.get(
        "/cds-services",
        summary="CDS Hooks service discovery",
        description=(
            "Returns the CDS Hooks 2.0 discovery document: the list of "
            "CDS services DoctorAgent exposes (patient-view, order-select, "
            "order-sign) along with their prefetch templates so the EHR "
            "can pre-fetch FHIR resources."
        ),
        responses=_error_responses(401),
    )
    async def cds_services_discovery(
        _auth: None = Depends(_cds_auth_dependency),
    ) -> dict[str, Any]:
        """``GET /cds-services`` → ``{"services": [...]}`` per spec."""
        return {"services": discover_services()}

    @router.post(
        "/cds-services/{service_id}",
        summary="Invoke a CDS Hooks service",
        description=(
            "Runs the named CDS Hooks service for the posted "
            "``CDSHookRequest``. DoctorAgent translates the hook context into "
            "the clinical workflow (deterministic rule engine + LLM "
            "specialists + guardrails), then maps the result back into "
            "CDS Cards (info / suggestion / app-link) for the EHR to render."
        ),
        responses=_error_responses(400, 401, 404, 422, 500),
    )
    async def cds_service_invoke(
        service_id: str,
        hook_request: CDSHookRequest,
        raw_request: Request,  # type: ignore[name-defined]
        _auth: None = Depends(_cds_auth_dependency),
    ) -> Any:
        """``POST /cds-services/{service_id}`` → ``CDSHookResponse``."""
        discovery = {svc["id"]: svc for svc in discover_services()}
        if service_id not in discovery:
            raise HTTPException(  # type: ignore[misc]
                status_code=404,
                detail=f"CDS service '{service_id}' not found",
            )
        expected_hook = discovery[service_id]["hook"]
        if hook_request.hook != expected_hook:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail=(
                    f"CDS service '{service_id}' expects hook '{expected_hook}' "
                    f"but received '{hook_request.hook}'"
                ),
            )

        # Pull collaborators off FastAPI app.state (set by create_app).
        # ``getattr(app.state, name, None)`` lets unit tests call the router
        # without wiring the full agent.
        app_state = getattr(raw_request, "app", None)
        state = getattr(app_state, "state", None) if app_state is not None else None
        llm_provider = getattr(state, "cds_llm_provider", None) if state else None
        fhir_client = getattr(state, "cds_fhir_client", None) if state else None
        audit_logger = getattr(state, "cds_audit_logger", None) if state else None

        service = CDSHookService(
            llm_provider=llm_provider,
            fhir_client=fhir_client,
            audit_logger=audit_logger,
        )
        return await service.invoke(hook_request)

else:  # pragma: no cover — FastAPI not installed
    router = None  # type: ignore[assignment]

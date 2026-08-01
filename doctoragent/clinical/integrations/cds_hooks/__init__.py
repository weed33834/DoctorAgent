"""HL7 CDS Hooks 2.0 integration for the DoctorAgent clinical workflow.

Implements the two CDS Hooks endpoints:

* ``GET /cds-services`` — discovery: returns the list of CDS services
  DoctorAgent exposes (``doctoragent-patient-view``, ``doctoragent-order-select``,
  ``doctoragent-order-sign``).
* ``POST /cds-services/{id}`` — invocation: the EHR posts a
  :class:`CDSHookRequest` and DoctorAgent runs the deterministic rule engine +
  LLM specialist fan-out, returning :class:`CDSHookResponse` (cards with
  ``info`` / ``suggestion`` / ``app-link`` semantics).

The Pydantic models in :mod:`doctoragent.clinical.integrations.cds_hooks._models`
are pure data — no FastAPI dependency — so they can be reused by the workflow
layer and unit tests. The HTTP layer (FastAPI router) lives in
:mod:`doctoragent.clinical.integrations.cds_hooks.router`.

Spec reference: https://cds-hooks.hl7.org/2.0/
"""

from __future__ import annotations

from typing import Any

from doctoragent.clinical.integrations.cds_hooks._models import (
    Action,
    Card,
    CardIndicator,
    CardSource,
    CDSHookRequest,
    CDSHookResponse,
    CDSService,
    Link,
    Suggestion,
    SupportedHook,
)
from doctoragent.clinical.integrations.cds_hooks.service import (
    CDSHookService,
    discover_services,
    translate_request_to_workflow,
    translate_result_to_response,
)

__all__ = [
    "Action",
    "CDSHookRequest",
    "CDSHookResponse",
    "CDSHookService",
    "CDSService",
    "Card",
    "CardIndicator",
    "CardSource",
    "Link",
    "Suggestion",
    "SupportedHook",
    "discover_services",
    "translate_request_to_workflow",
    "translate_result_to_response",
]


def get_router() -> Any | None:
    """Lazy import of the FastAPI router.

    Returns ``None`` when FastAPI is not installed so the package stays
    importable on minimal installs (mirrors the pattern used by the rest of
    the API package).
    """
    try:
        from doctoragent.clinical.integrations.cds_hooks.router import router
    except ImportError:  # pragma: no cover — FastAPI optional
        return None
    return router

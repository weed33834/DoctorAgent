"""HL7 CDS Hooks 2.0 — Pydantic request/response models.

This module is **pure data models** (no FastAPI dependency), so the schema
types can be unit-tested, used by the workflow layer and serialised into JSON
without dragging the HTTP server into a CLI / batch job.

Spec reference: https://cds-hooks.hl7.org/2.0/

Key shapes
----------
* :class:`CDSService` (discovery element) — id, hook, title, description,
  ``prefetch`` template, usage licence.
* :class:`CDSHookRequest` (EHR → CDS Service) — hook, hookInstance,
  ``context`` (free-form, hook-specific), ``prefetch`` (FHIR Bundle cache),
  ``fhirServer`` + ``fhirAuthorization`` (SMART-on-FHIR).
* :class:`Card` / :class:`Suggestion` / :class:`Action` / :class:`Link`
  (CDS Service → EHR) — the four card types ``info`` / ``suggestion`` /
  ``app-link`` and the inline suggestion actions a clinician can accept.
* :class:`CDSHookResponse` — the wrapper an EHR expects
  (``cards`` + optional ``systemActions``).

The models are deliberately permissive (``extra="allow"``) so vendor
extensions don't break parsing — CDS Hooks 2.0 explicitly permits EHRs to
attach vendor-specific context fields.
"""

# ruff: noqa: N815 — CDS Hooks 2.0 spec mandates camelCase JSON keys
# (hookInstance, fhirServer, fhirAuthorization, appContext, overrideReasons,
# systemActions, objectCode). Renaming would break EHR interoperability.

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Action",
    "CDSHookRequest",
    "CDSHookResponse",
    "CDSService",
    "Card",
    "CardIndicator",
    "CardSource",
    "Link",
    "Suggestion",
    "SupportedHook",
]


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class CardIndicator(str, Enum):
    """CDS Hooks 2.0 ``Card.indicator`` — UI severity hint for the EHR."""

    INFO = "info"
    WARN = "warning"
    CRITICAL = "critical"
    # ``error`` is not part of CDS Hooks 2.0 but kept for forward compat with
    # vendor EHRs that surface a fourth severity level.
    ERROR = "error"


class SupportedHook(str, Enum):
    """Hook ids we actively implement (a subset of the CDS Hooks catalog).

    The full catalog (https://cds-hooks.hl7.org/hooks) defines many more
    hooks (``medication-prescribe``, ``encounter-discharge``, …); we expose
    the three that map cleanly to the clinical workflow today and let other
    hook ids fall through to a generic handler so EHRs aren't blocked if
    they invoke an unlisted hook.
    """

    PATIENT_VIEW = "patient-view"
    ORDER_SELECT = "order-select"
    ORDER_SIGN = "order-sign"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
class CDSService(BaseModel):
    """A single entry in the ``GET /cds-services`` discovery response.

    Spec: https://cds-hooks.hl7.org/2.0/#cds-service
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Unique service id (URL-safe). Becomes the path segment "
        "in POST /cds-services/{id}.",
    )
    hook: str = Field(
        ...,
        description="Hook id this service responds to (e.g. 'patient-view').",
    )
    title: str | None = Field(default=None, description="Human-readable service name (EHR UI).")
    description: str | None = Field(
        default=None,
        description="What the service does; shown to the clinician before "
        "invocation. Required by CDS Hooks 2.0 in production deployments.",
    )
    prefetch: dict[str, str] | None = Field(
        default=None,
        description="FHIR search query templates the EHR should pre-fetch "
        "(e.g. {'patient': 'Patient/{{context.patientId}}'}).",
    )
    objectCode: str | None = Field(
        default=None,
        description="Optional scopes the service requires from the SMART "
        "launch token (CDS Hooks 2.0 §scope-discovery).",
    )
    usage_requirement: str | None = Field(
        default=None,
        alias="usageRequirement",
        description="Optional human-readable pre-condition text.",
    )


# --------------------------------------------------------------------------- #
# Request (EHR → Service)
# --------------------------------------------------------------------------- #
class CDSHookRequest(BaseModel):
    """Body of ``POST /cds-services/{id}``.

    Spec: https://cds-hooks.hl7.org/2.0/#invoking-cds-services
    """

    model_config = ConfigDict(extra="allow")

    hook: str = Field(..., description="The hook id that triggered this call.")
    hookInstance: str = Field(
        ...,
        description="Server-generated UUID for this invocation; the same "
        "value MUST be echoed in feedback / override events.",
    )
    fhirServer: str | None = Field(
        default=None,
        description="Base URL of the calling EHR's FHIR endpoint. When set, "
        "the CDS service can issue FHIR reads (if it has the SMART token).",
    )
    fhirAuthorization: dict[str, Any] | None = Field(
        default=None,
        description="SMART-on-FHIR OAuth2 access token payload (token, "
        "scope, expires_in, …). Treated as opaque & never logged.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Hook-specific context (patientId, userId, "
        "selections, draftOrders, …). Vendor extensions allowed.",
    )
    prefetch: dict[str, Any] | None = Field(
        default=None,
        description="EHR-pre-fetched FHIR resources keyed by the keys "
        "declared in the service's ``prefetch`` template.",
    )


# --------------------------------------------------------------------------- #
# Response (Service → EHR)
# --------------------------------------------------------------------------- #
class Link(BaseModel):
    """A hyperlink presented inside an ``app-link`` card."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(..., description="Anchor text.")
    url: str = Field(..., description="Absolute URL.")
    type: str | None = Field(default="text/html", description="MIME type (default text/html).")
    appContext: str | None = Field(
        default=None,
        description="Opaque context the launched SMART app receives via its launch flow.",
    )


class Action(BaseModel):
    """A single suggested FHIR transaction (create/update/delete)."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(
        ...,
        description="'create' | 'update' | 'delete'.",
    )
    description: str = Field(..., description="Human-readable action label.")
    resource: dict[str, Any] | None = Field(
        default=None,
        description="FHIR resource for create/update actions.",
    )


class Suggestion(BaseModel):
    """A clinician-accept-or-reject suggestion inside a card."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(..., description="Short label rendered as the suggestion button.")
    uuid: str | None = Field(
        default=None,
        description="Optional id for telemetry / override correlation.",
    )
    actions: list[Action] = Field(
        default_factory=list,
        description="FHIR transactions applied when the clinician "
        "accepts the suggestion (often 0..1 actions).",
    )


class CardSource(BaseModel):
    """Attribution / provenance for a card."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(..., description="Source name (e.g. 'DoctorAgent CDS').")
    url: str | None = Field(default=None, description="Deeplink to the source / context.")
    icon: str | None = Field(default=None, description="Icon URL (https).")


class Card(BaseModel):
    """A single CDS Hooks card returned to the EHR.

    Spec: https://cds-hooks.hl7.org/2.0/#card
    """

    model_config = ConfigDict(extra="allow")

    uuid: str | None = Field(
        default=None,
        description="Server-generated id for telemetry / override "
        "correlation. Auto-filled when omitted by the builder.",
    )
    summary: str = Field(
        ...,
        min_length=1,
        max_length=140,
        description="One-line headline shown in the EHR UI banner.",
    )
    detail: str | None = Field(
        default=None,
        description="Markdown body with the full rationale, citations and "
        "disclaimer. EHRs render this in a popover / side panel.",
    )
    indicator: CardIndicator = Field(
        default=CardIndicator.INFO,
        description="UI severity hint (info | warning | critical).",
    )
    source: CardSource | None = Field(default=None, description="Attribution of the card.")
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="Acceptable actions the clinician can apply inline. "
        "Empty for pure ``info`` / ``app-link`` cards.",
    )
    links: list[Link] = Field(
        default_factory=list,
        description="Hyperlinks for ``app-link`` cards.",
    )
    overrideReasons: list[dict[str, Any]] | None = Field(
        default=None,
        description="CDS Hooks 2.0: reasons the EHR may show if the clinician dismisses the card.",
    )


class CDSHookResponse(BaseModel):
    """Top-level response body for ``POST /cds-services/{id}``.

    Spec: https://cds-hooks.hl7.org/2.0/#cds-service-response
    """

    model_config = ConfigDict(extra="allow")

    cards: list[Card] = Field(
        default_factory=list,
        description="Cards to surface to the clinician (order preserved).",
    )
    systemActions: list[Action] | None = Field(
        default=None,
        description="Server-only FHIR transactions applied silently "
        "(no clinician UI). Used for guardrail auto-blocks gated behind "
        "policy.",
    )

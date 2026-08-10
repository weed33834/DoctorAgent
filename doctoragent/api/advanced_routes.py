"""Advanced API routes for agent features.

Includes endpoints for:
- Knowledge Graph management (build, query, subgraph)
- Advanced RAG (agentic RAG, query routing, corrective RAG)
- Security analytics (posture, anomalies, risk scores)
- Shamir secret sharing (split/reconstruct keys)
- Zero-trust access evaluation
- DLP scanning
- Agent management (trajectory, cancel, skills)
- Key management (rotate, status)
- DAG workflow execution
- Task scheduler management
- Compliance operations (DSAR export/erase)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional FastAPI dependency (mirrors the guard in ``server.py``)
# ---------------------------------------------------------------------------

_FASTAPI_AVAILABLE = False
try:
    from fastapi import (
        APIRouter,
        Body,
        Depends,
        File,
        HTTPException,
        Query,
        Request,
        Response,
        UploadFile,
    )
    from fastapi.security import HTTPBearer

    _bearer_scheme = HTTPBearer(auto_error=False)
    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - FastAPI is optional
    _bearer_scheme = None  # type: ignore[assignment]


# ===========================================================================
# OpenAPI error-response documentation helper
# ===========================================================================

_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
}


def _error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    """Build a ``responses=`` mapping documenting common error codes."""
    descriptions = {
        400: "Bad request (invalid identifier or payload).",
        401: "Authentication required or token invalid.",
        403: "Forbidden (sensitive operation denied, e.g. token not configured).",
        404: "Resource not found.",
        422: "Validation error (request body failed Pydantic validation).",
        500: "Internal server error.",
        503: "Service unavailable (required subsystem not configured).",
    }
    return {
        code: {
            "description": descriptions.get(code, "Error"),
            "content": {"application/json": {"schema": _ERROR_SCHEMA}},
        }
        for code in codes
    }


# ===========================================================================
# Authentication (self-contained bearer-token guard)
# ---------------------------------------------------------------------------
# Mirrors the policy implemented in ``doctoragent.api.server``:
#   * read endpoints  -> token required when configured, otherwise local-only;
#   * write endpoints -> fail-closed (token always required).
# When ``DOCTORAGENT_OIDC_ISSUER`` is configured the bearer token is validated
# as an OIDC JWT via the server's shared authenticator (lazy import keeps the
# router self-contained so it can be ``include_router``-ed without import-order
# issues). This matches the CDS Hooks router's delegation pattern so every
# authenticated surface honours OIDC consistently.
#
# The loopback set, token resolver, OIDC-configured flag, local-request check
# and bearer extractor are imported from the shared ``_guards`` module so this
# router cannot drift from the API server's policy.
# ===========================================================================

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


async def _auth_dependency(
    request: Request,  # type: ignore[name-defined]
    credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
) -> Any:
    """Auth dependency for **read-only** endpoints.

    When ``DOCTORAGENT_OIDC_ISSUER`` is configured the bearer token MUST be a
    valid OIDC JWT (delegated to the server's OIDC authenticator). Otherwise,
    when ``DOCTORAGENT_API_TOKEN`` is configured the bearer token must match.
    When neither is configured the endpoint is fail-closed to local requests.
    """
    # OIDC mode: delegate to the shared authenticator via lazy import.
    if _oidc_is_configured():
        from doctoragent.api.server import _authenticate_oidc

        return await _authenticate_oidc(request, credentials)
    expected = _resolve_token()
    if expected is not None:
        provided = getattr(credentials, "credentials", None)
        if not _verify_bearer(provided, expected):
            raise HTTPException(  # type: ignore[misc]
                status_code=401, detail="Invalid or missing authentication token"
            )
        return provided
    if not _is_local_request(request):
        raise HTTPException(  # type: ignore[misc]
            status_code=401, detail="DOCTORAGENT_API_TOKEN not set; remote access denied"
        )
    return None


async def _sensitive_auth_dependency(
    request: Request,  # type: ignore[name-defined]
    credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
) -> Any:
    """Auth dependency for **write/sensitive** endpoints (fail-closed).

    OIDC-verified tokens are accepted when configured. Otherwise the static
    ``DOCTORAGENT_API_TOKEN`` must be set, and sensitive endpoints are denied
    by default when it is unset so an unconfigured deployment cannot
    accidentally expose decryption, key-rotation or sync-trigger capabilities.
    """
    # OIDC mode: delegate to the shared authenticator via lazy import.
    if _oidc_is_configured():
        from doctoragent.api.server import _authenticate_oidc

        return await _authenticate_oidc(request, credentials)
    expected = _resolve_token()
    if expected is None:
        raise HTTPException(  # type: ignore[misc]
            status_code=403,
            detail="Authentication required for this endpoint: set DOCTORAGENT_API_TOKEN",
        )
    provided = getattr(credentials, "credentials", None)
    if not _verify_bearer(provided, expected):
        raise HTTPException(  # type: ignore[misc]
            status_code=401, detail="Invalid or missing authentication token"
        )
    return provided


def _service_unavailable(name: str) -> HTTPException:  # type: ignore[name-defined]
    """Build a 503 error for a missing subsystem."""
    return HTTPException(  # type: ignore[misc]
        status_code=503,
        detail=f"{name} subsystem is not available on this server",
    )


# ===========================================================================
# Service resolution helpers (getter pattern over ``request.app.state``)
# ---------------------------------------------------------------------------
# The advanced router is mounted by the host server.  Rather than capturing
# the agent/config/pool via closures (as ``server.py`` does for its own
# routes) we resolve them lazily from ``request.app.state`` and construct the
# heavier advanced services on first use, caching each singleton back onto
# ``app.state`` so subsequent requests reuse it.
# ===========================================================================


def _get_agent(request: Request) -> Any:  # type: ignore[name-defined]
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        agent = getattr(request.app.state, "orchestrator", None)
    return agent


def _get_config(request: Request) -> Any:  # type: ignore[name-defined]
    config = getattr(request.app.state, "config", None)
    if config is None:
        agent = _get_agent(request)
        config = getattr(agent, "config", None) if agent is not None else None
    return config


def _get_task_store(request: Request) -> Any:  # type: ignore[name-defined]
    store = getattr(request.app.state, "task_store", None)
    if store is None:
        agent = _get_agent(request)
        if agent is not None:
            store = getattr(agent, "task_store", None)
    return store


def _get_llm_provider(request: Request) -> Any:  # type: ignore[name-defined]
    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None:
        agent = _get_agent(request)
        if agent is not None:
            classifier = getattr(agent, "classifier", None)
            provider = getattr(classifier, "provider", None) if classifier is not None else None
    return provider


def _get_audit_logger(request: Request) -> Any:  # type: ignore[name-defined]
    audit = getattr(request.app.state, "audit_logger", None)
    if audit is None:
        agent = _get_agent(request)
        if agent is not None:
            audit = getattr(agent, "audit_logger", None)
    return audit


def _get_master_key_provider(request: Request) -> Any:  # type: ignore[name-defined]
    provider = getattr(request.app.state, "master_key_provider", None)
    if provider is None:
        agent = _get_agent(request)
        if agent is not None:
            provider = getattr(agent, "master_key_provider", None)
    return provider


def _get_pipeline_pool(request: Request) -> Any:  # type: ignore[name-defined]
    return getattr(request.app.state, "pipeline_pool", None)


def _tenant_id(request: Request) -> str:  # type: ignore[name-defined]
    store = _get_task_store(request)
    return getattr(store, "_tenant_id", None) or getattr(store, "tenant_id", None) or "default"


def _get_or_create(
    request: Request,  # type: ignore[name-defined]
    attr: str,
    factory: Callable[[], Any],
) -> Any:
    """Return a cached singleton from ``app.state`` or build + cache it."""
    state = request.app.state
    instance = getattr(state, attr, None)
    if instance is None:
        try:
            instance = factory()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to construct %s", attr)
            return None
        setattr(state, attr, instance)
    return instance


def _get_state_dict(request: Request, attr: str) -> dict[str, Any]:  # type: ignore[name-defined]
    """Return (and lazily create) a dict-style registry on ``app.state``."""
    state = request.app.state
    registry = getattr(state, attr, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(state, attr, registry)
    return registry


def _serialize(obj: Any) -> Any:
    """Best-effort conversion of dataclasses/enums to JSON-safe structures."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    # Enums with a ``.value`` attribute (e.g. StrEnum).
    value = getattr(obj, "value", None)
    if value is not None and not isinstance(obj, type):
        return value
    return obj


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any]:
    """sqlite3 row factory converting a row to a plain dict."""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _slug(name: str) -> str:
    """Convert a free-form name to a URL-safe slug."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return s or "template"


def _extract_variables(template: str) -> list[str]:
    """Extract ``{var}`` placeholders from a template string."""
    return list(dict.fromkeys(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template)))


# ===========================================================================
# Pydantic request / response models
# ===========================================================================

# ── Knowledge Graph ────────────────────────────────────────────────────────


class KGBuildRequest(BaseModel):
    """Request body for POST /kg/build."""

    limit: int | None = Field(
        default=None,
        ge=1,
        description="Optional cap on the number of document chunks to process.",
    )


class KGBuildResponse(BaseModel):
    """Response for POST /kg/build."""

    model_config = ConfigDict(extra="allow")

    chunks_processed: int = 0
    entities_extracted: int = 0
    relations_extracted: int = 0
    total_entities: int = 0
    total_relations: int = 0
    message: str = ""


class KGQueryRequest(BaseModel):
    """Request body for POST /kg/query."""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)


class KGQueryResponse(BaseModel):
    """Response for POST /kg/query."""

    query: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)


class KGEntityResponse(BaseModel):
    """Response for GET /kg/entity/{name}."""

    model_config = ConfigDict(extra="allow")

    name: str = ""
    entity_type: str = "concept"
    properties: dict[str, Any] = Field(default_factory=dict)
    source_doc_ids: list[str] = Field(default_factory=list)


class KGSubgraphRequest(BaseModel):
    """Request body for POST /kg/subgraph."""

    entity_name: str = Field(min_length=1, max_length=500)
    depth: int = Field(default=2, ge=0, le=10)


class KGSubgraphResponse(BaseModel):
    """Response for POST /kg/subgraph."""

    seed: str = ""
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)


# ── Advanced RAG ───────────────────────────────────────────────────────────


class RAGRouteRequest(BaseModel):
    """Request body for POST /rag/route."""

    query: str = Field(min_length=1, max_length=2000)


class RAGRouteResponse(BaseModel):
    """Response for POST /rag/route."""

    query: str = ""
    query_type: str = "factual"
    strategy: str = "hybrid"
    retrieval_config: dict[str, Any] = Field(default_factory=dict)


class AgenticRAGRequest(BaseModel):
    """Request body for POST /rag/agentic."""

    query: str = Field(min_length=1, max_length=2000)
    max_iterations: int = Field(default=5, ge=1, le=20)
    top_k: int = Field(default=5, ge=1, le=50)


class AgenticRAGResponse(BaseModel):
    """Response for POST /rag/agentic."""

    query: str = ""
    answer: str = ""
    iterations: int = 0
    documents: list[dict[str, Any]] = Field(default_factory=list)
    action_history: list[dict[str, Any]] = Field(default_factory=list)


class CorrectiveRAGRequest(BaseModel):
    """Request body for POST /rag/correct."""

    query: str = Field(min_length=1, max_length=2000)
    max_iterations: int = Field(default=2, ge=0, le=5)
    top_k: int = Field(default=5, ge=1, le=50)


class CorrectiveRAGResponse(BaseModel):
    """Response for POST /rag/correct."""

    model_config = ConfigDict(extra="allow")

    query: str = ""
    original_query: str = ""
    docs: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    iterations: int = 0
    corrected: bool = False
    trace: list[dict[str, Any]] = Field(default_factory=list)


class RAGCacheStatsResponse(BaseModel):
    """Response for GET /rag/cache/stats."""

    model_config = ConfigDict(extra="allow")

    hit_rate: float = 0.0
    size: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    max_size: int = 0
    ttl_seconds: float = 0.0


# ── Security Analytics ─────────────────────────────────────────────────────


class SecurityPostureResponse(BaseModel):
    """Response for GET /security/posture."""

    model_config = ConfigDict(extra="allow")

    total_events: int = 0
    anomalies_count: int = 0
    high_risk_subjects: list[str] = Field(default_factory=list)
    avg_risk_score: float = 0.0


class AnomalyItem(BaseModel):
    """A single anomaly record."""

    event_id: str = ""
    event_type: str = ""
    subject_id: str = ""
    resource: str = ""
    severity: str = "INFO"
    risk_score: float = 0.0
    timestamp: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AnomaliesResponse(BaseModel):
    """Response for GET /security/anomalies."""

    total: int = 0
    anomalies: list[AnomalyItem] = Field(default_factory=list)


class RiskScoreResponse(BaseModel):
    """Response for GET /security/risk/{subject_id}."""

    subject_id: str = ""
    risk_score: float = 0.0


class RiskTrendResponse(BaseModel):
    """Response for GET /security/risk-trend."""

    days: int = 7
    trend: list[dict[str, Any]] = Field(default_factory=list)


# ── Shamir Secret Sharing ──────────────────────────────────────────────────


class ShamirSplitRequest(BaseModel):
    """Request body for POST /shamir/split."""

    secret_hex: str = Field(min_length=2, description="Hex-encoded secret to split")
    threshold: int = Field(ge=1, description="Minimum shares required to reconstruct (k)")
    total: int = Field(ge=1, description="Total number of shares to generate (n)")


class ShamirSplitResponse(BaseModel):
    """Response for POST /shamir/split."""

    shares: list[str] = Field(default_factory=list)
    threshold: int = 0
    total: int = 0


class ShamirReconstructRequest(BaseModel):
    """Request body for POST /shamir/reconstruct."""

    shares: list[str] = Field(min_length=1, description="Share tokens produced by /shamir/split")


class ShamirReconstructResponse(BaseModel):
    """Response for POST /shamir/reconstruct."""

    secret_hex: str = ""


# ── Zero-Trust ─────────────────────────────────────────────────────────────


class ZTEvaluateRequest(BaseModel):
    """Request body for POST /zt/evaluate."""

    subject_id: str = Field(min_length=1, max_length=256)
    resource_path: str = Field(min_length=1, max_length=1024)
    action: str = Field(min_length=1, max_length=64)
    device_id: str | None = Field(default=None, max_length=256)
    ip_address: str | None = Field(default=None, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)


class ZTEvaluateResponse(BaseModel):
    """Response for POST /zt/evaluate."""

    allowed: bool = False
    trust_level: str = "none"
    reason: str = ""
    conditions: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    timestamp: str | None = None


class ZTDeviceRequest(BaseModel):
    """Request body for POST /zt/device."""

    device_id: str = Field(min_length=1, max_length=256)
    os_version: str = ""
    disk_encrypted: bool = False
    firewall_enabled: bool = False
    trust_score: float = Field(default=0.0, ge=0.0, le=1.0)


class ZTDeviceResponse(BaseModel):
    """Response for POST /zt/device."""

    message: str = ""
    device_id: str = ""


class ZTHistoryResponse(BaseModel):
    """Response for GET /zt/history."""

    total: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)


# ── DLP ────────────────────────────────────────────────────────────────────


class DLPScanRequest(BaseModel):
    """Request body for POST /dlp/scan and POST /dlp/redact."""

    text: str = Field(min_length=1, max_length=1_000_000)


class DLPMatchItem(BaseModel):
    """A single detected sensitive-data match."""

    data_type: str = ""
    value: str = ""
    start_pos: int = 0
    end_pos: int = 0
    confidence: float = 0.0
    masked_value: str = ""


class DLPScanResponse(BaseModel):
    """Response for POST /dlp/scan."""

    count: int = 0
    matches: list[DLPMatchItem] = Field(default_factory=list)


class DLPRedactResponse(BaseModel):
    """Response for POST /dlp/redact."""

    redacted_text: str = ""
    count: int = 0
    matches: list[DLPMatchItem] = Field(default_factory=list)


# ── Agent Management ───────────────────────────────────────────────────────


class AgentTrajectoryResponse(BaseModel):
    """Response for GET /agent/trajectory/{task_id}."""

    task_id: str = ""
    found: bool = False
    steps: list[dict[str, Any]] = Field(default_factory=list)
    total_tool_calls: int = 0
    total_time_ms: float = 0.0


class AgentSkillsResponse(BaseModel):
    """Response for GET /agent/skills."""

    total: int = 0
    skills: list[dict[str, Any]] = Field(default_factory=list)


class AgentEvolveRequest(BaseModel):
    """Request body for POST /agent/evolve."""

    task_ids: list[str] = Field(
        default_factory=list,
        description="Task IDs whose stored trajectories should drive evolution.",
    )


class AgentEvolveResponse(BaseModel):
    """Response for POST /agent/evolve."""

    model_config = ConfigDict(extra="allow")

    analyzed: int = 0
    experiences_stored: int = 0
    lessons: list[Any] = Field(default_factory=list)
    optimized_prompt: str = ""
    message: str = ""


class AgentToTResponse(BaseModel):
    """Response for GET /agent/tot."""

    query: str = ""
    answer: str = ""
    best_path: list[dict[str, Any]] = Field(default_factory=list)
    tree: dict[str, Any] | None = None


# ── Key Management ─────────────────────────────────────────────────────────


class KeyRotateRequest(BaseModel):
    """Request body for POST /keys/rotate."""

    reason: str = Field(default="manual", max_length=256)


class KeyRotateResponse(BaseModel):
    """Response for POST /keys/rotate."""

    rotated: bool = False
    reason: str = ""
    message: str = ""


class KeyStatusResponse(BaseModel):
    """Response for GET /keys/status."""

    model_config = ConfigDict(extra="allow")

    provider_type: str = ""
    exists: bool = False
    auto_rotator_running: bool = False
    last_rotation: str | None = None
    grace_keys: int = 0


# ── DAG Workflow ───────────────────────────────────────────────────────────


class DAGTaskSpec(BaseModel):
    """A single task definition submitted for DAG execution.

    Callables cannot be serialised over HTTP, so the server attaches a
    default no-op executor; real work is performed by callables registered
    server-side (matched by ``id`` when available).
    """

    id: str = Field(min_length=1, max_length=128)
    name: str = ""
    dependencies: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=3, ge=0, le=10)
    timeout_seconds: float | None = Field(default=None, gt=0)


class DAGExecuteRequest(BaseModel):
    """Request body for POST /dag/execute."""

    tasks: list[DAGTaskSpec] = Field(min_length=1, max_length=500)


class DAGExecuteResponse(BaseModel):
    """Response for POST /dag/execute."""

    dag_id: str = ""
    status: dict[str, Any] = Field(default_factory=dict)


class DAGStatusResponse(BaseModel):
    """Response for GET /dag/status/{dag_id}."""

    dag_id: str = ""
    found: bool = False
    status: dict[str, Any] = Field(default_factory=dict)


# ── Task Scheduler ─────────────────────────────────────────────────────────


class SchedulerStatusResponse(BaseModel):
    """Response for GET /scheduler/status."""

    model_config = ConfigDict(extra="allow")

    queue: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


# ── Compliance ─────────────────────────────────────────────────────────────


class ComplianceExportRequest(BaseModel):
    """Request body for POST /compliance/export (DSAR access)."""

    subject_id: str = Field(min_length=1, max_length=256)


class ComplianceExportResponse(BaseModel):
    """Response for POST /compliance/export."""

    model_config = ConfigDict(extra="allow")

    subject_id: str = ""
    exported_at: str = ""
    document_count: int = 0
    documents: list[dict[str, Any]] = Field(default_factory=list)


class ComplianceEraseRequest(BaseModel):
    """Request body for POST /compliance/erase (DSAR erasure)."""

    subject_id: str = Field(min_length=1, max_length=256)
    delete_files: bool = True


class ComplianceEraseResponse(BaseModel):
    """Response for POST /compliance/erase."""

    subject_id: str = ""
    erased_count: int = 0
    message: str = ""


class ConsentsResponse(BaseModel):
    """Response for GET /compliance/consents."""

    total: int = 0
    consents: list[dict[str, Any]] = Field(default_factory=list)


class MessageResponse(BaseModel):
    """Generic ``{"message": ...}`` envelope."""

    message: str = ""


# ── Memory Management ──────────────────────────────────────────────────────


class MemoryStoreFactRequest(BaseModel):
    """Request body for POST /memory/facts."""

    content: str = Field(min_length=1, max_length=8000)
    memory_type: str = Field(default="semantic", description="semantic | episodic | procedural")
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryRecallRequest(BaseModel):
    """Request body for POST /memory/recall."""

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=50)


class MemoryListResponse(BaseModel):
    """Response for GET /memory/facts / episodes / sessions."""

    model_config = ConfigDict(extra="allow")

    total: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)


# ── Lifecycle Hooks ────────────────────────────────────────────────────────


class HookRegisterRequest(BaseModel):
    """Request body for POST /hooks."""

    name: str = Field(min_length=1, max_length=128)
    hook_type: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=0, ge=-1000, le=1000)
    enabled: bool = True
    description: str = Field(default="", max_length=512)
    # Script-based hook: a tiny Python expression evaluated against the
    # HookContext (``ctx``). Empty means a no-op marker hook.
    script: str = Field(default="", max_length=4000)


class HookListResponse(BaseModel):
    """Response for GET /hooks."""

    total: int = 0
    hook_types: list[str] = Field(default_factory=list)
    hooks: list[dict[str, Any]] = Field(default_factory=list)


# ── Observability ──────────────────────────────────────────────────────────


class ObservabilitySnapshotResponse(BaseModel):
    """Response for GET /observability/snapshot."""

    model_config = ConfigDict(extra="allow")

    metrics: dict[str, Any] = Field(default_factory=dict)
    traces: list[dict[str, Any]] = Field(default_factory=list)
    recent_logs: list[dict[str, Any]] = Field(default_factory=list)
    health: dict[str, Any] = Field(default_factory=dict)


# ── Reinforcement Learning / Feedback ─────────────────────────────────────


class RLFeedbackRequest(BaseModel):
    """Request body for POST /rl/feedback."""

    task_id: str = Field(default="", max_length=128)
    query: str = Field(default="", max_length=4000)
    response: str = Field(default="", max_length=8000)
    rating: int = Field(..., ge=-1, le=1, description="1 = positive, 0 = neutral, -1 = negative")
    comment: str = Field(default="", max_length=2000)
    user_id: str = Field(default="anonymous", max_length=128)


class RLFeedbackResponse(BaseModel):
    """Response for POST /rl/feedback."""

    recorded: bool = True
    feedback_id: str = ""
    reward: float = 0.0
    message: str = ""


class RLPreferencesResponse(BaseModel):
    """Response for GET /rl/preferences."""

    total: int = 0
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    average_reward: float = 0.0
    recent: list[dict[str, Any]] = Field(default_factory=list)


class RLPolicyResponse(BaseModel):
    """Response for GET /rl/policy."""

    model_config = ConfigDict(extra="allow")

    policy_version: str = ""
    total_experiences: int = 0
    total_feedback: int = 0
    average_reward: float = 0.0
    top_tools: list[dict[str, Any]] = Field(default_factory=list)
    top_lessons: list[dict[str, Any]] = Field(default_factory=list)


# ── Multi-Agent Collaboration ──────────────────────────────────────────────


class CollabDelegateRequest(BaseModel):
    """Request body for POST /collab/delegate."""

    task: str = Field(min_length=1, max_length=4000)
    role: str = Field(default="specialist", max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)
    max_rounds: int = Field(default=3, ge=1, le=20)


class CollabDelegateResponse(BaseModel):
    """Response for POST /collab/delegate."""

    model_config = ConfigDict(extra="allow")

    delegated: bool = True
    role: str = ""
    message_id: str = ""
    response: str = ""
    rounds: int = 0


class CollabAgentsResponse(BaseModel):
    """Response for GET /collab/agents."""

    total: int = 0
    agents: list[dict[str, Any]] = Field(default_factory=list)


class CollabMessagesResponse(BaseModel):
    """Response for GET /collab/messages."""

    total: int = 0
    messages: list[dict[str, Any]] = Field(default_factory=list)


# ── Plugin Management ──────────────────────────────────────────────────────


class PluginListResponse(BaseModel):
    """Response for GET /plugins."""

    total: int = 0
    plugins: list[dict[str, Any]] = Field(default_factory=list)


# ── A/B Experiments ────────────────────────────────────────────────────────


class ExperimentCreateRequest(BaseModel):
    """Request body for POST /experiments."""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    variants: list[dict[str, Any]] = Field(default_factory=list)
    metric: str = Field(default="reward", max_length=64)
    traffic_pct: int = Field(default=100, ge=1, le=100)


class ExperimentResponse(BaseModel):
    """Response for GET /experiments and POST /experiments."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    description: str = ""
    status: str = "running"
    variants: list[dict[str, Any]] = Field(default_factory=list)
    metric: str = ""
    traffic_pct: int = 100
    results: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ExperimentsListResponse(BaseModel):
    """Response for GET /experiments."""

    total: int = 0
    experiments: list[dict[str, Any]] = Field(default_factory=list)


# ── Prompt Templates ───────────────────────────────────────────────────────


class PromptTemplateCreateRequest(BaseModel):
    """Request body for POST /prompts."""

    name: str = Field(min_length=1, max_length=128)
    template: str = Field(min_length=1, max_length=16000)
    variables: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=1024)
    tags: list[str] = Field(default_factory=list)


class PromptTemplateResponse(BaseModel):
    """Response for GET /prompts/{id}."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    template: str = ""
    variables: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    version: int = 1
    created_at: str = ""
    updated_at: str = ""


class PromptTemplatesListResponse(BaseModel):
    """Response for GET /prompts."""

    total: int = 0
    templates: list[dict[str, Any]] = Field(default_factory=list)


class PromptRenderRequest(BaseModel):
    """Request body for POST /prompts/{id}/render."""

    variables: dict[str, Any] = Field(default_factory=dict)


class PromptRenderResponse(BaseModel):
    """Response for POST /prompts/{id}/render."""

    rendered: str = ""
    missing_variables: list[str] = Field(default_factory=list)


# ===========================================================================
# Router + endpoints (only defined when FastAPI is available)
# ===========================================================================

if _FASTAPI_AVAILABLE:
    router = APIRouter(prefix="/api/v1", tags=["advanced"])

    # ===================================================================
    # 1. Knowledge Graph
    # ===================================================================

    def _get_knowledge_graph(request: Request) -> Any:  # type: ignore[name-defined]
        """Lazily build (and cache) a :class:`KnowledgeGraph` for this tenant."""
        config = _get_config(request)
        task_store = _get_task_store(request)
        if config is None:
            return None
        try:
            from doctoragent.model.knowledge_graph import KnowledgeGraph
        except ImportError:  # pragma: no cover
            logger.exception("KnowledgeGraph module unavailable")
            return None
        db_path = config.paths.index / "knowledge_graph.db"
        tenant = _tenant_id(request)

        def _factory() -> Any:
            graph = KnowledgeGraph(db_path, tenant_id=tenant)
            if task_store is not None:
                graph.attach_task_store(task_store)
            return graph

        return _get_or_create(request, f"knowledge_graph:{tenant}", _factory)

    @router.post(
        "/kg/build",
        response_model=KGBuildResponse,
        summary="Build the knowledge graph from vault documents",
        description=(
            "Extracts entities and relations from every indexed document chunk "
            "and persists them to the knowledge graph. Requires an LLM provider "
            "for extraction."
        ),
        responses=_error_responses(401, 403, 500, 503),
    )
    async def kg_build(
        body: KGBuildRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Build the knowledge graph from documents.

        The optional ``limit`` caps the number of chunks processed when the
        underlying builder supports it.
        """
        graph = _get_knowledge_graph(request)
        if graph is None:
            raise _service_unavailable("Knowledge Graph")
        task_store = _get_task_store(request)
        llm_provider = _get_llm_provider(request)
        if task_store is None:
            raise _service_unavailable("Task Store")
        try:
            stats = await asyncio.to_thread(graph.build_from_documents, task_store, llm_provider)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Knowledge graph build failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Knowledge graph build failed: {exc}"
            ) from exc
        if not isinstance(stats, dict):
            stats = {}
        stats.setdefault("message", "Knowledge graph build complete")
        if body.limit is not None:
            stats["requested_limit"] = body.limit
        return stats

    @router.post(
        "/kg/query",
        response_model=KGQueryResponse,
        summary="Query the knowledge graph",
        description="Graph-based retrieval returning source chunks ranked by entity proximity.",
        responses=_error_responses(401, 422, 500, 503),
    )
    async def kg_query(
        body: KGQueryRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Query the knowledge graph for documents related to the query entities."""
        graph = _get_knowledge_graph(request)
        if graph is None:
            raise _service_unavailable("Knowledge Graph")
        llm_provider = _get_llm_provider(request)
        try:
            results = await asyncio.to_thread(graph.retrieve, body.query, llm_provider, body.top_k)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Knowledge graph query failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Knowledge graph query failed: {exc}"
            ) from exc
        return {"query": body.query, "results": _serialize(results)}

    @router.get(
        "/kg/entity/{name}",
        response_model=KGEntityResponse,
        summary="Get entity details",
        description="Returns the stored entity record (type, properties, source documents).",
        responses=_error_responses(401, 404, 503),
    )
    async def kg_entity(
        name: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Get details for a single knowledge-graph entity by name."""
        graph = _get_knowledge_graph(request)
        if graph is None:
            raise _service_unavailable("Knowledge Graph")
        entity = graph.get_entity(name)
        if entity is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Entity '{name}' not found"
            )
        return entity.to_dict()

    @router.post(
        "/kg/subgraph",
        response_model=KGSubgraphResponse,
        summary="Get a subgraph around an entity",
        description="Breadth-first traversal returning entities and relations within ``depth`` hops.",
        responses=_error_responses(401, 422, 500, 503),
    )
    async def kg_subgraph(
        body: KGSubgraphRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the subgraph surrounding a seed entity."""
        graph = _get_knowledge_graph(request)
        if graph is None:
            raise _service_unavailable("Knowledge Graph")
        try:
            sub = graph.get_subgraph(body.entity_name, body.depth)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Knowledge graph subgraph failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Subgraph retrieval failed: {exc}"
            ) from exc
        return {
            "seed": sub.get("seed", body.entity_name),
            "entities": [_serialize(e) for e in sub.get("entities", [])],
            "relations": [_serialize(r) for r in sub.get("relations", [])],
        }

    # ===================================================================
    # 2. Advanced RAG
    # ===================================================================

    @router.post(
        "/rag/route",
        response_model=RAGRouteResponse,
        summary="Classify a query and get routing information",
        description="Runs the adaptive query router to pick a retrieval strategy and tuning.",
        responses=_error_responses(401, 422, 500, 503),
    )
    async def rag_route(
        body: RAGRouteRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Classify the query and return the recommended retrieval configuration."""
        llm_provider = _get_llm_provider(request)
        try:
            from doctoragent.model.query_router import QueryRouter
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Query Router") from None
        router_ = QueryRouter(llm_provider)
        try:
            qtype = await asyncio.to_thread(router_.classify_query, body.query, llm_provider)
            strategy = router_.route(qtype)
            config = router_.get_retrieval_config(qtype)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Query routing failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Query routing failed: {exc}"
            ) from exc
        return {
            "query": body.query,
            "query_type": str(qtype),
            "strategy": str(strategy),
            "retrieval_config": config.to_dict(),
        }

    @router.post(
        "/rag/agentic",
        response_model=AgenticRAGResponse,
        summary="Run agentic RAG",
        description="LLM-controlled retrieval loop that decides each retrieval action.",
        responses=_error_responses(400, 401, 422, 500, 503),
    )
    async def rag_agentic(
        body: AgenticRAGRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Run the agentic RAG controller loop."""
        llm_provider = _get_llm_provider(request)
        if llm_provider is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No LLM provider available. Configure a connection first.",
            )
        pool = _get_pipeline_pool(request)
        task_store = _get_task_store(request)
        tenant = _tenant_id(request)
        try:
            from doctoragent.model.agentic_rag import AgenticRAG
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Agentic RAG") from None

        async def _retrieve(query: str, top_k: int) -> list[dict[str, Any]]:
            if pool is not None:
                rag = pool.get_pipeline(tenant)
                docs = rag.retrieve(query, top_k=top_k) if hasattr(rag, "retrieve") else []
            elif task_store is not None and hasattr(task_store, "search"):
                docs = await asyncio.to_thread(task_store.search, query, top_k)
            else:
                docs = []
            return [_serialize(d) for d in docs]

        controller = AgenticRAG(_retrieve, llm_provider, max_iterations=body.max_iterations)
        try:
            state = await controller.run(body.query)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agentic RAG failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Agentic RAG failed: {exc}"
            ) from exc
        return {
            "query": body.query,
            "answer": getattr(state, "current_answer", "") or "",
            "iterations": getattr(state, "iteration", 0),
            "documents": _serialize(getattr(state, "retrieved_docs", [])),
            "action_history": _serialize(getattr(state, "action_history", [])),
        }

    @router.post(
        "/rag/correct",
        response_model=CorrectiveRAGResponse,
        summary="Run corrective RAG (CRAG)",
        description="Grades retrieved documents and rewrites/re-retrieves when they are insufficient.",
        responses=_error_responses(400, 401, 422, 500, 503),
    )
    async def rag_correct(
        body: CorrectiveRAGRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Run the corrective RAG self-correction loop."""
        llm_provider = _get_llm_provider(request)
        if llm_provider is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No LLM provider available. Configure a connection first.",
            )
        pool = _get_pipeline_pool(request)
        task_store = _get_task_store(request)
        tenant = _tenant_id(request)
        try:
            from doctoragent.model.corrective_rag import CorrectiveRAG
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Corrective RAG") from None

        def _retrieve(query: str) -> list[Any]:
            if pool is not None:
                rag = pool.get_pipeline(tenant)
                if hasattr(rag, "retrieve"):
                    return rag.retrieve(query, top_k=body.top_k)
            if task_store is not None and hasattr(task_store, "search"):
                return task_store.search(query, body.top_k)
            return []

        crag = CorrectiveRAG(llm_provider)
        try:
            result = await asyncio.to_thread(
                crag.run_correction_loop,
                body.query,
                _retrieve,
                llm_provider,
                body.max_iterations,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Corrective RAG failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Corrective RAG failed: {exc}"
            ) from exc
        if not isinstance(result, dict):
            result = {}
        return _serialize(result)

    @router.get(
        "/rag/cache/stats",
        response_model=RAGCacheStatsResponse,
        summary="Get RAG cache statistics",
        description="Returns hit rate, size and eviction counters for the query-result cache.",
        responses=_error_responses(401, 503),
    )
    async def rag_cache_stats(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return RAG query-result cache statistics."""
        try:
            from doctoragent.model.cache import QueryResultCache
        except ImportError:  # pragma: no cover
            raise _service_unavailable("RAG Cache") from None
        cache: Any = _get_or_create(request, "rag_cache", lambda: QueryResultCache())
        if cache is None:
            raise _service_unavailable("RAG Cache")
        try:
            return cache.stats()
        except Exception as exc:  # noqa: BLE001
            logger.exception("RAG cache stats failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Cache stats failed: {exc}"
            ) from exc

    @router.delete(
        "/rag/cache",
        response_model=MessageResponse,
        summary="Clear the RAG cache",
        description="Invalidates all cached query results.",
        responses=_error_responses(401, 403, 500, 503),
    )
    async def rag_cache_clear(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, str]:
        """Clear all entries from the RAG query-result cache."""
        try:
            from doctoragent.model.cache import QueryResultCache
        except ImportError:  # pragma: no cover
            raise _service_unavailable("RAG Cache") from None
        cache: Any = _get_or_create(request, "rag_cache", lambda: QueryResultCache())
        if cache is None:
            raise _service_unavailable("RAG Cache")
        try:
            cache.invalidate_all()
        except Exception as exc:  # noqa: BLE001
            logger.exception("RAG cache clear failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Cache clear failed: {exc}"
            ) from exc
        return {"message": "RAG cache cleared"}

    # ===================================================================
    # 3. Security Analytics
    # ===================================================================

    def _get_security_analytics(request: Request) -> Any:  # type: ignore[name-defined]
        audit = _get_audit_logger(request)
        from doctoragent.security.analytics import SecurityAnalyticsEngine

        return _get_or_create(
            request, "security_analytics", lambda: SecurityAnalyticsEngine(audit_logger=audit)
        )

    @router.get(
        "/security/posture",
        response_model=SecurityPostureResponse,
        summary="Get security posture metrics",
        description="Aggregate dashboard metrics: event volume, anomaly count, risk overview.",
        responses=_error_responses(401, 503),
    )
    async def security_posture(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the current security posture."""
        engine = _get_security_analytics(request)
        if engine is None:
            raise _service_unavailable("Security Analytics")
        try:
            return engine.get_security_posture()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Security posture failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Security posture failed: {exc}"
            ) from exc

    @router.get(
        "/security/anomalies",
        response_model=AnomaliesResponse,
        summary="Get top anomalies",
        description="Returns the highest-risk anomalies, descending by risk score.",
        responses=_error_responses(401, 503),
    )
    async def security_anomalies(
        request: Request,  # type: ignore[name-defined]
        limit: int = Query(10, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the top anomalies ranked by risk score."""
        engine = _get_security_analytics(request)
        if engine is None:
            raise _service_unavailable("Security Analytics")
        try:
            anomalies = engine.get_top_anomalies(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Anomaly query failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Anomaly query failed: {exc}"
            ) from exc
        items = [_serialize(a) for a in anomalies]
        return {"total": len(items), "anomalies": items}

    @router.get(
        "/security/risk/{subject_id}",
        response_model=RiskScoreResponse,
        summary="Get risk score for a subject",
        description="Returns the real-time risk score in [0, 1] for the given subject.",
        responses=_error_responses(401, 503),
    )
    async def security_risk(
        subject_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the current risk score for a subject."""
        engine = _get_security_analytics(request)
        if engine is None:
            raise _service_unavailable("Security Analytics")
        try:
            score = engine.calculate_risk_score(subject_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Risk score failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Risk score failed: {exc}"
            ) from exc
        return {"subject_id": subject_id, "risk_score": score}

    @router.get(
        "/security/risk-trend",
        response_model=RiskTrendResponse,
        summary="Get risk trend over time",
        description="Day-by-day average risk and event counts for the last N days.",
        responses=_error_responses(401, 503),
    )
    async def security_risk_trend(
        request: Request,  # type: ignore[name-defined]
        days: int = Query(7, ge=1, le=365),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the risk trend over the last ``days`` days."""
        engine = _get_security_analytics(request)
        if engine is None:
            raise _service_unavailable("Security Analytics")
        try:
            trend = engine.get_risk_trend(days=days)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Risk trend failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Risk trend failed: {exc}"
            ) from exc
        return {"days": days, "trend": _serialize(trend)}

    # ===================================================================
    # 4. Shamir Secret Sharing
    # ===================================================================

    def _get_shamir(request: Request) -> Any:  # type: ignore[name-defined]
        from doctoragent.security.shamir import ShamirSecretSharing

        return _get_or_create(request, "shamir", lambda: ShamirSecretSharing())

    @router.post(
        "/shamir/split",
        response_model=ShamirSplitResponse,
        summary="Split a secret into shares",
        description="Splits a hex-encoded secret into n shares using a k-of-n threshold scheme.",
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def shamir_split(
        body: ShamirSplitRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Split a secret into threshold shares."""
        if body.threshold > body.total:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="threshold must be <= total"
            )
        shamir = _get_shamir(request)
        try:
            shares = shamir.split_hex(body.secret_hex, body.threshold, body.total)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=str(exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Shamir split failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Shamir split failed: {exc}"
            ) from exc
        return {"shares": shares, "threshold": body.threshold, "total": body.total}

    @router.post(
        "/shamir/reconstruct",
        response_model=ShamirReconstructResponse,
        summary="Reconstruct a secret from shares",
        description="Reconstructs the original hex secret from a sufficient set of shares.",
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def shamir_reconstruct(
        body: ShamirReconstructRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Reconstruct a secret from its shares."""
        shamir = _get_shamir(request)
        try:
            secret_hex = shamir.reconstruct_hex(body.shares)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=str(exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Shamir reconstruct failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Shamir reconstruct failed: {exc}"
            ) from exc
        return {"secret_hex": secret_hex}

    # ===================================================================
    # 5. Zero-Trust
    # ===================================================================

    def _get_zero_trust(request: Request) -> Any:  # type: ignore[name-defined]
        audit = _get_audit_logger(request)
        from doctoragent.security.zero_trust import ZeroTrustEngine

        return _get_or_create(
            request, "zero_trust_engine", lambda: ZeroTrustEngine(audit_logger=audit)
        )

    @router.post(
        "/zt/evaluate",
        response_model=ZTEvaluateResponse,
        summary="Evaluate an access request",
        description="Combines device posture, trust score and policy into an access decision.",
        responses=_error_responses(401, 403, 422, 500, 503),
    )
    async def zt_evaluate(
        body: ZTEvaluateRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Evaluate a zero-trust access request."""
        engine = _get_zero_trust(request)
        if engine is None:
            raise _service_unavailable("Zero-Trust Engine")
        try:
            from doctoragent.security.zero_trust import AccessRequest
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Zero-Trust Engine") from None
        req = AccessRequest(
            subject_id=body.subject_id,
            resource_path=body.resource_path,
            action=body.action,
            device_id=body.device_id,
            ip_address=body.ip_address,
            context=dict(body.context),
        )
        try:
            decision = engine.evaluate_access(req)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Zero-trust evaluation failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Zero-trust evaluation failed: {exc}"
            ) from exc
        expires = getattr(decision, "expires_at", None)
        ts = getattr(decision, "timestamp", None)
        return {
            "allowed": bool(getattr(decision, "allowed", False)),
            "trust_level": str(getattr(decision, "trust_level", "none")),
            "reason": getattr(decision, "reason", ""),
            "conditions": list(getattr(decision, "conditions", []) or []),
            "expires_at": expires.isoformat() if expires is not None else None,
            "timestamp": ts.isoformat() if ts is not None else None,
        }

    @router.post(
        "/zt/device",
        response_model=ZTDeviceResponse,
        summary="Register device posture",
        description="Registers or updates a device's security posture record.",
        responses=_error_responses(401, 403, 422, 500, 503),
    )
    async def zt_device(
        body: ZTDeviceRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Register or update a device posture record."""
        engine = _get_zero_trust(request)
        if engine is None:
            raise _service_unavailable("Zero-Trust Engine")
        try:
            from doctoragent.security.zero_trust import DevicePosture
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Zero-Trust Engine") from None
        posture = DevicePosture(
            device_id=body.device_id,
            os_version=body.os_version,
            disk_encrypted=body.disk_encrypted,
            firewall_enabled=body.firewall_enabled,
            trust_score=body.trust_score,
        )
        try:
            engine.register_device(body.device_id, posture)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Device registration failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Device registration failed: {exc}"
            ) from exc
        return {"device_id": body.device_id, "message": "Device posture registered"}

    @router.get(
        "/zt/history",
        response_model=ZTHistoryResponse,
        summary="Get access history",
        description="Returns recent access decisions, optionally filtered by subject.",
        responses=_error_responses(401, 503),
    )
    async def zt_history(
        request: Request,  # type: ignore[name-defined]
        subject_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(100, ge=1, le=1000),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return recent zero-trust access decisions."""
        engine = _get_zero_trust(request)
        if engine is None:
            raise _service_unavailable("Zero-Trust Engine")
        try:
            decisions = engine.get_access_history(subject_id=subject_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Access history failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Access history failed: {exc}"
            ) from exc
        records = [_serialize(d) for d in decisions]
        return {"total": len(records), "history": records}

    # ===================================================================
    # 6. DLP
    # ===================================================================

    def _get_dlp(request: Request) -> Any:  # type: ignore[name-defined]
        from doctoragent.security.dlp import DataLossPrevention

        return _get_or_create(request, "dlp_scanner", lambda: DataLossPrevention())

    @router.post(
        "/dlp/scan",
        response_model=DLPScanResponse,
        summary="Scan text for sensitive data",
        description="Detects PII/PCI/PHI and other sensitive patterns in the supplied text.",
        responses=_error_responses(401, 403, 422, 500, 503),
    )
    async def dlp_scan(
        body: DLPScanRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Scan text for sensitive data without modifying it."""
        scanner = _get_dlp(request)
        if scanner is None:
            raise _service_unavailable("DLP Scanner")
        try:
            matches = scanner.scan(body.text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("DLP scan failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"DLP scan failed: {exc}"
            ) from exc
        items = [_serialize(m) for m in matches]
        return {"count": len(items), "matches": items}

    @router.post(
        "/dlp/redact",
        response_model=DLPRedactResponse,
        summary="Scan and redact text",
        description="Detects sensitive data and returns a redacted copy with matches reported.",
        responses=_error_responses(401, 403, 422, 500, 503),
    )
    async def dlp_redact(
        body: DLPScanRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Scan text and return a redacted version."""
        scanner = _get_dlp(request)
        if scanner is None:
            raise _service_unavailable("DLP Scanner")
        try:
            redacted_text, matches = scanner.scan_and_redact(body.text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("DLP redact failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"DLP redact failed: {exc}"
            ) from exc
        items = [_serialize(m) for m in matches]
        return {"redacted_text": redacted_text, "count": len(items), "matches": items}

    # ===================================================================
    # 7. Agent Management
    # ===================================================================

    @router.get(
        "/agent/trajectory/{task_id}",
        response_model=AgentTrajectoryResponse,
        summary="Get agent execution trajectory",
        description="Returns the recorded step-by-step trajectory for a completed agent run.",
        responses=_error_responses(401, 404, 503),
    )
    async def agent_trajectory(
        task_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Retrieve the trajectory stored for an agent task."""
        trajectories = _get_state_dict(request, "agent_trajectories")
        trajectory = trajectories.get(task_id)
        if trajectory is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404,
                detail=f"No trajectory stored for task '{task_id}'",
            )
        steps = getattr(trajectory, "steps", None)
        if steps is None and isinstance(trajectory, dict):
            steps = trajectory.get("steps", [])
        return {
            "task_id": task_id,
            "found": True,
            "steps": [
                {
                    "step_type": (
                        s.step_type.value
                        if hasattr(s, "step_type") and hasattr(s.step_type, "value")
                        else getattr(s, "step_type", "")
                        if not isinstance(s, dict)
                        else s.get("step_type", "")
                    ),
                    "content": getattr(s, "content", "")
                    if not isinstance(s, dict)
                    else s.get("content", ""),
                    "tool_name": getattr(s, "tool_name", None)
                    if not isinstance(s, dict)
                    else s.get("tool_name"),
                }
                for s in (steps or [])
            ],
            "total_tool_calls": getattr(trajectory, "total_tool_calls", 0)
            if not isinstance(trajectory, dict)
            else trajectory.get("total_tool_calls", 0),
            "total_time_ms": getattr(trajectory, "total_time_ms", 0.0)
            if not isinstance(trajectory, dict)
            else trajectory.get("total_time_ms", 0.0),
        }

    @router.get(
        "/agent/skills",
        response_model=AgentSkillsResponse,
        summary="List available skills",
        description="Returns the registered agent skills and their definitions.",
        responses=_error_responses(401, 503),
    )
    async def agent_skills(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """List all registered agent skills."""
        try:
            from doctoragent.model.skills import create_default_skill_registry
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Skill Registry") from None
        registry = _get_or_create(
            request, "skill_registry", lambda: create_default_skill_registry()
        )
        if registry is None:
            raise _service_unavailable("Skill Registry")
        try:
            skills = registry.list_skills()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Skill listing failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Skill listing failed: {exc}"
            ) from exc
        items = [s.model_dump() if hasattr(s, "model_dump") else _serialize(s) for s in skills]
        return {"total": len(items), "skills": items}

    @router.post(
        "/agent/evolve",
        response_model=AgentEvolveResponse,
        summary="Trigger agent self-evolution",
        description="Analyse stored trajectories to extract lessons and store reusable experiences.",
        responses=_error_responses(400, 401, 403, 500, 503),
    )
    async def agent_evolve(
        body: AgentEvolveRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Trigger self-evolution from stored trajectories."""
        task_store = _get_task_store(request)
        llm_provider = _get_llm_provider(request)
        if task_store is None or llm_provider is None:
            raise _service_unavailable("Self-Evolution Engine")
        try:
            from doctoragent.model.self_evolution import SelfEvolutionEngine
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Self-Evolution Engine") from None
        engine = _get_or_create(
            request,
            "self_evolution_engine",
            lambda: SelfEvolutionEngine(task_store, llm_provider),
        )
        if engine is None:
            raise _service_unavailable("Self-Evolution Engine")

        trajectories: list[Any] = []
        registry = _get_state_dict(request, "agent_trajectories")
        if body.task_ids:
            trajectories = [registry[tid] for tid in body.task_ids if tid in registry]
        else:
            trajectories = list(registry.values())

        if not trajectories:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No trajectories available to evolve from",
            )
        try:
            summary = await asyncio.to_thread(engine.evolve, trajectories)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent evolution failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Agent evolution failed: {exc}"
            ) from exc
        if not isinstance(summary, dict):
            summary = {"message": str(summary)}
        summary.setdefault("message", "Evolution complete")
        return _serialize(summary)

    @router.get(
        "/agent/tot",
        response_model=AgentToTResponse,
        summary="Run Tree-of-Thought reasoning",
        description="Explores multiple reasoning branches and returns the best thought path.",
        responses=_error_responses(400, 401, 422, 500, 503),
    )
    async def agent_tot(
        request: Request,  # type: ignore[name-defined]
        query: str = Query(..., min_length=1, max_length=2000),  # type: ignore[name-defined]  # noqa: B008
        context: str | None = Query(None, max_length=4000),  # type: ignore[name-defined]  # noqa: B008
        max_depth: int = Query(3, ge=1, le=10),  # type: ignore[name-defined]  # noqa: B008
        branching_factor: int = Query(3, ge=1, le=10),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Run Tree-of-Thought reasoning over a query."""
        llm_provider = _get_llm_provider(request)
        if llm_provider is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No LLM provider available. Configure a connection first.",
            )
        try:
            from doctoragent.model.tree_of_thought import TreeOfThoughts
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Tree-of-Thoughts") from None
        tot = TreeOfThoughts(
            llm_provider,
            max_depth=max_depth,
            branching_factor=branching_factor,
        )
        try:
            await asyncio.to_thread(tot.search_sync, query, context)
            best_path = tot.select_best_path()
            tree = tot.tree
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tree-of-Thought failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Tree-of-Thought failed: {exc}"
            ) from exc
        path_nodes = [n.to_dict() for n in best_path]
        answer = path_nodes[-1].get("content", "") if path_nodes else ""
        return {
            "query": query,
            "answer": answer,
            "best_path": path_nodes,
            "tree": tree.to_dict() if tree is not None else None,
        }

    # ===================================================================
    # 8. Key Management
    # ===================================================================

    @router.post(
        "/keys/rotate",
        response_model=KeyRotateResponse,
        summary="Trigger key rotation",
        description="Triggers an emergency master-key rotation via the auto-rotator.",
        responses=_error_responses(401, 403, 500, 503),
    )
    async def keys_rotate(
        body: KeyRotateRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Trigger a master-key rotation."""
        agent = _get_agent(request)
        rotator = getattr(agent, "key_rotator", None) if agent is not None else None
        if rotator is None:
            raise _service_unavailable("Key Rotator")
        try:
            await asyncio.to_thread(rotator.trigger_emergency_rotation, body.reason)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Key rotation failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Key rotation failed: {exc}"
            ) from exc
        return {
            "rotated": True,
            "reason": body.reason,
            "message": "Key rotation triggered successfully",
        }

    @router.get(
        "/keys/status",
        response_model=KeyStatusResponse,
        summary="Get key status",
        description="Returns the master-key provider type, existence and rotator status.",
        responses=_error_responses(401, 503),
    )
    async def keys_status(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return master-key and rotator status."""
        provider = _get_master_key_provider(request)
        agent = _get_agent(request)
        if provider is None:
            raise _service_unavailable("Master Key Provider")
        rotator = getattr(agent, "key_rotator", None) if agent is not None else None
        provider_type = type(provider).__name__
        exists = False
        try:
            exists = bool(provider.exists())
        except Exception:  # noqa: BLE001
            exists = False
        running = False
        last_rotation = None
        grace_keys = 0
        if rotator is not None:
            try:
                running = bool(rotator.is_running())
            except Exception:  # noqa: BLE001
                running = False
            try:
                lr = rotator.last_rotation()
                last_rotation = lr.isoformat() if lr is not None else None
            except Exception:  # noqa: BLE001
                last_rotation = None
            try:
                grace_keys = len(rotator.grace_registry().get_grace_keys(b""))
            except Exception:  # noqa: BLE001
                grace_keys = 0
        return {
            "provider_type": provider_type,
            "exists": exists,
            "auto_rotator_running": running,
            "last_rotation": last_rotation,
            "grace_keys": grace_keys,
        }

    # ===================================================================
    # 9. DAG Workflow
    # ===================================================================

    @router.post(
        "/dag/execute",
        response_model=DAGExecuteResponse,
        summary="Execute a DAG workflow",
        description="Validates and executes a directed acyclic graph of tasks in dependency waves.",
        responses=_error_responses(400, 401, 403, 422, 500, 503),
    )
    async def dag_execute(
        body: DAGExecuteRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Execute a DAG workflow defined by the submitted task specs."""
        try:
            from doctoragent.orchestration.dag_engine import DAGEngine, DAGTask
        except ImportError:  # pragma: no cover
            raise _service_unavailable("DAG Engine") from None

        async def _default_callable(task: Any) -> Any:
            """Default no-op executor used when no server-side callable is registered."""
            return {"task_id": task.id, "params": dict(getattr(task, "params", {}) or {})}

        dag_id = uuid4().hex
        engine = DAGEngine()
        try:
            for spec in body.tasks:
                task = DAGTask(
                    id=spec.id,
                    name=spec.name or spec.id,
                    callable=_default_callable,
                    dependencies=list(spec.dependencies),
                    max_retries=spec.max_retries,
                    timeout_seconds=spec.timeout_seconds,
                    params=dict(spec.params),
                )
                engine.add_task(task)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"Invalid DAG definition: {exc}"
            ) from exc

        engines = _get_state_dict(request, "dag_engines")
        engines[dag_id] = engine
        try:
            status = await engine.execute()
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=422, detail=f"DAG validation failed: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("DAG execution failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"DAG execution failed: {exc}"
            ) from exc
        return {"dag_id": dag_id, "status": _serialize(status)}

    @router.get(
        "/dag/status/{dag_id}",
        response_model=DAGStatusResponse,
        summary="Get DAG execution status",
        description="Returns the execution status of a previously submitted DAG.",
        responses=_error_responses(401, 404, 503),
    )
    async def dag_status(
        dag_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the status of a DAG execution by ID."""
        engines = _get_state_dict(request, "dag_engines")
        engine = engines.get(dag_id)
        if engine is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"DAG '{dag_id}' not found"
            )
        try:
            status = engine.get_status()
        except Exception as exc:  # noqa: BLE001
            logger.exception("DAG status failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"DAG status failed: {exc}"
            ) from exc
        return {"dag_id": dag_id, "found": True, "status": _serialize(status)}

    # ===================================================================
    # 10. Task Scheduler
    # ===================================================================

    def _get_scheduler(request: Request) -> Any:  # type: ignore[name-defined]
        try:
            from doctoragent.orchestration.scheduler import TaskScheduler
        except ImportError:  # pragma: no cover
            return None
        return _get_or_create(request, "task_scheduler", lambda: TaskScheduler())

    @router.get(
        "/scheduler/status",
        response_model=SchedulerStatusResponse,
        summary="Get task scheduler status",
        description="Returns queue depth, running counts and aggregate scheduler metrics.",
        responses=_error_responses(401, 503),
    )
    async def scheduler_status(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return task-scheduler queue and metrics status."""
        scheduler = _get_scheduler(request)
        if scheduler is None:
            raise _service_unavailable("Task Scheduler")
        try:
            queue = scheduler.get_queue_status()
            metrics = scheduler.get_metrics()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduler status failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Scheduler status failed: {exc}"
            ) from exc
        return {"queue": _serialize(queue), "metrics": _serialize(metrics)}

    # ===================================================================
    # 11. Compliance (DSAR)
    # ===================================================================

    def _get_compliance_manager(request: Request) -> Any:  # type: ignore[name-defined]
        task_store = _get_task_store(request)
        config = _get_config(request)
        if task_store is None:
            return None
        try:
            from doctoragent.security.compliance import ComplianceManager
        except ImportError:  # pragma: no cover
            return None
        audit = _get_audit_logger(request)
        vault_dir = (
            getattr(getattr(config, "paths", None), "vault", None) if config is not None else None
        )

        def _factory() -> Any:
            return ComplianceManager(
                task_store,
                audit_logger=audit,
                vault_dir=vault_dir,
            )

        return _get_or_create(request, "compliance_manager", _factory)

    @router.post(
        "/compliance/export",
        response_model=ComplianceExportResponse,
        summary="Export subject data (DSAR access)",
        description="Collects all data associated with a subject for a data-subject access request.",
        responses=_error_responses(401, 403, 500, 503),
    )
    async def compliance_export(
        body: ComplianceExportRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Export all data belonging to a subject (DSAR access)."""
        manager = _get_compliance_manager(request)
        if manager is None:
            raise _service_unavailable("Compliance Manager")
        try:
            export = await asyncio.to_thread(manager.export_subject_data, body.subject_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("DSAR export failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"DSAR export failed: {exc}"
            ) from exc
        if not isinstance(export, dict):
            export = {}
        return _serialize(export)

    @router.post(
        "/compliance/erase",
        response_model=ComplianceEraseResponse,
        summary="Erase subject data (DSAR erasure)",
        description="Deletes all data associated with a subject for a data-subject erasure request.",
        responses=_error_responses(401, 403, 500, 503),
    )
    async def compliance_erase(
        body: ComplianceEraseRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Erase all data belonging to a subject (DSAR erasure)."""
        manager = _get_compliance_manager(request)
        if manager is None:
            raise _service_unavailable("Compliance Manager")
        try:
            erased = await asyncio.to_thread(
                manager.erase_subject_data, body.subject_id, body.delete_files
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("DSAR erase failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"DSAR erase failed: {exc}"
            ) from exc
        erased_count = erased if isinstance(erased, int) else 0
        return {
            "subject_id": body.subject_id,
            "erased_count": erased_count,
            "message": f"Erased {erased_count} record(s) for subject '{body.subject_id}'",
        }

    @router.get(
        "/compliance/consents",
        response_model=ConsentsResponse,
        summary="Get consent records",
        description="Returns consent records, optionally filtered by subject.",
        responses=_error_responses(401, 503),
    )
    async def compliance_consents(
        request: Request,  # type: ignore[name-defined]
        subject_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return consent records, optionally filtered by subject."""
        manager = _get_compliance_manager(request)
        if manager is None:
            raise _service_unavailable("Compliance Manager")
        try:
            consents = manager.list_consents(subject_id=subject_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Consent query failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Consent query failed: {exc}"
            ) from exc
        records = [_serialize(c) for c in consents]
        return {"total": len(records), "consents": records}

    # ===================================================================
    # 14. Clinical differentiation surfaces — PHI de-identification,
    #     deterministic rule inspection, citation verification, agent
    #     graph topology, RAG evaluation, SMART-on-FHIR launch.
    #
    # These expose capabilities that were already implemented but only
    # reachable implicitly (inside guardrails / inside /clinical/analyze).
    # Surfacing them as first-class endpoints lets the console / auditors
    # / competition judges demo each differentiator in isolation.
    # ===================================================================

    class DeidentifyRequest(BaseModel):
        """Request body for ``POST /deidentify``."""

        model_config = ConfigDict(extra="forbid")
        text: str = Field(..., description="Free text to de-identify (PHI removal).")
        strategy: str = Field(
            "redact",
            description="redact | pseudonymize | mask",
        )

    @router.post(  # type: ignore[name-defined]
        "/deidentify",
        tags=["Clinical"],
        summary="De-identify free text (HIPAA Safe Harbor PHI removal)",
        description=(
            "Runs the PHI detector over the supplied text and applies the "
            "chosen strategy (redact / pseudonymize / mask). Returns the "
            "de-identified text plus the list of detected PHI entities "
            "(type, value, offsets) so an auditor can verify coverage. "
            "Covers 10 core clinical PHI categories."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def deidentify(
        req: DeidentifyRequest,
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """De-identify text and report every PHI entity found."""
        from doctoragent.clinical.deidentification import PHIDetector

        if req.strategy not in ("redact", "pseudonymize", "mask"):
            raise HTTPException(  # type: ignore[misc]
                status_code=422,
                detail="strategy must be one of: redact, pseudonymize, mask",
            )
        try:
            detector = PHIDetector()
            matches = detector.detect_phi(req.text)
            if req.strategy == "pseudonymize":
                deidentified, mapping = detector.pseudonymize(req.text)
            else:
                deidentified = detector.deidentify(req.text, strategy=req.strategy)
                mapping = {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("de-identification failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"De-identification failed: {exc}"
            ) from exc
        return {
            "original": req.text,
            "deidentified": deidentified,
            "strategy": req.strategy,
            "matches": matches,
            "match_count": len(matches),
            "types_found": sorted({m["type"] for m in matches}),
            "mapping": mapping,
        }

    @router.get(  # type: ignore[name-defined]
        "/safety/rules",
        tags=["Clinical"],
        summary="List deterministic clinical safety rules",
        description=(
            "Returns the catalogue of deterministic safety rules the "
            "ClinicalRuleEngine evaluates (vitals / labs / drug-drug "
            "interaction / allergy cross-reactivity / duplicate therapy), "
            "each with its severity levels and a human-readable description."
        ),
        responses=_error_responses(
            401,
        ),
    )
    async def safety_rules_list(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the static rule catalogue for console rendering."""
        from doctoragent.clinical.safety.rules import ClinicalRuleType

        rules = [
            {
                "rule_type": ClinicalRuleType.VITALS.value,
                "description": "生命体征危急值检测（心率/血压/呼吸/体温/血氧）",
                "severity_levels": ["critical", "warning", "info"],
            },
            {
                "rule_type": ClinicalRuleType.LABS.value,
                "description": "检验值异常判定（钾/钠/肌酐/血糖/血红蛋白等）",
                "severity_levels": ["critical", "warning", "info"],
            },
            {
                "rule_type": ClinicalRuleType.DRUG_INTERACTION.value,
                "description": "药物-药物相互作用检测（openFDA + RxNorm）",
                "severity_levels": ["contraindicated", "warning"],
            },
            {
                "rule_type": ClinicalRuleType.ALLERGY.value,
                "description": "过敏交叉反应检测（基于用药与过敏史）",
                "severity_levels": ["contraindicated", "warning"],
            },
            {
                "rule_type": ClinicalRuleType.DUPLICATE_THERAPY.value,
                "description": "重复用药检测（同类药物叠加）",
                "severity_levels": ["warning", "info"],
            },
        ]
        return {"rules": rules, "total": len(rules)}

    class SafetyRulesTestRequest(BaseModel):
        """Request body for ``POST /safety/rules/test``."""

        model_config = ConfigDict(extra="forbid")
        patient_context: dict[str, Any] = Field(
            ...,
            description=(
                "Patient data: vitals, labs, medications, allergies "
                "(same shape as /clinical/analyze)."
            ),
        )

    @router.post(  # type: ignore[name-defined]
        "/safety/rules/test",
        tags=["Clinical"],
        summary="Run the deterministic rule engine in isolation",
        description=(
            "Evaluates the patient_context against the deterministic safety "
            "rules only (no LLM). Returns every fired finding with severity, "
            "the affected resources, and a blocking flag (critical / "
            "contraindicated). Useful for demonstrating the safety floor "
            "independently of the LLM workflow."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def safety_rules_test(
        req: SafetyRulesTestRequest,
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Run the rule engine and return findings."""
        from doctoragent.clinical.safety import ClinicalRuleEngine

        try:
            engine = ClinicalRuleEngine()
            results = await engine.evaluate_all(req.patient_context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("rule engine test failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Rule engine failed: {exc}"
            ) from exc
        findings = [r.model_dump() for r in results]
        blocking = any(f.get("severity") in ("critical", "contraindicated") for f in findings)
        return {
            "findings": findings,
            "total": len(findings),
            "blocking": blocking,
        }

    class CitationVerifyRequest(BaseModel):
        """Request body for ``POST /citations/verify``."""

        model_config = ConfigDict(extra="forbid")
        text: str = Field(..., description="Clinical output text to verify.")
        required: bool = Field(
            True,
            description="When true, missing citations flag the output.",
        )

    @router.post(  # type: ignore[name-defined]
        "/citations/verify",
        tags=["Clinical"],
        summary="Verify a clinical output carries traceable citations",
        description=(
            "Runs the citation guardrail: checks the text for PMID / FHIR "
            "resource ID / guideline references. Returns whether a citation "
            "was found, the matched citation strings, and the guardrail "
            "action (allow / flag)."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def citations_verify(
        req: CitationVerifyRequest,
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Verify citations in clinical text."""
        from doctoragent.clinical.safety import ClinicalGuardrails
        from doctoragent.clinical.safety import guardrails as _guardrails_mod

        try:
            guardrails = ClinicalGuardrails()
            result = guardrails.check_citations(req.text, required=req.required)
        except Exception as exc:  # noqa: BLE001
            logger.exception("citation verification failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Citation verification failed: {exc}"
            ) from exc
        # Extract the matched citation strings for display.
        citations: list[str] = []
        for pattern in getattr(_guardrails_mod, "_CITATION_PATTERNS", []):
            for m in pattern.finditer(req.text):
                if m.group(0) not in citations:
                    citations.append(m.group(0))
        return {
            "has_citation": result.action == "allow",
            "action": result.action,
            "citations": citations,
            "warning": (result.warnings[0] if result.warnings else ""),
        }

    @router.get(  # type: ignore[name-defined]
        "/agents/graph",
        tags=["Agents"],
        summary="Return the clinical multi-agent graph topology",
        description=(
            "Returns the compiled multi-agent DAG (rules → parallel "
            "specialists → documentation → guardrail) as a JSON-serialisable "
            "structure so the console can render the agent topology. The "
            "engine field reports whether LangGraph or the hand-rolled "
            "fallback is active."
        ),
        responses=_error_responses(
            401,
        ),
    )
    async def agents_graph(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Return the clinical agent graph topology for console rendering."""
        try:
            from doctoragent.clinical.agents.graph import (
                get_clinical_graph_topology,
                langgraph_available,
            )

            topology = get_clinical_graph_topology()
            topology["langgraph_available"] = langgraph_available()
            return topology
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent graph topology failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Graph topology failed: {exc}"
            ) from exc

    class EvaluateRequest(BaseModel):
        """Request body for ``POST /evaluate``."""

        model_config = ConfigDict(extra="forbid")
        input: str = Field(..., description="The user query.")
        actual_output: str = Field(..., description="The generated answer.")
        expected_output: str | None = Field(None, description="Ground-truth answer (optional).")
        retrieval_context: list[str] = Field(
            default_factory=list,
            description="Retrieved context chunks.",
        )
        context: list[str] = Field(
            default_factory=list,
            description="Ground-truth context (optional).",
        )
        threshold: float = Field(0.5, description="Pass threshold (0-1).")
        judge_model: str = Field("gpt-4o-mini", description="LLM judge model.")
        api_key: str | None = Field(None, description="Judge API key.")
        base_url: str | None = Field(None, description="Judge base URL.")

    @router.post(  # type: ignore[name-defined]
        "/evaluate",
        tags=["Evaluation"],
        summary="Run the RAG evaluation suite (DeepEval / deterministic fallback)",
        description=(
            "Evaluates the supplied test case against Faithfulness, Answer "
            "Relevancy, Context Precision and Context Recall. Uses DeepEval "
            "when installed + a judge key is available; otherwise degrades to "
            "the deterministic fallback metrics so the endpoint is always "
            "callable. Returns per-metric scores + a summary."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def evaluate_rag(
        req: EvaluateRequest,
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Run the RAG evaluation suite and return the scorecard."""
        from doctoragent.model.deepeval_integration import RAGEvaluator
        from doctoragent.model.evaluation import LLMTestCase

        test_case = LLMTestCase(
            input=req.input,
            actual_output=req.actual_output,
            expected_output=req.expected_output,
            retrieval_context=req.retrieval_context,
            context=req.context,
        )
        evaluator = RAGEvaluator(
            threshold=req.threshold,
            judge_model=req.judge_model,
            api_key=req.api_key,
            base_url=req.base_url,
        )
        try:
            report = await evaluator.evaluate(test_case)
        except Exception as exc:  # noqa: BLE001
            logger.exception("evaluation failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Evaluation failed: {exc}"
            ) from exc
        return report.to_dict()

    # ===================================================================
    # 11. Memory Management
    # ===================================================================
    # The agent's :class:`~doctoragent.model.rag.MemorySystem` already
    # persists long-term facts, episodic turns and conversation sessions.
    # These endpoints expose that store for console inspection & editing,
    # and add an RL feedback channel that feeds back into self-evolution.

    def _get_memory_system(request: Request) -> Any:  # type: ignore[name-defined]
        """Resolve (or lazily construct) the agent's MemorySystem."""
        agent = _get_agent(request)
        ms = getattr(agent, "memory_system", None) if agent is not None else None
        if ms is not None:
            return ms
        # Lazily build a standalone MemorySystem off the task_store path
        # so the console still works when the agent was not wired with one.
        return _get_or_create(request, "memory_system", lambda: _build_memory_system(request))

    def _build_memory_system(request: Request) -> Any:  # type: ignore[name-defined]
        try:
            from doctoragent.model.rag import MemorySystem

            # Prefer the agent's embedding provider (for semantic recall).
            agent = _get_agent(request)
            embedding_provider = (
                getattr(agent, "_embedding_provider", None) if agent is not None else None
            )
            # Resolve db_path: prefer config.paths.index / tasks.db.
            db_path = None
            tenant_id = _tenant_id(request)
            config = _get_config(request)
            if config is not None:
                idx_path = getattr(getattr(config, "paths", None), "index", None)
                if idx_path is not None:
                    db_path = Path(str(idx_path)) / "tasks.db"
            if db_path is None:
                task_store = _get_task_store(request)
                if task_store is not None:
                    db_path = getattr(task_store, "db_path", None) or getattr(
                        task_store, "path", None
                    )
            if db_path is None:
                return None
            return MemorySystem(Path(str(db_path)), tenant_id, embedding_provider)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to build MemorySystem")
            return None

    @router.get(
        "/memory/facts",
        response_model=MemoryListResponse,
        tags=["Memory"],
        summary="List long-term facts",
        description="Returns stored semantic / episodic / procedural facts, optionally filtered by ``memory_type``.",
        responses=_error_responses(401, 503),
    )
    async def memory_facts_list(
        request: Request,  # type: ignore[name-defined]
        memory_type: str | None = Query(
            None, description="Filter by memory_type (semantic | episodic | procedural)"
        ),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(100, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        items: list[dict[str, Any]] = []
        try:
            with ms._connect() as conn:  # type: ignore[attr-defined]
                conn.row_factory = _row_to_dict
                if memory_type:
                    rows = conn.execute(
                        "SELECT memory_id, content, memory_type, importance, created_at, last_accessed, access_count, metadata FROM memory_long_term WHERE memory_type = ? AND tenant_id = ? ORDER BY importance DESC, created_at DESC LIMIT ?",
                        (memory_type, ms.tenant_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT memory_id, content, memory_type, importance, created_at, last_accessed, access_count, metadata FROM memory_long_term WHERE tenant_id = ? ORDER BY importance DESC, created_at DESC LIMIT ?",
                        (ms.tenant_id, limit),
                    ).fetchall()
                items = []
                for r in rows:
                    d = dict(r)
                    if d.get("metadata"):
                        try:
                            d["metadata"] = (
                                json.loads(d["metadata"])
                                if isinstance(d["metadata"], str)
                                else d["metadata"]
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    items.append(d)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory_facts_list failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to list facts: {exc}"
            ) from exc
        return {"total": len(items), "items": _serialize(items)}

    @router.post(
        "/memory/facts",
        response_model=MemoryListResponse,
        tags=["Memory"],
        summary="Store a long-term fact",
        description="Persist a new fact into the agent's long-term memory store.",
        responses=_error_responses(401, 403, 422, 503, 500),
    )
    async def memory_facts_store(
        body: MemoryStoreFactRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        try:
            entry_id = await asyncio.to_thread(
                ms.store_fact,
                body.content,
                body.memory_type,
                body.importance,
                body.metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory_facts_store failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to store fact: {exc}"
            ) from exc
        # store_fact returns the memory_id (str); synthesise an entry payload.
        item = {
            "memory_id": entry_id,
            "content": body.content,
            "memory_type": body.memory_type,
            "importance": body.importance,
            "metadata": body.metadata,
        }
        return {"total": 1, "items": [item]}

    @router.delete(
        "/memory/facts/{fact_id}",
        response_model=MessageResponse,
        tags=["Memory"],
        summary="Delete a long-term fact",
        responses=_error_responses(401, 403, 404, 503, 500),
    )
    async def memory_facts_delete(
        fact_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        try:
            with ms._connect() as conn:  # type: ignore[attr-defined]
                cur = conn.execute("DELETE FROM memory_long_term WHERE memory_id = ?", (fact_id,))
                deleted = cur.rowcount
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to delete fact: {exc}"
            ) from exc
        if not deleted:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Fact '{fact_id}' not found"
            )
        return {"message": f"Deleted fact {fact_id}"}

    @router.post(
        "/memory/recall",
        response_model=MemoryListResponse,
        tags=["Memory"],
        summary="Recall facts + episodes for a query",
        description="Hybrid recall of long-term facts and episodic memory relevant to *query*.",
        responses=_error_responses(401, 422, 503, 500),
    )
    async def memory_recall(
        body: MemoryRecallRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        try:
            facts = await asyncio.to_thread(ms.recall_facts, body.query, body.limit)
            episodes = await asyncio.to_thread(
                ms.recall_episodes, body.query, max(1, body.limit // 2)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory_recall failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Recall failed: {exc}"
            ) from exc
        items: list[dict[str, Any]] = []
        for f in facts:
            items.append(
                {"kind": "fact", **(f.model_dump() if hasattr(f, "model_dump") else _serialize(f))}
            )
        for e in episodes or []:
            items.append({"kind": "episode", **_serialize(e)})
        return {"total": len(items), "items": items}

    @router.get(
        "/memory/sessions",
        response_model=MemoryListResponse,
        tags=["Memory"],
        summary="List conversation sessions",
        responses=_error_responses(401, 503, 500),
    )
    async def memory_sessions_list(
        request: Request,  # type: ignore[name-defined]
        limit: int = Query(50, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        items: list[dict[str, Any]] = []
        try:
            with ms._connect() as conn:  # type: ignore[attr-defined]
                conn.row_factory = _row_to_dict
                rows = conn.execute(
                    "SELECT session_id, started_at, last_active, turn_count, summary FROM conversation_sessions WHERE tenant_id = ? ORDER BY last_active DESC LIMIT ?",
                    (ms.tenant_id, limit),
                ).fetchall()
                items = [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to list sessions: {exc}"
            ) from exc
        return {"total": len(items), "items": _serialize(items)}

    @router.get(
        "/memory/episodes",
        response_model=MemoryListResponse,
        tags=["Memory"],
        summary="List episodic memory entries",
        responses=_error_responses(401, 503, 500),
    )
    async def memory_episodes_list(
        request: Request,  # type: ignore[name-defined]
        session_id: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(100, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        items: list[dict[str, Any]] = []
        try:
            with ms._connect() as conn:  # type: ignore[attr-defined]
                conn.row_factory = _row_to_dict
                if session_id:
                    rows = conn.execute(
                        "SELECT memory_id, session_id, user_message, assistant_response, key_facts, timestamp FROM memory_episodic WHERE session_id = ? AND tenant_id = ? ORDER BY timestamp DESC LIMIT ?",
                        (session_id, ms.tenant_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT memory_id, session_id, user_message, assistant_response, key_facts, timestamp FROM memory_episodic WHERE tenant_id = ? ORDER BY timestamp DESC LIMIT ?",
                        (ms.tenant_id, limit),
                    ).fetchall()
                items = [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to list episodes: {exc}"
            ) from exc
        return {"total": len(items), "items": _serialize(items)}

    @router.post(
        "/memory/consolidate",
        tags=["Memory"],
        summary="Run long-horizon memory consolidation (episodic → semantic)",
        description=(
            "Triggers the episodic → semantic compaction pass: un-consolidated "
            "episodes are distilled into durable semantic facts (deduplicated "
            "against existing long-term memory) so knowledge survives the "
            "episode-level TTL / forgetting. Idempotent — already-consolidated "
            "episodes are skipped."
        ),
        responses=_error_responses(401, 503, 500),
    )
    async def memory_consolidate(
        request: Request,  # type: ignore[name-defined]
        batch_size: int = Body(100, ge=1, le=2000),  # type: ignore[name-defined]  # noqa: B008
        prune: bool = Body(True),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        ms = _get_memory_system(request)
        if ms is None:
            raise _service_unavailable("Memory System")
        if not hasattr(ms, "consolidate_memories"):
            raise HTTPException(  # type: ignore[misc]
                status_code=501,
                detail="This MemorySystem version does not support consolidation",
            )
        try:
            stats = ms.consolidate_memories(batch_size=batch_size, prune_after=prune)
            return {"status": "ok", **stats}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Memory consolidation failed: {exc}"
            ) from exc

    # ===================================================================
    # 12.5 Voice conversation chain (ASR + TTS)
    # ===================================================================

    def _get_voice_service(request: Request) -> Any:  # type: ignore[name-defined]
        """Resolve (or lazily construct) the configured VoiceService."""
        svc = getattr(request.app.state, "voice_service", None)
        if svc is not None:
            return svc
        try:
            from doctoragent.config import VoiceConfig
            from doctoragent.voice.service import VoiceService

            config = _get_config(request)
            voice_config = getattr(config, "voice", None) or VoiceConfig()
            svc = VoiceService(voice_config)
            setattr(request.app.state, "voice_service", svc)
            return svc
        except Exception:  # noqa: BLE001
            logger.exception("Failed to build VoiceService")
            return None

    @router.get(
        "/voice/status",
        tags=["Voice"],
        summary="Voice service capability status",
        responses=_error_responses(401),
    )
    async def voice_status(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        svc = _get_voice_service(request)
        if svc is None:
            return {"enabled": False, "transcribe": False, "tts": False}
        return svc.availability()

    @router.post(
        "/voice/transcribe",
        tags=["Voice"],
        summary="Transcribe an audio clip to text (ASR)",
        description=(
            "Accepts an audio file upload (webm/wav/mp3) and returns the "
            "transcribed text via the configured OpenAI-compatible speech-to-"
            "text endpoint. Returns 501 when transcription is not configured."
        ),
        responses=_error_responses(400, 401, 501, 500),
    )
    async def voice_transcribe(
        request: Request,  # type: ignore[name-defined]
        file: UploadFile = File(...),  # type: ignore[name-defined]  # noqa: B008
        language: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        svc = _get_voice_service(request)
        if svc is None or not svc.transcribe_available:
            raise HTTPException(  # type: ignore[misc]
                status_code=501, detail="Voice transcription is not configured"
            )
        data = await file.read()
        config = _get_config(request)
        max_bytes = (
            getattr(getattr(config, "voice", None), "max_audio_bytes", 10 * 1024 * 1024)
            if config is not None
            else 10 * 1024 * 1024
        )
        if len(data) > max_bytes:
            raise HTTPException(  # type: ignore[misc]
                status_code=413,
                detail=f"Audio exceeds {max_bytes} byte upload limit",
            )
        try:
            text = await svc.transcribe(
                data,
                filename=file.filename or "audio.webm",
                mime=file.content_type or "audio/webm",
                language=language,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Voice transcription failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Transcription failed: {exc}"
            ) from exc
        return {"text": text}

    @router.post(
        "/voice/synthesize",
        tags=["Voice"],
        summary="Synthesize speech audio from text (TTS)",
        description=(
            "Takes ``{text, voice?}`` and returns synthesized audio (mp3) via "
            "the configured OpenAI-compatible text-to-speech endpoint. Returns "
            "501 when synthesis is not configured."
        ),
        responses=_error_responses(400, 401, 501, 500),
    )
    async def voice_synthesize(
        request: Request,  # type: ignore[name-defined]
        payload: dict[str, Any] = Body(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> Response:
        svc = _get_voice_service(request)
        if svc is None or not svc.tts_available:
            raise HTTPException(  # type: ignore[misc]
                status_code=501, detail="Voice synthesis is not configured"
            )
        text = str(payload.get("text", "")).strip()
        if not text:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="'text' is required"
            )
        try:
            audio = await svc.synthesize(text, voice=payload.get("voice"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Voice synthesis failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Synthesis failed: {exc}"
            ) from exc
        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=speech.mp3"},
        )

    # ===================================================================
    # 12. Lifecycle Hooks Management
    # ===================================================================

    def _get_hook_manager(request: Request) -> Any:  # type: ignore[name-defined]
        """Resolve (or lazily create) a LifecycleHookManager on app.state."""
        return _get_or_create(
            request,
            "lifecycle_hook_manager",
            lambda: _build_hook_manager(request),
        )

    def _build_hook_manager(request: Request) -> Any:  # type: ignore[name-defined]
        try:
            from doctoragent.orchestration.lifecycle_hooks import LifecycleHookManager

            mgr = LifecycleHookManager()
            # Optionally wire in pre-existing hooks from the agent pipeline
            # (best-effort — the agent may not expose its hook manager).
            agent = _get_agent(request)
            existing = getattr(agent, "hook_manager", None) if agent is not None else None
            if existing is not None:
                for h in existing.get_hooks():
                    mgr.register(h)
            return mgr
        except Exception:  # noqa: BLE001
            logger.exception("Failed to build LifecycleHookManager")
            return None

    @router.get(
        "/hooks",
        response_model=HookListResponse,
        tags=["Hooks"],
        summary="List registered lifecycle hooks",
        responses=_error_responses(401, 503),
    )
    async def hooks_list(
        request: Request,  # type: ignore[name-defined]
        hook_type: str | None = Query(None, description="Filter by HookType"),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        mgr = _get_hook_manager(request)
        if mgr is None:
            raise _service_unavailable("Lifecycle Hook Manager")
        target = None
        if hook_type:
            try:
                from doctoragent.orchestration.lifecycle_hooks import HookType

                target = HookType(hook_type)
            except ValueError:
                pass
        hooks = mgr.get_hooks(target)
        items = [
            {
                "name": h.name,
                "hook_type": h.hook_type.value
                if hasattr(h.hook_type, "value")
                else str(h.hook_type),
                "priority": h.priority,
                "enabled": h.enabled,
                "has_condition": h.condition is not None,
            }
            for h in hooks
        ]
        types = mgr.get_hook_types()
        return {
            "total": len(items),
            "hook_types": [t.value if hasattr(t, "value") else str(t) for t in types],
            "hooks": items,
        }

    @router.post(
        "/hooks",
        response_model=HookListResponse,
        tags=["Hooks"],
        summary="Register a marker lifecycle hook",
        description=(
            "Register a named marker hook (no-op callback) for the given "
            "``hook_type``. Real Python callbacks cannot be uploaded via HTTP; "
            "this endpoint lets the console model & enable/disable hook slots "
            "and observe the registered set."
        ),
        responses=_error_responses(401, 403, 422, 503, 500),
    )
    async def hooks_register(
        body: HookRegisterRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        mgr = _get_hook_manager(request)
        if mgr is None:
            raise _service_unavailable("Lifecycle Hook Manager")
        try:
            from doctoragent.orchestration.lifecycle_hooks import Hook, HookType
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Lifecycle Hook Manager") from None
        try:
            ht = HookType(body.hook_type)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=422, detail=f"Unknown hook_type: {body.hook_type}"
            ) from exc

        def _marker_callback(ctx: Any) -> None:  # noqa: ARG001
            logger.debug("Marker hook %s fired for %s", body.name, ht)

        hook = Hook(
            name=body.name,
            hook_type=ht,
            callback=_marker_callback,
            priority=body.priority,
            enabled=body.enabled,
        )
        mgr.register(hook)
        return {
            "total": 1,
            "hook_types": [ht.value],
            "hooks": [
                {
                    "name": hook.name,
                    "hook_type": ht.value,
                    "priority": hook.priority,
                    "enabled": hook.enabled,
                    "has_condition": False,
                }
            ],
        }

    @router.patch(
        "/hooks/{hook_name}",
        response_model=MessageResponse,
        tags=["Hooks"],
        summary="Enable / disable a hook",
        responses=_error_responses(401, 403, 404, 503, 500),
    )
    async def hooks_toggle(
        hook_name: str,
        request: Request,  # type: ignore[name-defined]
        enabled: bool = Query(..., description="true to enable, false to disable"),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        mgr = _get_hook_manager(request)
        if mgr is None:
            raise _service_unavailable("Lifecycle Hook Manager")
        ok = mgr.enable(hook_name) if enabled else mgr.disable(hook_name)
        if not ok:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Hook '{hook_name}' not found"
            )
        return {"message": f"Hook '{hook_name}' {'enabled' if enabled else 'disabled'}"}

    @router.delete(
        "/hooks/{hook_name}",
        response_model=MessageResponse,
        tags=["Hooks"],
        summary="Unregister a hook",
        responses=_error_responses(401, 403, 404, 503, 500),
    )
    async def hooks_delete(
        hook_name: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        mgr = _get_hook_manager(request)
        if mgr is None:
            raise _service_unavailable("Lifecycle Hook Manager")
        ok = mgr.unregister(hook_name)
        if not ok:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Hook '{hook_name}' not found"
            )
        return {"message": f"Unregistered hook '{hook_name}'"}

    @router.get(
        "/hooks/types",
        tags=["Hooks"],
        summary="List all known HookTypes",
        responses=_error_responses(401, 503),
    )
    async def hooks_types(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        try:
            from doctoragent.orchestration.lifecycle_hooks import HookType
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Lifecycle Hook Manager") from None
        types = [{"value": t.value, "name": t.name} for t in HookType]
        return {"total": len(types), "types": types}

    # ===================================================================
    # 13. Observability
    # ===================================================================
    # In-process observability ring buffers — populated by the metrics
    # module and a lightweight trace collector kept on app.state.

    def _get_observability_state(request: Request) -> dict[str, Any]:  # type: ignore[name-defined]
        state = request.app.state
        obs = getattr(state, "observability", None)
        if not isinstance(obs, dict):
            obs = {
                "traces": [],
                "logs": [],
                "feedback": [],
                "experiments": [],
                "prompt_templates": {},
            }
            state.observability = obs
        return obs

    @router.get(
        "/observability/snapshot",
        response_model=ObservabilitySnapshotResponse,
        tags=["Observability"],
        summary="Get observability snapshot",
        description="Returns the latest in-process metrics, recent traces and recent logs.",
        responses=_error_responses(401, 503),
    )
    async def observability_snapshot(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        try:
            from doctoragent.observability import get_metrics
        except ImportError:  # pragma: no cover
            get_metrics = lambda: {}  # noqa: E731

        metrics: dict[str, Any] = {}
        if get_metrics is not None:
            try:
                metrics = _serialize(get_metrics())
            except Exception:  # noqa: BLE001
                metrics = {}

        obs = _get_observability_state(request)
        traces = list(obs.get("traces", []))[-50:]
        logs = list(obs.get("logs", []))[-100:]

        # Health snapshot pulled from the agent's task_store if available.
        health: dict[str, Any] = {}
        ts = _get_task_store(request)
        if ts is not None:
            try:
                stats = ts.stats() if hasattr(ts, "stats") else {}
                health["task_store"] = _serialize(stats)
            except Exception:  # noqa: BLE001
                health["task_store"] = {}
        pool = _get_pipeline_pool(request)
        if pool is not None:
            try:
                health["pipeline_pool"] = _serialize(pool.stats())
            except Exception:  # noqa: BLE001
                health["pipeline_pool"] = {}
        health["timestamp"] = _now_iso()

        return {
            "metrics": metrics,
            "traces": traces,
            "recent_logs": logs,
            "health": health,
        }

    @router.get(
        "/observability/metrics",
        tags=["Observability"],
        summary="Get raw Prometheus metrics snapshot",
        responses=_error_responses(401, 503),
    )
    async def observability_metrics(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        try:
            from doctoragent.observability import get_metrics
        except ImportError:  # pragma: no cover
            raise _service_unavailable("Metrics") from None
        try:
            return _serialize(get_metrics())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to read metrics: {exc}"
            ) from exc

    @router.get(
        "/observability/traces",
        tags=["Observability"],
        summary="List recent in-process traces",
        responses=_error_responses(401, 503),
    )
    async def observability_traces(
        request: Request,  # type: ignore[name-defined]
        limit: int = Query(50, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        traces = list(obs.get("traces", []))[-limit:]
        return {"total": len(traces), "traces": traces}

    @router.get(
        "/observability/logs",
        tags=["Observability"],
        summary="List recent in-process logs",
        responses=_error_responses(401, 503),
    )
    async def observability_logs(
        request: Request,  # type: ignore[name-defined]
        level: str | None = Query(None, description="Filter by level (INFO/WARNING/ERROR)"),  # type: ignore[name-defined]  # noqa: B008
        limit: int = Query(100, ge=1, le=1000),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        logs = list(obs.get("logs", []))[-limit:]
        if level:
            logs = [log for log in logs if str(log.get("level", "")).upper() == level.upper()]
        return {"total": len(logs), "logs": logs}

    # ===================================================================
    # 14. Reinforcement Learning / Feedback
    # ===================================================================

    @router.post(
        "/rl/feedback",
        response_model=RLFeedbackResponse,
        tags=["Reinforcement Learning"],
        summary="Record user feedback (reward signal)",
        description=(
            "Records a thumbs-up / thumbs-down style feedback for an agent "
            "response. The reward (rating in [-1, 1]) is appended to the "
            "RL feedback ring buffer and used by the self-evolution engine "
            "to bias future prompt/tool selection."
        ),
        responses=_error_responses(401, 403, 422, 503, 500),
    )
    async def rl_feedback(
        body: RLFeedbackRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        fb_list = obs.setdefault("feedback", [])
        feedback_id = uuid4().hex
        reward = float(body.rating)
        entry = {
            "feedback_id": feedback_id,
            "task_id": body.task_id,
            "query": body.query,
            "response": body.response[:500],
            "rating": body.rating,
            "reward": reward,
            "comment": body.comment,
            "user_id": body.user_id,
            "timestamp": _now_iso(),
        }
        fb_list.append(entry)
        # Keep ring buffer bounded.
        if len(fb_list) > 1000:
            del fb_list[: len(fb_list) - 1000]
        return {
            "recorded": True,
            "feedback_id": feedback_id,
            "reward": reward,
            "message": "Feedback recorded",
        }

    @router.get(
        "/rl/preferences",
        response_model=RLPreferencesResponse,
        tags=["Reinforcement Learning"],
        summary="List collected preference feedback",
        responses=_error_responses(401, 503),
    )
    async def rl_preferences(
        request: Request,  # type: ignore[name-defined]
        limit: int = Query(50, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        fb = list(obs.get("feedback", []))
        positive = sum(1 for f in fb if f.get("rating") > 0)
        neutral = sum(1 for f in fb if f.get("rating") == 0)
        negative = sum(1 for f in fb if f.get("rating") < 0)
        avg = (sum(f.get("reward", 0.0) for f in fb) / len(fb)) if fb else 0.0
        recent = fb[-limit:]
        recent.reverse()
        return {
            "total": len(fb),
            "positive": positive,
            "neutral": neutral,
            "negative": negative,
            "average_reward": round(avg, 4),
            "recent": recent,
        }

    @router.get(
        "/rl/policy",
        response_model=RLPolicyResponse,
        tags=["Reinforcement Learning"],
        summary="Get current policy stats",
        description="Returns the current self-evolution policy snapshot: experience count, top tools and lessons.",
        responses=_error_responses(401, 503),
    )
    async def rl_policy(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        fb = list(obs.get("feedback", []))
        avg = (sum(f.get("reward", 0.0) for f in fb) / len(fb)) if fb else 0.0

        # Pull top tools / lessons from the self-evolution engine if available.
        top_tools: list[dict[str, Any]] = []
        top_lessons: list[dict[str, Any]] = []
        total_experiences = 0
        try:
            from doctoragent.model.self_evolution import SelfEvolutionEngine

            task_store = _get_task_store(request)
            llm_provider = _get_llm_provider(request)
            if task_store is not None and llm_provider is not None:
                engine = _get_or_create(
                    request,
                    "self_evolution_engine",
                    lambda: SelfEvolutionEngine(task_store, llm_provider),
                )
                if engine is not None:
                    try:
                        with engine._connect() as conn:  # type: ignore[attr-defined]
                            conn.row_factory = _row_to_dict
                            try:
                                rows = conn.execute(
                                    "SELECT tool_name, COUNT(*) as c FROM experiences WHERE tool_name IS NOT NULL GROUP BY tool_name ORDER BY c DESC LIMIT 10"
                                ).fetchall()
                                top_tools = [dict(r) for r in rows]
                            except Exception:  # noqa: BLE001
                                top_tools = []
                            try:
                                cnt = conn.execute(
                                    "SELECT COUNT(*) AS c FROM experiences"
                                ).fetchone()
                                total_experiences = int(cnt["c"]) if cnt else 0
                            except Exception:  # noqa: BLE001
                                total_experiences = 0
                            try:
                                rows = conn.execute(
                                    "SELECT query_pattern, lesson, success_rate FROM experiences ORDER BY success_rate DESC LIMIT 10"
                                ).fetchall()
                                top_lessons = [dict(r) for r in rows]
                            except Exception:  # noqa: BLE001
                                top_lessons = []
                    except Exception:  # noqa: BLE001
                        pass
        except ImportError:  # pragma: no cover
            pass

        return {
            "policy_version": "v1",
            "total_experiences": total_experiences,
            "total_feedback": len(fb),
            "average_reward": round(avg, 4),
            "top_tools": top_tools,
            "top_lessons": top_lessons,
        }

    # ===================================================================
    # 15. Multi-Agent Collaboration
    # ===================================================================

    @router.get(
        "/collab/agents",
        response_model=CollabAgentsResponse,
        tags=["Collaboration"],
        summary="List registered collaborative agents",
        description="Returns the agent registry: orchestrator + specialist roles.",
        responses=_error_responses(401, 503),
    )
    async def collab_agents(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        agents_list: list[dict[str, Any]] = []
        agent = _get_agent(request)
        if agent is not None:
            agents_list.append(
                {
                    "name": "orchestrator",
                    "role": "orchestrator",
                    "type": type(agent).__name__,
                    "description": "Top-level multi-agent orchestrator",
                    "tools": list(getattr(agent, "tools", {}).keys())
                    if hasattr(agent, "tools") and isinstance(getattr(agent, "tools", None), dict)
                    else [],
                }
            )
            sub = getattr(agent, "specialists", None) or getattr(agent, "agents", None)
            if isinstance(sub, dict):
                for name, inst in sub.items():
                    agents_list.append(
                        {
                            "name": name,
                            "role": "specialist",
                            "type": type(inst).__name__ if inst is not None else "",
                            "description": (inst.__doc__ or "").strip().split("\n")[0]
                            if inst is not None and getattr(inst, "__doc__", None)
                            else "",
                            "tools": [],
                        }
                    )
            elif isinstance(sub, list):
                for i, inst in enumerate(sub):
                    agents_list.append(
                        {
                            "name": getattr(inst, "name", None) or f"agent-{i}",
                            "role": "specialist",
                            "type": type(inst).__name__ if inst is not None else "",
                            "description": (inst.__doc__ or "").strip().split("\n")[0]
                            if inst is not None and getattr(inst, "__doc__", None)
                            else "",
                            "tools": [],
                        }
                    )
        # Always provide a sensible default catalog even when no agent wired.
        if not agents_list:
            agents_list = [
                {
                    "name": "orchestrator",
                    "role": "orchestrator",
                    "type": "Agent",
                    "description": "Top-level multi-agent orchestrator",
                    "tools": [],
                },
                {
                    "name": "historian",
                    "role": "specialist",
                    "type": "SpecialistAgent",
                    "description": "Patient history specialist",
                    "tools": [],
                },
                {
                    "name": "pharmacologist",
                    "role": "specialist",
                    "type": "SpecialistAgent",
                    "description": "Drug interaction specialist",
                    "tools": [],
                },
                {
                    "name": "researcher",
                    "role": "specialist",
                    "type": "SpecialistAgent",
                    "description": "Literature review specialist",
                    "tools": [],
                },
            ]
        return {"total": len(agents_list), "agents": agents_list}

    @router.post(
        "/collab/delegate",
        response_model=CollabDelegateResponse,
        tags=["Collaboration"],
        summary="Delegate a task to a specialist role",
        description="Send a task message to the named role and return the response.",
        responses=_error_responses(401, 403, 422, 503, 500),
    )
    async def collab_delegate(
        body: CollabDelegateRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        msgs = obs.setdefault("collab_messages", [])
        msg_id = uuid4().hex
        msgs.append(
            {
                "message_id": msg_id,
                "from": "console",
                "to": body.role,
                "task": body.task,
                "context": body.context,
                "timestamp": _now_iso(),
                "direction": "out",
            }
        )

        agent = _get_agent(request)
        response_text = ""
        rounds = 0
        if agent is not None:
            import asyncio

            # Try async delegate method first (new implementation)
            if hasattr(agent, "delegate") and asyncio.iscoroutinefunction(agent.delegate):
                try:
                    response_text = await agent.delegate(body.task, body.role)
                    rounds = 1
                except Exception as exc:  # noqa: BLE001
                    response_text = f"[delegation failed: {exc}]"
                    rounds = 0
            else:
                # Legacy: try sync delegation methods
                for meth in ("delegate_to", "send_to", "ask_specialist"):
                    fn = getattr(agent, meth, None)
                    if callable(fn):
                        try:
                            result = (
                                fn(body.role, body.task, **body.context)
                                if meth == "delegate_to"
                                else fn(body.task, body.role)
                            )
                            if asyncio.iscoroutine(result):
                                result = await result
                            if hasattr(result, "answer"):
                                response_text = result.answer
                            elif isinstance(result, str):
                                response_text = result
                            else:
                                response_text = _serialize(result)
                            rounds = 1
                            break
                        except Exception as exc:  # noqa: BLE001
                            response_text = f"[delegation failed: {exc}]"
                            rounds = 0
        if not response_text:
            response_text = (
                f"[stub] Delegated task to role '{body.role}': {body.task[:200]}. "
                "Wire a real delegation method on the agent to execute."
            )
        msgs.append(
            {
                "message_id": uuid4().hex,
                "from": body.role,
                "to": "console",
                "task": body.task,
                "response": response_text[:500],
                "timestamp": _now_iso(),
                "direction": "in",
            }
        )
        if len(msgs) > 200:
            del msgs[: len(msgs) - 200]
        return {
            "delegated": True,
            "role": body.role,
            "message_id": msg_id,
            "response": response_text,
            "rounds": rounds,
        }

    @router.get(
        "/collab/messages",
        response_model=CollabMessagesResponse,
        tags=["Collaboration"],
        summary="List inter-agent messages",
        responses=_error_responses(401, 503),
    )
    async def collab_messages(
        request: Request,  # type: ignore[name-defined]
        limit: int = Query(100, ge=1, le=500),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        msgs = list(obs.get("collab_messages", []))[-limit:]
        msgs.reverse()
        return {"total": len(msgs), "messages": msgs}

    # ===================================================================
    # 16. Plugin Management
    # ===================================================================

    @router.get(
        "/plugins",
        response_model=PluginListResponse,
        tags=["Plugins"],
        summary="List installed plugins",
        description="Returns plugins detected by entry-point discovery (best-effort).",
        responses=_error_responses(401, 503),
    )
    async def plugins_list(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        plugins: list[dict[str, Any]] = []
        try:
            from importlib.metadata import entry_points

            eps = entry_points()
            try:
                doctoragent_eps = eps.select(group="doctoragent.plugins")
            except AttributeError:  # py < 3.10
                doctoragent_eps = eps.get("doctoragent.plugins", [])
            for ep in doctoragent_eps:
                plugins.append(
                    {
                        "name": ep.name,
                        "value": ep.value,
                        "group": ep.group,
                        "enabled": True,
                        "loaded": False,
                    }
                )
        except Exception:  # noqa: BLE001
            pass
        # Add the built-in "plugins" so the console always
        # shows the user something useful.
        builtin = [
            {
                "name": "clinical",
                "value": "doctoragent.clinical",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
            {
                "name": "rag",
                "value": "doctoragent.model.rag",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
            {
                "name": "knowledge_graph",
                "value": "doctoragent.model.knowledge_graph",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
            {
                "name": "self_evolution",
                "value": "doctoragent.model.self_evolution",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
            {
                "name": "lifecycle_hooks",
                "value": "doctoragent.orchestration.lifecycle_hooks",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
            {
                "name": "observability",
                "value": "doctoragent.observability",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
            {
                "name": "security",
                "value": "doctoragent.security",
                "group": "builtin",
                "enabled": True,
                "loaded": True,
            },
        ]
        names = {p["name"] for p in plugins}
        for b in builtin:
            if b["name"] not in names:
                plugins.append(b)
        return {"total": len(plugins), "plugins": plugins}

    # ===================================================================
    # 17. A/B Experiments
    # ===================================================================

    @router.get(
        "/experiments",
        response_model=ExperimentsListResponse,
        tags=["Experiments"],
        summary="List A/B experiments",
        responses=_error_responses(401, 503),
    )
    async def experiments_list(
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        exps = list(obs.get("experiments", []))
        return {"total": len(exps), "experiments": exps}

    @router.post(
        "/experiments",
        response_model=ExperimentResponse,
        tags=["Experiments"],
        summary="Create an A/B experiment",
        responses=_error_responses(401, 403, 422, 503, 500),
    )
    async def experiments_create(
        body: ExperimentCreateRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        if len(body.variants) < 2:
            raise HTTPException(  # type: ignore[misc]
                status_code=422, detail="At least 2 variants are required"
            )
        obs = _get_observability_state(request)
        exps = obs.setdefault("experiments", [])
        exp_id = uuid4().hex[:12]
        # Initialise per-variant counters.
        variants = []
        for v in body.variants:
            variants.append(
                {
                    "name": v.get("name", "variant"),
                    "weight": float(v.get("weight", 1.0)),
                    "assignments": 0,
                    "rewards": 0.0,
                    "config": v.get("config", {}),
                }
            )
        exp = {
            "id": exp_id,
            "name": body.name,
            "description": body.description,
            "status": "running",
            "variants": variants,
            "metric": body.metric,
            "traffic_pct": body.traffic_pct,
            "results": {},
            "created_at": _now_iso(),
        }
        exps.append(exp)
        if len(exps) > 100:
            del exps[: len(exps) - 100]
        return exp

    @router.get(
        "/experiments/{exp_id}",
        response_model=ExperimentResponse,
        tags=["Experiments"],
        summary="Get experiment details + interim results",
        responses=_error_responses(401, 404, 503),
    )
    async def experiment_get(
        exp_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        exps = obs.get("experiments", [])
        for exp in exps:
            if exp.get("id") == exp_id:
                # Compute interim results.
                results: dict[str, Any] = {}
                total_assign = sum(v.get("assignments", 0) for v in exp["variants"])
                for v in exp["variants"]:
                    assign = v.get("assignments", 0)
                    rew = v.get("rewards", 0.0)
                    avg = (rew / assign) if assign else 0.0
                    share = (assign / total_assign) if total_assign else 0.0
                    results[v["name"]] = {
                        "assignments": assign,
                        "total_reward": round(rew, 4),
                        "average_reward": round(avg, 4),
                        "traffic_share": round(share, 4),
                    }
                exp["results"] = results
                return exp
        raise HTTPException(  # type: ignore[misc]
            status_code=404, detail=f"Experiment '{exp_id}' not found"
        )

    @router.post(
        "/experiments/{exp_id}/assign",
        tags=["Experiments"],
        summary="Assign a sample to a variant (record reward)",
        responses=_error_responses(401, 403, 404, 503, 500),
    )
    async def experiment_assign(
        exp_id: str,
        request: Request,  # type: ignore[name-defined]
        variant: str = Query(..., description="Variant name"),  # type: ignore[name-defined]  # noqa: B008
        reward: float = Query(0.0, ge=-1.0, le=1.0),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        for exp in obs.get("experiments", []):
            if exp.get("id") == exp_id:
                for v in exp["variants"]:
                    if v["name"] == variant:
                        v["assignments"] = v.get("assignments", 0) + 1
                        v["rewards"] = v.get("rewards", 0.0) + reward
                        return {"assigned": True, "variant": variant, "reward": reward}
                raise HTTPException(  # type: ignore[misc]
                    status_code=404,
                    detail=f"Variant '{variant}' not found in experiment '{exp_id}'",
                )
        raise HTTPException(  # type: ignore[misc]
            status_code=404, detail=f"Experiment '{exp_id}' not found"
        )

    @router.delete(
        "/experiments/{exp_id}",
        response_model=MessageResponse,
        tags=["Experiments"],
        summary="Stop & remove an experiment",
        responses=_error_responses(401, 403, 404, 503),
    )
    async def experiment_delete(
        exp_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        exps = obs.get("experiments", [])
        before = len(exps)
        obs["experiments"] = [e for e in exps if e.get("id") != exp_id]
        if len(obs["experiments"]) == before:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Experiment '{exp_id}' not found"
            )
        return {"message": f"Stopped experiment '{exp_id}'"}

    # ===================================================================
    # 18. Prompt Templates (versioned)
    # ===================================================================

    @router.get(
        "/prompts",
        response_model=PromptTemplatesListResponse,
        tags=["Prompt Templates"],
        summary="List prompt templates",
        responses=_error_responses(401, 503),
    )
    async def prompts_list(
        request: Request,  # type: ignore[name-defined]
        tag: str | None = Query(None),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.setdefault("prompt_templates", {})
        items = list(store.values())
        if tag:
            items = [t for t in items if tag in t.get("tags", [])]
        items.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
        return {"total": len(items), "templates": items}

    @router.post(
        "/prompts",
        response_model=PromptTemplateResponse,
        tags=["Prompt Templates"],
        summary="Create a prompt template",
        responses=_error_responses(401, 403, 422, 503, 500),
    )
    async def prompts_create(
        body: PromptTemplateCreateRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.setdefault("prompt_templates", {})
        # Generate stable id from name (slugify).
        slug = _slug(body.name)
        base_id = slug
        idx = 1
        while base_id in store:
            idx += 1
            base_id = f"{slug}-{idx}"
        now = _now_iso()
        template = {
            "id": base_id,
            "name": body.name,
            "template": body.template,
            "variables": body.variables or _extract_variables(body.template),
            "description": body.description,
            "tags": body.tags,
            "version": 1,
            "history": [{"version": 1, "template": body.template, "updated_at": now}],
            "created_at": now,
            "updated_at": now,
        }
        store[base_id] = template
        return template

    @router.get(
        "/prompts/{template_id}",
        response_model=PromptTemplateResponse,
        tags=["Prompt Templates"],
        summary="Get a prompt template",
        responses=_error_responses(401, 404, 503),
    )
    async def prompts_get(
        template_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.get("prompt_templates", {})
        t = store.get(template_id)
        if t is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        return t

    @router.put(
        "/prompts/{template_id}",
        response_model=PromptTemplateResponse,
        tags=["Prompt Templates"],
        summary="Update a prompt template (creates a new version)",
        responses=_error_responses(401, 403, 404, 422, 503, 500),
    )
    async def prompts_update(
        template_id: str,
        body: PromptTemplateCreateRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.get("prompt_templates", {})
        t = store.get(template_id)
        if t is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        now = _now_iso()
        new_version = int(t.get("version", 1)) + 1
        history = list(t.get("history", []))
        history.append({"version": new_version, "template": body.template, "updated_at": now})
        t.update(
            {
                "name": body.name,
                "template": body.template,
                "variables": body.variables or _extract_variables(body.template),
                "description": body.description,
                "tags": body.tags,
                "version": new_version,
                "history": history,
                "updated_at": now,
            }
        )
        return t

    @router.delete(
        "/prompts/{template_id}",
        response_model=MessageResponse,
        tags=["Prompt Templates"],
        summary="Delete a prompt template",
        responses=_error_responses(401, 403, 404, 503),
    )
    async def prompts_delete(
        template_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.get("prompt_templates", {})
        if template_id not in store:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        del store[template_id]
        return {"message": f"Deleted template '{template_id}'"}

    @router.post(
        "/prompts/{template_id}/render",
        response_model=PromptRenderResponse,
        tags=["Prompt Templates"],
        summary="Render a prompt template with variables",
        responses=_error_responses(401, 403, 404, 422, 503, 500),
    )
    async def prompts_render(
        template_id: str,
        body: PromptRenderRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.get("prompt_templates", {})
        t = store.get(template_id)
        if t is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        template = t["template"]
        variables = t.get("variables", [])
        missing = [v for v in variables if v not in body.variables]
        rendered = template
        for v in variables:
            rendered = rendered.replace("{" + v + "}", str(body.variables.get(v, "{" + v + "}")))
        return {"rendered": rendered, "missing_variables": missing}

    @router.get(
        "/prompts/{template_id}/versions",
        tags=["Prompt Templates"],
        summary="List template versions",
        responses=_error_responses(401, 404, 503),
    )
    async def prompt_versions(
        template_id: str,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        obs = _get_observability_state(request)
        store: dict[str, Any] = obs.get("prompt_templates", {})
        t = store.get(template_id)
        if t is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        return {
            "id": template_id,
            "current_version": t.get("version", 1),
            "versions": list(t.get("history", [])),
        }

    # ── SMART-on-FHIR launch flow (OAuth2 + PKCE) ─────────────────────
    # Exposes the SMARTClient as HTTP routes so the console can initiate an
    # EHR authorize redirect and complete the code-for-token exchange. The
    # PKCE verifier + state are held in app.state keyed by the state token
    # for the duration of the round-trip.

    class SMARTAuthorizeRequest(BaseModel):
        """Request body for ``POST /smart/authorize``."""

        model_config = ConfigDict(extra="forbid")
        fhir_base: str = Field(
            ..., description="FHIR base URL (e.g. https://fhir.example.com/fhir)."
        )
        client_id: str = Field(..., description="Registered SMART client_id.")
        redirect_uri: str = Field(..., description="OAuth2 redirect URI.")
        scopes: list[str] = Field(
            default_factory=lambda: ["patient/*.read", "openid", "fhirUser"],
            description="Requested SMART scopes.",
        )
        launch: str | None = Field(None, description="EHR launch token (EHR launch).")
        aud: str | None = Field(None, description="Audience (FHIR base) for EHR launch.")
        client_secret: str | None = Field(None, description="Confidential client secret.")

    @router.post(  # type: ignore[name-defined]
        "/smart/authorize",
        tags=["SMART-on-FHIR"],
        summary="Initiate a SMART-on-FHIR launch (build authorize URL + PKCE)",
        description=(
            "Discovers the EHR's SMART configuration and builds the "
            "authorization URL with PKCE S256. Returns the URL the browser "
            "should redirect to plus a state token; the PKCE verifier is "
            "held server-side keyed by state for the callback. Requires "
            "the `auth` extra (authlib)."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def smart_authorize(
        req: SMARTAuthorizeRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Build the SMART authorize URL + PKCE verifier."""
        from doctoragent.clinical.fhir.smart import SMARTClient, SMARTLaunchParams

        client = SMARTClient(
            fhir_base=req.fhir_base,
            client_id=req.client_id,
            redirect_uri=req.redirect_uri,
            client_secret=req.client_secret,
        )
        try:
            async with client:
                discovery = await client.discover()
                params = SMARTLaunchParams(
                    client_id=req.client_id,
                    redirect_uri=req.redirect_uri,
                    scopes=tuple(req.scopes),
                    launch=req.launch,
                    aud=req.aud,
                )
                url, verifier, state = client.build_authorization_url(discovery, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.exception("SMART authorize failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"SMART launch failed: {exc}"
            ) from exc
        # Persist the verifier + discovery for the callback.
        pending = getattr(request.app.state, "smart_pending", None) or {}
        pending[state] = {
            "verifier": verifier,
            "fhir_base": req.fhir_base,
            "client_id": req.client_id,
            "redirect_uri": req.redirect_uri,
            "client_secret": req.client_secret,
            "scopes": req.scopes,
        }
        request.app.state.smart_pending = pending
        return {
            "authorize_url": url,
            "state": state,
            "scopes": req.scopes,
            "pkce_method": "S256",
            "supports_pkce": True,
        }

    class SMARTCallbackRequest(BaseModel):
        """Request body for ``POST /smart/callback``."""

        model_config = ConfigDict(extra="forbid")
        code: str = Field(..., description="Authorization code from the EHR redirect.")
        state: str = Field(..., description="State token returned by /smart/authorize.")

    @router.post(  # type: ignore[name-defined]
        "/smart/callback",
        tags=["SMART-on-FHIR"],
        summary="Complete the SMART launch (exchange code for token)",
        description=(
            "Exchanges the authorization code (with the PKCE verifier held "
            "from /smart/authorize) for a SMART access token + patient "
            "context. Returns the token set; the caller stores the "
            "access_token for subsequent FHIR reads."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def smart_callback(
        req: SMARTCallbackRequest,
        request: Request,  # type: ignore[name-defined]
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """Exchange the SMART auth code for a token."""
        from doctoragent.clinical.fhir.smart import SMARTClient, SMARTLaunchError

        pending = getattr(request.app.state, "smart_pending", None) or {}
        ctx = pending.get(req.state)
        if ctx is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="Unknown or expired SMART state; re-initiate /smart/authorize",
            )
        client = SMARTClient(
            fhir_base=ctx["fhir_base"],
            client_id=ctx["client_id"],
            redirect_uri=ctx["redirect_uri"],
            client_secret=ctx.get("client_secret"),
        )
        try:
            async with client:
                discovery = await client.discover()
                result = await client.exchange_code(
                    discovery,
                    code=req.code,
                    verifier=ctx["verifier"],
                    state=req.state,
                )
        except SMARTLaunchError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"SMART token exchange failed: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("SMART callback failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"SMART callback failed: {exc}"
            ) from exc
        # Clear the pending PKCE verifier (single-use).
        pending.pop(req.state, None)
        request.app.state.smart_pending = pending
        return {
            "access_token": result.access_token,
            "token_type": getattr(result, "token_type", "Bearer"),
            "expires_in": getattr(result, "expires_in", None),
            "refresh_token": getattr(result, "refresh_token", None),
            "patient_id": getattr(result, "patient_id", None),
            "scopes": list(getattr(result, "scopes", []) or ctx["scopes"]),
        }

    # ===================================================================
    # 19. Compliance Checker (合规检查)
    # ===================================================================
    # 检测企业用户缺失的资质，返回合规事项、教程链接与警告信息。
    # 状态持久化在 ~/.doctoragent/compliance_status.json。

    @router.get(
        "/compliance/status",
        tags=["Compliance"],
        summary="获取合规状态",
        description="检查所有合规项的当前状态，返回合规项列表及汇总统计。",
        responses=_error_responses(401, 503),
    )
    async def get_compliance_status(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """获取合规状态。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        return checker.check_compliance()

    @router.get(
        "/compliance/summary",
        tags=["Compliance"],
        summary="获取合规摘要",
        description="返回前端展示用的合规摘要：整体状态、完成率、阻断项等。",
        responses=_error_responses(401, 503),
    )
    async def get_compliance_summary(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """获取合规摘要。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        return checker.get_compliance_summary()

    @router.get(
        "/compliance/items",
        tags=["Compliance"],
        summary="获取所有合规项列表",
        responses=_error_responses(401, 503),
    )
    async def get_compliance_items(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """获取所有合规项列表。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        return {"items": [item.model_dump() for item in checker.ITEMS]}

    @router.get(
        "/compliance/missing",
        tags=["Compliance"],
        summary="获取未完成的必须合规项",
        responses=_error_responses(401, 503),
    )
    async def get_missing_compliance(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """获取未完成的必须合规项。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        return {"items": [item.model_dump() for item in checker.get_missing_items()]}

    @router.post(
        "/compliance/{item_id}/status",
        tags=["Compliance"],
        summary="更新合规项状态",
        responses=_error_responses(401, 403, 404, 422, 503),
    )
    async def update_compliance_status(
        item_id: str,
        status: str = Body(
            ..., description="新状态: not_started/in_progress/completed/not_required"
        ),  # type: ignore[name-defined]  # noqa: B008
        notes: str = Body("", description="可选备注"),  # type: ignore[name-defined]  # noqa: B008
        _auth: Any = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """更新合规项状态。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        success = checker.update_status(item_id, status, notes)
        if not success:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"合规项 {item_id} 不存在"
            )
        return {"success": True}

    @router.get(
        "/compliance/tutorial/{item_id}",
        tags=["Compliance"],
        summary="获取合规项的教程内容",
        responses=_error_responses(401, 404, 503),
    )
    async def get_compliance_tutorial(
        item_id: str,
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """获取合规项的教程内容。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        item = next((i for i in checker.ITEMS if i.id == item_id), None)
        if not item:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail=f"合规项 {item_id} 不存在"
            )
        # 读取教程文件（如果存在）
        content = ""
        if item.tutorial_path:
            tutorial_path = Path(__file__).parent.parent.parent / item.tutorial_path
            if tutorial_path.exists():
                content = tutorial_path.read_text(encoding="utf-8")
            else:
                content = f"# {item.name}\n\n教程文档待补充：{item.tutorial_path}"
        return {
            "item": item.model_dump(),
            "content": content,
        }

    @router.get(
        "/compliance/warning",
        tags=["Compliance"],
        summary="获取合规警告（用于前端弹窗）",
        responses=_error_responses(401, 503),
    )
    async def get_compliance_warning(
        _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        """获取合规警告（用于前端弹窗）。"""
        from doctoragent.clinical.compliance_checker import get_compliance_checker

        checker = get_compliance_checker()
        return {
            "should_show": checker.should_show_warning(),
            "message": checker.get_warning_message(),
            "blocking_items": [item.model_dump() for item in checker.get_blocking_items()],
        }

else:  # pragma: no cover - FastAPI not installed
    router = None  # type: ignore[assignment]

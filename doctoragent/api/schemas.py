"""Pydantic schemas for inter-layer messaging."""

from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from doctoragent.compat import StrEnum


class SensitivityLevel(StrEnum):
    """Content sensitivity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FileEvent(BaseModel):
    """File system event from Inbox watcher."""

    event_id: UUID
    source_path: Path
    event_type: str = "created"


class ClassificationResult(BaseModel):
    """Model output for a file classification."""

    sensitivity: SensitivityLevel
    category: str
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    disguise_name: str
    disguise_extension: str


class EncryptResult(BaseModel):
    """Result of an encryption operation."""

    task_id: UUID
    vault_path: Path
    salt: bytes
    nonce: bytes


class TaskStatus(BaseModel):
    """Agent task status."""

    task_id: UUID
    state: str
    message: str = ""


class TaskSummary(BaseModel):
    """Lightweight task summary for UI lists."""

    task_id: UUID
    state: str
    message: str = ""
    source_path: Path | None = None


class SearchQuery(BaseModel):
    """Natural language search query."""

    query: str = Field(max_length=1000)
    top_k: int = Field(default=5, ge=1, le=100)
    semantic: bool = False


class SearchResult(BaseModel):
    """Single search result."""

    vault_path: Path
    category: str
    summary: str
    score: float
    text: str = ""  # BM25 chunk content match (optional, populated when chunk hit)


class BrowserSubmission(BaseModel):
    """Encrypted content payload from the browser extension (Phase 7.5).

    The extension encrypts the plaintext with AES-256-GCM using a key
    derived from the API bearer token via PBKDF2-SHA256.  The server
    derives the same key from the token it already holds, decrypts the
    content, and writes the plaintext to the Inbox directory so the
    normal classification/encryption pipeline can process it.
    """

    content: str = Field(description="Base64-encoded AES-256-GCM ciphertext")
    nonce: str = Field(description="Base64-encoded 12-byte GCM nonce")
    salt: str = Field(description="Base64-encoded PBKDF2 salt")
    filename: str | None = Field(default=None, description="Optional filename for the Inbox file")
    source: str = Field(default="browser", description="Origin label (selection, page, manual)")
    iterations: int | None = Field(
        default=None,
        description="PBKDF2 iteration count the extension used to derive the AES key. "
        "When present the server decrypts with exactly this count; when absent the "
        "server falls back to trying the current then legacy counts for backwards "
        "compatibility with older extensions.",
    )


# ---------------------------------------------------------------------------
# RAG / Agent endpoints (POST /vault/ask, POST /vault/agent)
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    """Request body for POST /vault/ask (RAG question answering)."""

    question: str = Field(
        min_length=1, max_length=2000, description="The question to ask the RAG pipeline"
    )
    top_k: int = Field(default=5, ge=1, le=50, description="Number of retrieval results to use")
    session_id: str | None = Field(
        default=None,
        description="Conversation session ID for memory continuity. "
        "Omit to let the pipeline create a new session.",
    )
    use_memory: bool = Field(default=True, description="Whether to use the memory system")


class AgentStepSummary(BaseModel):
    """Lightweight summary of a single agent execution step."""

    step_type: str
    content: str
    tool_name: str | None = None


class AgentTaskRequest(BaseModel):
    """Request body for POST /vault/agent (agent task execution)."""

    task: str = Field(
        min_length=1, max_length=4000, description="The task for the agent to execute"
    )
    max_iterations: int = Field(
        default=10, ge=1, le=50, description="Maximum reasoning loop iterations"
    )
    session_id: str | None = Field(
        default=None,
        description="会话ID，用于多轮对话记忆连续性。前端传入聊天会话的ID，"
        "后端用它关联 episodic memory，使同一对话的历史可被召回。",
    )
    history: list[dict[str, str]] | None = Field(
        default=None,
        description="历史对话记录，格式 [{role: 'user'|'assistant', content: '...'}]。"
        "前端传入最近几轮对话，后端注入 agent 的 short_term_memory。",
    )


class AgentTaskResponse(BaseModel):
    """Response for POST /vault/agent."""

    answer: str
    task: str
    total_tool_calls: int = 0
    total_time_ms: float = 0.0
    steps: list[AgentStepSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Clinical endpoints (POST /clinical/analyze)
# ---------------------------------------------------------------------------


class ClinicalAnalyzeRequest(BaseModel):
    """Request body for POST /clinical/analyze.

    Runs the full clinical workflow: deterministic rule engine → parallel
    specialist LLM agents (history / drug-safety / literature) → documentation
    draft → comprehensive guardrail review. Every run is recorded in the
    tamper-evident audit log (``clinical_decision`` event) so the full
    decision chain is reconstructable for FDA SaMD / 21 CFR Part 11.
    """

    patient_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Patient data dict — may carry patient_id, vitals, "
        "labs, medications and allergies.",
    )
    query: str = Field(
        min_length=1,
        max_length=4000,
        description="The clinical question to answer (e.g. drug-interaction "
        "check, differential diagnosis support, literature review).",
    )


class ClinicalAnalyzeResponse(BaseModel):
    """Response for POST /clinical/analyze.

    Mirrors :class:`~doctoragent.clinical.agents.orchestrator.ClinicalWorkflowResult`
    as a JSON-safe dict so the schema stays decoupled from the clinical layer.
    ``requires_human_review`` is always honoured by the client: a True value
    means the output MUST be reviewed by a clinician before any action.
    """

    history_summary: str = ""
    safety_findings: list[dict[str, Any]] = Field(default_factory=list)
    literature: list[dict[str, Any]] = Field(default_factory=list)
    documentation: dict[str, Any] | None = None
    guardrail_result: dict[str, Any] = Field(default_factory=dict)
    disclaimer: str = ""
    citations: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


# ---------------------------------------------------------------------------
# Audit endpoints (GET /audit/logs, GET /audit/statistics, POST /audit/export)
# ---------------------------------------------------------------------------


class AuditExportRequest(BaseModel):
    """Request body for POST /audit/export."""

    start_time: str | None = Field(default=None, description="ISO-8601 start timestamp (inclusive)")
    end_time: str | None = Field(default=None, description="ISO-8601 end timestamp (exclusive)")
    format: str = Field(default="ndjson", description="Output format: ndjson or csv")


# ---------------------------------------------------------------------------
# Tenant endpoints (GET /tenants, POST /tenants)
# ---------------------------------------------------------------------------


class CreateTenantRequest(BaseModel):
    """Request body for POST /tenants."""

    tenant_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    key_provider_type: str = Field(default="filepassword")
    password: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Connection endpoints (POST /connections)
# ---------------------------------------------------------------------------


class ConnectionCreate(BaseModel):
    """Request body for creating a new platform connection.

    Mirrors the fields of :class:`doctoragent.connections.models.Connection`
    except ``id`` (auto-generated) so callers don't supply it.
    """

    name: str = Field(min_length=1, max_length=200)
    platform_type: str
    base_url: str
    model_name: str = ""
    auth_method: str = "none"
    api_key: str = ""
    username: str = ""
    password: str = ""
    is_local: bool = True
    is_enabled: bool = True
    is_cloud_authorized: bool = False
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    custom_headers: dict[str, str] = Field(default_factory=dict)
    custom_payload: dict[str, Any] = Field(default_factory=dict)
    timeout: float = Field(default=120.0, gt=0)
    priority: int = 0


# ===========================================================================
# Explicit response models (Phase 8 — replace ad-hoc ``dict[str, Any]`` returns)
#
# Every field carries a default so applying these as ``response_model`` never
# causes a validation failure when the handler returns a partial dict; unknown
# keys are filtered by FastAPI's response serialisation, which is the desired
# behaviour for a documented, contract-stable API.
# ===========================================================================


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str = "ok"
    version: str = ""


class MessageResponse(BaseModel):
    """Generic ``{"message": ...}`` envelope used by simple action endpoints."""

    message: str = ""


class VersionResponse(BaseModel):
    """Response for GET /api/version."""

    current_version: str = "v1"
    server_version: str = ""
    supported_versions: list[str] = Field(default_factory=lambda: ["v1"])
    deprecation_notice: str | None = None


class FileListItem(BaseModel):
    """A single entry in the paginated vault file listing."""

    task_id: str = ""
    vault_path: str = ""
    category: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)


class FileListResponse(BaseModel):
    """Response for GET /vault/files."""

    total: int = 0
    offset: int = 0
    limit: int = 50
    files: list[FileListItem] = Field(default_factory=list)


class RecentTaskItem(BaseModel):
    """A recent task entry inside :class:`VaultStatusResponse`."""

    task_id: str = ""
    state: str = ""
    message: str = ""
    source_path: str | None = None


class VaultStatusResponse(BaseModel):
    """Response for GET /vault/status."""

    inbox_files: int = 0
    vault_files: int = 0
    categories: dict[str, int] = Field(default_factory=dict)
    recent_tasks: list[RecentTaskItem] = Field(default_factory=list)


class FileMetadataResponse(BaseModel):
    """Response for GET /vault/files/{file_id}."""

    task_id: str = ""
    state: str = ""
    source_path: str | None = None
    vault_path: str | None = None
    category: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class AskResponse(BaseModel):
    """Response for POST /vault/ask.

    Mirrors the public fields of :class:`doctoragent.model.rag.RagResponse` so the
    API contract is stable and decoupled from the model layer.  All fields
    default so a partial handler return still validates.
    """

    answer: str = ""
    question: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    model_used: str = ""
    retrieval_method: str = "hybrid"
    total_chunks_searched: int = 0
    context_tokens_used: int = 0
    memory_used: bool = False
    conversation_turns: int = 0
    session_id: str | None = None
    evaluation_metrics: dict[str, Any] = Field(default_factory=dict)
    synthesis_strategy: str = "compact"


class SyncStatusResponse(BaseModel):
    """Response for GET /sync/status."""

    available: bool = False
    message: str = ""
    device_id: str | None = None
    running: bool = False
    peers_discovered: int = 0
    last_sync_times: dict[str, Any] = Field(default_factory=dict)


class WebhookEndpointItem(BaseModel):
    """A webhook endpoint description (URL + subscriptions, never the secret)."""

    url: str = ""
    events: list[str] = Field(default_factory=list)
    label: str | None = None


class WebhookListResponse(BaseModel):
    """Response for GET /webhooks/endpoints."""

    enabled: bool = False
    endpoints: list[WebhookEndpointItem] = Field(default_factory=list)


class WebhookDeliveryRecord(BaseModel):
    """A single webhook delivery record."""

    model_config = ConfigDict(extra="allow")

    event_id: str = ""
    event_type: str = ""
    endpoint: str = ""
    success: bool = False
    attempts: int = 0
    status_code: int | None = None
    error: str | None = None
    duration_ms: float = 0.0


class WebhookTestResponse(BaseModel):
    """Response for POST /webhooks/test."""

    attempted: int = 0
    message: str = ""


class BackupResponse(BaseModel):
    """Response for POST /backup/remote."""

    ok: bool = False
    backend: str = ""
    uploaded: int = 0
    skipped: int = 0
    removed: int = 0
    error: str | None = None


class InboxSubmitResponse(BaseModel):
    """Response for POST /inbox/submit."""

    ok: bool = False
    inbox_path: str = ""
    task_id: str = ""
    state: str = ""
    message: str = ""
    source: str = "browser"


class TenantInfoResponse(BaseModel):
    """Response for tenant list/create endpoints.

    Uses ``extra="allow"`` because :class:`~doctoragent.security.tenant.TenantInfo`
    is a dataclass whose serialised shape may grow; we forward every field.
    """

    model_config = ConfigDict(extra="allow")
    tenant_id: str = ""
    name: str = ""


class AuditLogRecord(BaseModel):
    """A single audit log record.

    Audit records are intentionally free-form, so ``extra="allow"`` keeps every
    field the logger emits instead of filtering it.
    """

    model_config = ConfigDict(extra="allow")
    timestamp: str | None = None
    event_type: str | None = None
    details: dict[str, Any] | None = None


class AuditStatisticsResponse(BaseModel):
    """Response for GET /audit/statistics (free-form aggregate stats)."""

    model_config = ConfigDict(extra="allow")


class PipelinePoolStatsResponse(BaseModel):
    """Response for GET /api/v1/system/pipeline-pool (introspection)."""

    pooled_tenants: int = 0
    tenants: list[dict[str, Any]] = Field(default_factory=list)


# ===========================================================================
# Batch operation schemas
# ===========================================================================


class BatchFileAction(str):
    """Allowed actions for POST /vault/files/batch."""

    DELETE = "delete"
    EXPORT = "export"
    RECLASSIFY = "reclassify"


class BatchFileOperationRequest(BaseModel):
    """Request body for POST /vault/files/batch."""

    action: str = Field(description="One of: delete, export, reclassify")
    file_ids: list[str] = Field(
        default_factory=list,
        max_length=500,
        description="Vault file task IDs to operate on",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific parameters (e.g. target category for reclassify)",
    )


class BatchFileResultItem(BaseModel):
    """Per-file result of a batch operation."""

    file_id: str
    success: bool = False
    message: str = ""
    detail: dict[str, Any] | None = None


class BatchFileOperationResponse(BaseModel):
    """Response for POST /vault/files/batch."""

    action: str = ""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[BatchFileResultItem] = Field(default_factory=list)


class BatchSearchRequest(BaseModel):
    """Request body for POST /vault/search/batch."""

    queries: list[SearchQuery] = Field(
        default_factory=list, max_length=50, description="Up to 50 search queries"
    )


class BatchSearchResultItem(BaseModel):
    """Per-query result of a batch search."""

    query: str = ""
    results: list[SearchResult] = Field(default_factory=list)
    error: str | None = None


class BatchSearchResponse(BaseModel):
    """Response for POST /vault/search/batch."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[BatchSearchResultItem] = Field(default_factory=list)


class BatchInboxSubmitResponse(BaseModel):
    """Response for POST /inbox/submit/batch."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[dict[str, Any]] = Field(default_factory=list)


# ===========================================================================
# Real-time (SSE / WebSocket) message schemas
# ===========================================================================


class StreamEvent(BaseModel):
    """Canonical SSE/WebSocket event envelope.

    Serialised to a single SSE ``data:`` line as
    ``data: {"type": "...", "content": "..."}\\n\\n``.
    """

    type: str = Field(
        description="Event type: status, token, retrieved, thought, action, observation, answer, done, error, ..."
    )
    content: Any | None = None
    data: Any | None = None
    timestamp: float | None = None


class WebSocketCommand(BaseModel):
    """Inbound command sent by a WebSocket client.

    Supported commands:

    * ``ping``           — client-initiated liveness check (server replies ``pong``).
    * ``subscribe``      — filter the event types the client wants (``data.types``).
    * ``cancel``         — cancel a running agent task (``data.run_id``).
    * ``unsubscribe``    — stop receiving events of given types (``data.types``).
    """

    command: str = Field(description="ping | subscribe | unsubscribe | cancel")
    data: dict[str, Any] | None = None

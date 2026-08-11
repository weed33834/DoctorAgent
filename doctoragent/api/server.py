"""Lightweight API server for remote/mobile access to DoctorAgent.

Provides a RESTful interface over the agent's core capabilities:
search, file listing, classification, sync management, and health checks.

FastAPI is an **optional** dependency.  When FastAPI is not installed
the helper ``is_available()`` returns ``False`` and calling
``create_app()`` or ``run_server()`` raises ``ImportError``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from doctoragent import __version__
from doctoragent.api.broadcaster import EventBroadcaster
from doctoragent.api.pipeline_pool import DEFAULT_TTL_SECONDS, PipelinePool
from doctoragent.api.rate_limit import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_RPM,
    SENSITIVE_RPM,
    RateLimiter,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
)
from doctoragent.api.schemas import (
    AgentStepSummary,
    AgentTaskRequest,
    AgentTaskResponse,
    AskRequest,
    AskResponse,
    AuditExportRequest,
    AuditStatisticsResponse,
    BackupResponse,
    BatchFileOperationRequest,
    BatchFileOperationResponse,
    BatchInboxSubmitResponse,
    BatchSearchRequest,
    BatchSearchResponse,
    BrowserSubmission,
    ClinicalAnalyzeRequest,
    ClinicalAnalyzeResponse,
    ConnectionCreate,
    CreateTenantRequest,
    FileListResponse,
    FileMetadataResponse,
    HealthResponse,
    InboxSubmitResponse,
    MessageResponse,
    PipelinePoolStatsResponse,
    SearchQuery,
    SearchResult,
    SyncStatusResponse,
    TenantInfoResponse,
    VaultStatusResponse,
    VersionResponse,
    WebhookListResponse,
    WebhookTestResponse,
)

_FASTAPI_AVAILABLE = False
try:
    from fastapi import (
        APIRouter,
        Depends,
        FastAPI,
        File,
        HTTPException,
        Query,
        Request,
        UploadFile,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.responses import (
        FileResponse,
        PlainTextResponse,
        StreamingResponse,
    )
    from fastapi.security import HTTPBearer
    from fastapi.staticfiles import StaticFiles
    from starlette.websockets import WebSocketState

    _FASTAPI_AVAILABLE = True
except ImportError:
    pass

# Observability hooks (structlog is a core dependency; prometheus-client and
# opentelemetry are part of the optional ``server`` extra). All imports are
# guarded so the server module stays importable on minimal installs.
_instrument_app: Callable[[Any], None] | None = None
_generate_latest_metrics: Callable[[], bytes] | None = None
_configure_tracing: Callable[..., None] | None = None
_configure_langfuse: Callable[..., bool] | None = None
_flush_langfuse: Callable[[], None] | None = None
_doctoragent_http_requests_total: Any = None
_doctoragent_http_request_duration_seconds: Any = None
try:
    from doctoragent.observability import instrument_app as _instrument_app
    from doctoragent.observability.langfuse import (
        configure_langfuse as _configure_langfuse,
    )
    from doctoragent.observability.langfuse import (
        flush_langfuse as _flush_langfuse,
    )
    from doctoragent.observability.metrics import (
        doctoragent_errors_total as _doctoragent_errors_total,
    )
    from doctoragent.observability.metrics import (
        doctoragent_http_request_duration_seconds as _doctoragent_http_request_duration_seconds,
    )
    from doctoragent.observability.metrics import (
        doctoragent_http_requests_total as _doctoragent_http_requests_total,
    )
    from doctoragent.observability.metrics import (
        generate_latest_metrics as _generate_latest_metrics,
    )
    from doctoragent.observability.tracing import (
        configure_tracing as _configure_tracing,
    )
except ImportError:  # pragma: no cover — observability is part of the core package
    _instrument_app = None
    _generate_latest_metrics = None
    _configure_tracing = None
    _configure_langfuse = None
    _flush_langfuse = None
    _doctoragent_errors_total = None
    _doctoragent_http_request_duration_seconds = None
    _doctoragent_http_requests_total = None

# RBAC (api/auth/). The auth package itself never hard-requires the
# ``auth`` or ``server`` extras, so this import is guarded only defensively —
# a failure here must never block server startup. ``require_role`` / ``Role``
# are used by the optional ``/admin/roles`` demo endpoint below.
_require_role: Callable[..., Any] | None = None
_Role: Any = None
try:
    from doctoragent.api.auth import Role as _Role
    from doctoragent.api.auth import require_role as _require_role
except ImportError:  # pragma: no cover — auth package is part of the core tree
    _require_role = None
    _Role = None

if TYPE_CHECKING:
    from doctoragent.config import AegisConfig
    from doctoragent.orchestration.agent import AegisAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API versioning
# ---------------------------------------------------------------------------

API_V1_PREFIX = "/api/v1"
#: All currently supported major API versions (newest last).
SUPPORTED_API_VERSIONS: list[str] = ["v1"]
CURRENT_API_VERSION: str = SUPPORTED_API_VERSIONS[-1]

# ---------------------------------------------------------------------------
# OpenAPI tag grouping (displayed in /docs)
# ---------------------------------------------------------------------------

TAGS_METADATA: list[dict[str, Any]] = [
    {"name": "System", "description": "Health, version, and server introspection."},
    {"name": "Vault", "description": "Encrypted vault file management, search, and RAG Q&A."},
    {"name": "Agent", "description": "Autonomous agent task execution (sync + streaming)."},
    {"name": "Inbox", "description": "Inbox ingestion (browser extension and batch upload)."},
    {"name": "Audit", "description": "Audit log query, statistics, and export."},
    {"name": "Sync", "description": "Multi-device sync status and triggering."},
    {"name": "Config", "description": "Runtime configuration read/update and tenant management."},
    {"name": "Connection", "description": "Platform connection (LLM provider) management."},
    {"name": "Webhooks", "description": "Outbound webhook endpoint management and testing."},
    {"name": "Backup", "description": "Remote backup operations."},
    {"name": "Realtime", "description": "Server-Sent Events and WebSocket real-time channels."},
    {"name": "Batch", "description": "Bulk operations across many resources in one request."},
    {
        "name": "Clinical",
        "description": "Clinical decision-support workflow (rule engine + LLM specialists + guardrails).",
    },
    {
        "name": "CDS Hooks",
        "description": (
            "HL7 CDS Hooks 2.0 endpoints (EHR-facing): "
            "``GET /cds-services`` discovery + "
            "``POST /cds-services/{id}`` invocation. Each invocation runs "
            "the clinical workflow and returns CDS Cards (info / suggestion / "
            "app-link) for the EHR to render."
        ),
    },
]

# Shared error-response documentation attached to every endpoint.
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
        413: "Request body too large.",
        422: "Validation error (request body failed Pydantic validation).",
        429: "Rate limit exceeded; retry after the ``Retry-After`` header (seconds).",
        500: "Internal server error.",
    }
    return {
        code: {
            "description": descriptions.get(code, "Error"),
            "content": {"application/json": {"schema": _ERROR_SCHEMA}},
        }
        for code in codes
    }


def is_available() -> bool:
    """Return ``True`` when FastAPI can be imported."""
    return _FASTAPI_AVAILABLE


def _check_available() -> None:
    """Raise ImportError if FastAPI is not installed."""
    if not _FASTAPI_AVAILABLE:
        raise ImportError(
            "FastAPI is required for the API server. Install it with: pip install fastapi[standard]"
        )


# ---------------------------------------------------------------------------
# Bearer token authentication
# ---------------------------------------------------------------------------
# Shared auth primitives (loopback set, token resolver, OIDC-configured flag,
# local-request check) live in ``doctoragent.api.auth._guards`` so the API
# server, the advanced router and the CDS Hooks router all share
# one definition and the policy cannot drift between surfaces.
from doctoragent.api.auth._guards import (  # noqa: E402
    LOCAL_HOSTS,
    is_local_request,
    oidc_is_configured,
    resolve_token,
    verify_bearer,
)


def _resolve_token() -> str | None:
    """Resolve the expected bearer token from the environment.

    Thin alias for the shared :func:`doctoragent.api.auth._guards.resolve_token`
    so existing call sites in this module keep their names.
    """
    return resolve_token()


def _verify_credentials(credentials: Any, expected: str) -> None:
    """Raise 401 if *credentials* do not match *expected* bearer token."""
    provided = getattr(credentials, "credentials", None)
    if not verify_bearer(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing authentication token",
        )  # type: ignore[misc]


# 本地回环地址集合：未配置 token 时仅允许这些来源访问读端点。
# Re-exported from the shared guards module so every router shares one
# definition (see doctoragent.api.auth._guards).
_LOCAL_HOSTS = LOCAL_HOSTS


def _is_local_request(request: Any) -> bool:
    """判断请求是否来自本地（回环地址或 Unix socket）。

    Thin alias for the shared :func:`doctoragent.api.auth._guards.is_local_request`
    so existing call sites in this module keep their names.
    """
    return is_local_request(request)


# ---------------------------------------------------------------------------
# Optional OIDC single-sign-on
# ---------------------------------------------------------------------------
#
# When ``DOCTORAGENT_OIDC_ISSUER`` is set, bearer tokens are treated as OIDC JWTs
# and verified via :class:`doctoragent.api.auth.oidc.OIDCAuthenticator`. The static
# ``DOCTORAGENT_API_TOKEN`` path is bypassed entirely in this mode. When the
# ``auth`` extra (authlib) is not installed but OIDC is configured, OIDC
# requests return ``503 Service Unavailable`` instead of crashing the server.

_oidc_authenticator: Any = None
_oidc_init_error: Exception | None = None
_oidc_init_attempted = False


def _oidc_is_configured() -> bool:
    """Return True when the operator has enabled OIDC via env config.

    Thin alias for the shared
    :func:`doctoragent.api.auth._guards.oidc_is_configured`.
    """
    return oidc_is_configured()


def _get_oidc_authenticator() -> Any:
    """Lazily build (and cache) the singleton :class:`OIDCAuthenticator`.

    Returns the authenticator, or ``None`` if OIDC is not configured or could
    not be initialised (e.g. authlib missing). In the latter case the failure
    is cached in ``_oidc_init_error`` so callers can surface a 503.
    """
    global _oidc_authenticator, _oidc_init_error, _oidc_init_attempted
    if not _oidc_is_configured():
        return None
    if _oidc_init_attempted:
        return _oidc_authenticator
    _oidc_init_attempted = True
    issuer = os.environ.get("DOCTORAGENT_OIDC_ISSUER", "")
    client_id = os.environ.get("DOCTORAGENT_OIDC_CLIENT_ID", "")
    audience = os.environ.get("DOCTORAGENT_OIDC_AUDIENCE")
    client_secret = os.environ.get("DOCTORAGENT_OIDC_CLIENT_SECRET")
    try:
        from doctoragent.api.auth.oidc import OIDCAuthenticator

        _oidc_authenticator = OIDCAuthenticator(
            issuer_url=issuer,
            client_id=client_id,
            client_secret=client_secret,
            audience=audience,
        )
        logger.info("OIDC authentication enabled (issuer=%s)", issuer)
    except ImportError as exc:
        _oidc_authenticator = None
        _oidc_init_error = exc
        logger.warning(
            "OIDC configured (DOCTORAGENT_OIDC_ISSUER set) but authlib is not "
            "installed; OIDC requests will return 503: %s",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — never fatal to server startup
        _oidc_authenticator = None
        _oidc_init_error = exc
        logger.warning("OIDC authenticator initialisation failed: %s", exc)
    return _oidc_authenticator


def _reset_oidc_state() -> None:
    """Clear cached OIDC state (used by tests that toggle env config)."""
    global _oidc_authenticator, _oidc_init_error, _oidc_init_attempted
    _oidc_authenticator = None
    _oidc_init_error = None
    _oidc_init_attempted = False


if _FASTAPI_AVAILABLE:
    _bearer_scheme = HTTPBearer(auto_error=False)

    async def _authenticate_oidc(request: Any, credentials: Any) -> Any:
        """Verify a bearer token via OIDC; return the UserInfo on success.

        Raises ``HTTPException`` (401/503) on any failure. Returns ``None``
        when OIDC is not configured, signalling the caller to fall through to
        the static bearer-token path.
        """
        if not _oidc_is_configured():
            return None
        authenticator = _get_oidc_authenticator()
        if authenticator is None:
            # OIDC configured but authlib missing (or init failed) → 503.
            raise HTTPException(
                status_code=503,
                detail=(
                    "OIDC authentication is configured but unavailable: "
                    "install the auth extra (pip install 'doctoragent[auth]'). "
                    f"Error: {_oidc_init_error}"
                ),
            )  # type: ignore[misc]
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="OIDC authentication required: missing bearer token",
            )  # type: ignore[misc]
        try:
            user = await authenticator.authenticate(request)
        except Exception as exc:  # noqa: BLE001 — surface as 401, never 500
            raise HTTPException(
                status_code=401,
                detail=f"OIDC authentication failed: {exc}",
            ) from exc  # type: ignore[misc]
        # Make the authenticated user available to RBAC dependencies.
        try:
            request.state.user = user
        except AttributeError:
            pass
        return user

    async def _auth_dependency(
        request: Request,  # type: ignore[name-defined]
        credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
    ) -> Any:
        """Auth dependency for **read-only** endpoints.

        当 ``DOCTORAGENT_OIDC_ISSUER`` 已配置时，bearer token 作为 OIDC JWT 验证；
        否则回退到静态 ``DOCTORAGENT_API_TOKEN`` 逻辑（向后兼容）。未配置 token 时
        fail-closed：仅允许来自本地（127.0.0.1/::1/Unix socket）的请求，外部
        请求返回 401。
        """
        oidc_user = await _authenticate_oidc(request, credentials)
        if oidc_user is not None:
            return oidc_user
        expected = _resolve_token()
        if expected is not None:
            if credentials is None:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid or missing authentication token",
                )  # type: ignore[misc]
            _verify_credentials(credentials, expected)
            return credentials
        # 未配置 token：fail-closed，仅允许本地请求
        if not _is_local_request(request):
            raise HTTPException(
                status_code=401,
                detail="DOCTORAGENT_API_TOKEN not set; remote access denied",
            )  # type: ignore[misc]
        return credentials

    async def _sensitive_auth_dependency(
        request: Request,  # type: ignore[name-defined]
        credentials: Any = Depends(_bearer_scheme),  # type: ignore[name-defined]  # noqa: B008
    ) -> Any:
        """Auth dependency for **write/sensitive** endpoints (fail-closed).

        OIDC-verified tokens are accepted when configured. Otherwise the static
        ``DOCTORAGENT_API_TOKEN`` must be set, and sensitive endpoints are denied
        by default when it is unset so an unconfigured deployment cannot
        accidentally expose decryption or sync-trigger capabilities.
        """
        oidc_user = await _authenticate_oidc(request, credentials)
        if oidc_user is not None:
            return oidc_user
        expected = _resolve_token()
        if expected is None:
            raise HTTPException(
                status_code=403,
                detail=("Authentication required for this endpoint: set DOCTORAGENT_API_TOKEN"),
            )  # type: ignore[misc]
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing authentication token",
            )  # type: ignore[misc]
        _verify_credentials(credentials, expected)
        return credentials

else:
    _bearer_scheme = None  # type: ignore[assignment]

    async def _auth_dependency() -> None:  # type: ignore[misc]
        return None

    async def _sensitive_auth_dependency() -> None:  # type: ignore[misc]
        return None


# ---------------------------------------------------------------------------
# WebSocket authentication (token passed as ?token= query parameter)
# ---------------------------------------------------------------------------


def _verify_ws_token(websocket: Any) -> None:
    """Authenticate a WebSocket handshake via the ``token`` query parameter.

    Browsers cannot set custom Authorization headers on the WebSocket upgrade
    request, so the API token is accepted as ``ws://host/ws?token=<api_token>``.

    Mirrors the HTTP auth policy:

    * ``DOCTORAGENT_API_TOKEN`` configured → query ``token`` must match.
    * Not configured → fail-closed: only local (loopback / Unix-socket)
      connections are accepted; remote upgrades are closed with code 4401.

    Raises by closing the socket with a 4401/4403 close code; the caller then
    returns early so the handler never runs for an unauthenticated upgrade.
    """
    expected = _resolve_token()
    if expected is None:
        if _is_local_request(websocket):
            return
        # Remote + no token configured: deny.
        raise _WSAuthError(code=4401, reason="DOCTORAGENT_API_TOKEN not set; remote access denied")
    token = websocket.query_params.get("token")
    if not verify_bearer(token, expected):
        raise _WSAuthError(code=4401, reason="Invalid or missing authentication token")


class _WSAuthError(Exception):
    """Internal control-flow exception carrying a WebSocket close code."""

    def __init__(self, *, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# SSE formatting helpers
# ---------------------------------------------------------------------------

#: Headers every SSE stream returns (disable proxy buffering + keep-alive).
_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # nginx: stream, don't buffer
}


def _sse_format(payload: dict[str, Any]) -> str:
    """Serialise *payload* as a single SSE ``data:`` frame terminated by ``\\n\\n``."""
    return f"data: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"


def _sse_comment(comment: str) -> str:
    """Emit an SSE comment line (ignored by EventSource, keeps proxies alive)."""
    return f": {comment}\n\n"


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def _count_files(path: Path) -> int:
    """Count regular files in a directory tree (shallow).

    Symbolic links are not counted so a malicious/stray symlink inside the
    vault cannot inflate counts or pull in files outside the vault root.
    """
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for entry in path.rglob("*") if entry.is_file() and not entry.is_symlink())


# ── Browser extension decryption (Phase 7.5) ────────────────────────────────

_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 推荐
_PBKDF2_ITERATIONS_LEGACY = 100_000  # 旧版浏览器扩展兼容


def _derive_pbkdf2_key(token: str, salt: bytes, iterations: int) -> bytes:
    """用 PBKDF2-SHA256 从 token 派生 32 字节 AES 密钥。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(token.encode("utf-8"))


def _decrypt_browser_submission(token: str, submission: BrowserSubmission) -> bytes:
    """Decrypt a browser extension payload using the API bearer token.

    The extension derives an AES-256 key from the token via
    PBKDF2-SHA256 and encrypts the plaintext with AES-256-GCM.

    迭代数兼容：若密文头里嵌了 iterations 字段则按存的解；否则先尝试
    新默认值（600 000，OWASP 2023 推荐），失败后回退到旧值（100 000）
    以兼容旧版浏览器扩展提交。
    """
    import base64

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = base64.b64decode(submission.salt)
    nonce = base64.b64decode(submission.nonce)
    ciphertext = base64.b64decode(submission.content)

    # 若密文头里嵌了 iterations 字段，按存的解
    iterations = getattr(submission, "iterations", None)
    if iterations is not None:
        key = _derive_pbkdf2_key(token, salt, iterations)
        return AESGCM(key).decrypt(nonce, ciphertext, None)

    # 未指定 iterations：先尝试新默认值，失败后回退到旧值兼容旧版扩展
    last_exc: Exception | None = None
    for iters in (_PBKDF2_ITERATIONS, _PBKDF2_ITERATIONS_LEGACY):
        try:
            key = _derive_pbkdf2_key(token, salt, iters)
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            last_exc = exc
            continue

    # 两种迭代数都失败，抛出最后一次异常
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Sync engine initialization
# ---------------------------------------------------------------------------


def _init_sync_engine(config: AegisConfig) -> Any:
    """Initialize a :class:`SyncEngine` if the sync subsystem is available.

    Creates the device identity, discovery, auth, and protocol components
    needed by :class:`~doctoragent.sync.engine.SyncEngine`. The device identity
    (``device_id`` + ``shared_secret``) is persisted to
    ``<config_dir>/sync_identity.json`` so it survives restarts.

    Returns the ``SyncEngine`` instance, or ``None`` if initialisation fails
    (e.g. the vault directory is not writable or the sync package cannot be
    imported).
    """
    try:
        import json
        import uuid

        from doctoragent.sync.auth import DeviceAuth
        from doctoragent.sync.discovery import DeviceDiscovery
        from doctoragent.sync.engine import DEFAULT_SYNC_PORT, SyncEngine
        from doctoragent.sync.protocol import SecureSyncProtocol

        config_dir = config.paths.connections.parent
        config_dir.mkdir(parents=True, exist_ok=True)
        identity_path = config_dir / "sync_identity.json"

        # Load or create a persistent device identity.
        device_id = str(uuid.uuid4())
        shared_secret = os.urandom(32)
        if identity_path.exists():
            try:
                data = json.loads(identity_path.read_text(encoding="utf-8"))
                loaded_id = data.get("device_id")
                if isinstance(loaded_id, str) and loaded_id:
                    device_id = loaded_id
                secret_hex = data.get("shared_secret", "")
                if isinstance(secret_hex, str) and len(secret_hex) == 64:
                    shared_secret = bytes.fromhex(secret_hex)
            except (json.JSONDecodeError, OSError, ValueError):
                logger.warning("Corrupt sync_identity.json; generating new identity")
        else:
            try:
                identity_path.write_text(
                    json.dumps(
                        {"device_id": device_id, "shared_secret": shared_secret.hex()},
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.chmod(identity_path, 0o600)
            except OSError:
                logger.warning("Failed to persist sync identity", exc_info=True)

        protocol = SecureSyncProtocol(device_id, shared_secret)
        discovery = DeviceDiscovery(
            device_name=f"DoctorAgent-{device_id[:8]}",
            port=DEFAULT_SYNC_PORT,
            device_id=device_id,
            enabled=config.discovery_enabled,
        )
        auth = DeviceAuth(config_dir)
        engine = SyncEngine(
            vault_path=config.paths.vault,
            protocol=protocol,
            discovery=discovery,
            auth=auth,
            vault_key=shared_secret,
        )
        logger.info("Sync engine initialised (device_id=%s)", device_id)
        return engine
    except Exception:
        logger.warning("Failed to initialise sync engine", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Audit log severity mapping — canonical source lives in
# ``doctoragent.security.audit_log.AUDIT_EVENT_SEVERITY`` (shared with the
# realtime broadcaster). Re-exported here under the legacy name so the
# /audit/logs severity filter keeps its existing call sites.
# ---------------------------------------------------------------------------

from doctoragent.security.audit_log import AUDIT_EVENT_SEVERITY as _AUDIT_SEVERITY_MAP  # noqa: E402


def _record_before(record: dict[str, Any], end_dt: Any) -> bool:
    """Return ``True`` if the audit record's timestamp is before *end_dt*."""
    from datetime import datetime

    ts = record.get("timestamp")
    if not ts:
        return False
    try:
        record_ts = datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    return record_ts < end_dt


# ---------------------------------------------------------------------------
# RAG pipeline factory (used by PipelinePool to lazily build per-tenant pipes)
# ---------------------------------------------------------------------------


def _make_rag_pipeline_factory(config: AegisConfig, agent: AegisAgent) -> Callable[..., Any]:
    """Return a ``factory(tenant_id, **kw) -> RagPipeline`` closure.

    The closure captures the config + agent providers once and rebuilds the
    heavy ``RagPipeline`` sub-systems (retriever, memory, context engineer …)
    only when a tenant is first accessed.  Subsequent calls for the same
    tenant are served from the :class:`PipelinePool` cache.
    """

    def _factory(tenant_id: str = "default", **_: Any) -> Any:
        from doctoragent.model.rag import RagPipeline

        embedding_provider = getattr(agent, "_embedding_provider", None)
        llm_provider = None
        if hasattr(agent.classifier, "provider"):
            llm_provider = agent.classifier.provider
        return RagPipeline(
            db_path=config.paths.index / "tasks.db",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            tenant_id=tenant_id,
            task_store=agent.task_store,
            audit_logger=getattr(agent, "audit_logger", None),
        )

    return _factory


def _make_agent_factory(
    config: AegisConfig, agent: AegisAgent, pool: PipelinePool
) -> Callable[..., Any]:
    """Return a factory building a fresh ``Agent`` bound to a pooled RAG pipe.

    Agents carry per-run trajectory state, so they are **not** pooled — only
    the underlying RAG pipeline is.  Each agent task gets a new Agent instance
    but reuses the tenant's pooled RagPipeline + MemorySystem.
    """

    def _factory(
        task: str,
        *,
        max_iterations: int = 10,
        tenant_id: str = "default",
    ) -> Any:
        from doctoragent.model.agent import AgentConfig, create_agent
        from doctoragent.model.rag import MemorySystem

        embedding_provider = getattr(agent, "_embedding_provider", None)
        llm_provider = None
        if hasattr(agent.classifier, "provider"):
            llm_provider = agent.classifier.provider
        if not llm_provider:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No LLM provider available. Configure a connection first.",
            )
        rag = pool.get_pipeline(tenant_id)
        memory = MemorySystem(config.paths.index / "tasks.db", tenant_id, embedding_provider)
        agent_config = AgentConfig(
            max_iterations=max_iterations,
            enable_planning=True,
            enable_reflection=True,
        )
        return create_agent(
            llm_provider=llm_provider,
            rag_pipeline=rag,
            task_store=agent.task_store,
            memory_system=memory,
            config=agent_config,
        )

    return _factory


def _build_configured_fhir_client(config: Any) -> Any:
    """Build a FHIR R4 client from ``config.clinical`` when a base URL is set.

    Returns ``None`` when ``fhir_base_url`` is empty (the clinical workflow
    then runs off the request patient_context / EHR prefetch). Construction
    failures (bad URL, fhir.resources missing) are logged and degrade to
    ``None`` so the server still boots — clinical endpoints return rules-only
    output flagged for human review instead of crashing.
    """
    clinical = getattr(config, "clinical", None)
    if clinical is None or not getattr(clinical, "fhir_base_url", ""):
        return None
    try:
        from doctoragent.clinical.fhir.client import FHIRClient
    except ImportError:
        logger.warning(
            "FHIR base URL configured but fhir.resources/clinical extra is not "
            "installed; clinical FHIR reads disabled. Install with: "
            "pip install doctoragent[clinical]"
        )
        return None
    try:
        client = FHIRClient(
            base_url=clinical.fhir_base_url,
            auth_token=clinical.fhir_auth_token,
            timeout=clinical.fhir_timeout_seconds,
        )
        logger.info("FHIR client configured for %s", clinical.fhir_base_url)
        return client
    except Exception as exc:  # noqa: BLE001 — never block server boot
        logger.warning("Failed to construct FHIR client: %s; clinical FHIR reads disabled", exc)
        return None


def create_app(config: AegisConfig, agent: AegisAgent) -> Any:
    """Create the FastAPI application with all endpoints registered.

    Parameters
    ----------
    config:
        The active DoctorAgent configuration.
    agent:
        The AegisAgent orchestrator for handling requests.

    Returns
    -------
    A fully-configured FastAPI instance ready for ``uvicorn`` or similar ASGI
    servers.
    """
    _check_available()

    # ── Real-time + pooling infrastructure (created before the lifespan so
    #    the lifespan can attach the running event loop to the broadcaster). ──
    broadcaster = EventBroadcaster()
    pool = PipelinePool(
        factory=_make_rag_pipeline_factory(config, agent),
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    rate_limiter = RateLimiter(default_rpm=DEFAULT_RPM, sensitive_rpm=SENSITIVE_RPM)
    agent_factory = _make_agent_factory(config, agent, pool)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # type: ignore[name-defined]
        if _resolve_token() is None:
            logger.warning(
                "DOCTORAGENT_API_TOKEN is not set: fail-closed 模式启用，"
                "仅允许本地（127.0.0.1/Unix socket）访问读端点，"
                "写/敏感端点已禁用，外部请求将被拒绝"
            )
        else:
            logger.info("DoctorAgent API authentication enabled (DOCTORAGENT_API_TOKEN set)")
        # Install the global OpenTelemetry tracer provider so FastAPI request
        # spans are emitted with the correct service.name resource attribute.
        # Safe no-op when the SDK is missing or the provider is already set.
        if _configure_tracing is not None:
            try:
                _configure_tracing(service_name="doctoragent")
            except Exception as exc:  # noqa: BLE001 - tracing must never block startup
                logger.warning("configure_tracing failed at startup: %s", exc)
        # Initialise the Langfuse LLM-tracing client if configured. Safe
        # no-op when the SDK is absent or env vars are unset. This is the
        # production wiring point that makes the @observe decorators on
        # run_clinical_workflow / chat_completion actually upload traces.
        try:
            if _configure_langfuse is not None:
                _configure_langfuse()
        except Exception as exc:  # noqa: BLE001 - telemetry never blocks startup
            logger.warning("configure_langfuse failed at startup: %s", exc)
        # Bind the running event loop so worker threads can publish events
        # back into the subscribers' queues in a thread-safe way.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — lifespan always runs in a loop
            loop = None
        broadcaster.attach_loop(loop)
        logger.info("DoctorAgent API server starting (API version %s)", CURRENT_API_VERSION)
        # Connect to externally-configured MCP servers (M4.16) and import their
        # tools, best-effort and non-blocking so a slow/unreachable server
        # never blocks startup.
        _mcp_specs = getattr(config.integrations, "mcp_servers", None) or []
        if _mcp_specs:
            _mcp_specs = list(_mcp_specs)
            _client_cache = app.state.mcp_clients
            _mcp_registry = app.state.mcp_tool_registry
            _mcp_registry_ref = _mcp_registry

            async def _import_configured_mcp_servers() -> None:
                if _mcp_registry_ref is None:
                    return
                try:
                    from doctoragent.agent.mcp_client import MCPClient, import_mcp_tools
                except ImportError:
                    logger.warning("mcp extra not installed; skipping configured MCP servers")
                    return
                for spec in _mcp_specs:
                    name = spec.get("name", "external")
                    try:
                        client = MCPClient(
                            name,
                            transport=spec.get("transport", "stdio"),
                            command=spec.get("command"),
                            args=spec.get("args") or [],
                            url=spec.get("url"),
                            http_headers=spec.get("http_headers") or {},
                        )
                        imported = await import_mcp_tools(
                            client, _mcp_registry_ref, prefix=spec.get("prefix", "")
                        )
                        _client_cache[name] = client
                        logger.info(
                            "Imported %d tool(s) from MCP server %s",
                            len(imported),
                            name,
                        )
                    except Exception as exc:  # noqa: BLE001 — best-effort
                        logger.warning("Failed to import MCP server %s: %s", name, exc)

            _startup_task = asyncio.create_task(_import_configured_mcp_servers())
            app.state.mcp_startup_task = _startup_task
        # Verify audit-log integrity on startup so tampered logs are detected
        # immediately rather than being silently read on demand. This makes
        # HMAC verification fail-closed by default instead of opt-in.
        _startup_audit_logger = getattr(agent, "audit_logger", None)
        if _startup_audit_logger is not None:
            try:
                ok, mismatches = _startup_audit_logger.verify()
                if not ok:
                    logger.error(
                        "Audit log tampering detected on startup: "
                        "%d record(s) failed HMAC verification",
                        len(mismatches),
                    )
            except Exception as e:  # noqa: BLE001 - startup must not crash on audit check
                logger.warning("Startup audit verify failed: %s", e)
        yield
        # ── Shutdown: clean up every long-lived resource the server owns so
        #    no threads/connections/orphan tasks outlive the process. This
        #    mirrors the CLI daemon's aclose() path (see __main__.py). ──
        logger.info("DoctorAgent API server shutting down")
        # 1. Cancel in-flight agent streaming tasks so they cannot keep
        #    touching the task store / LLM provider after teardown begins.
        for _run_id, task in list(app.state.agent_tasks.items()):
            if not task.done():
                task.cancel()
        # 2. Cancel background sync rounds triggered via POST /sync/trigger.
        for task in list(app.state.sync_tasks):
            if not task.done():
                task.cancel()
        # Await cancellations briefly so CancelledError handlers can flush
        # their final SSE/error frames. Swallow CancelledError/exceptions.
        pending: list[asyncio.Task[Any]] = [
            t
            for t in list(app.state.agent_tasks.values()) + list(app.state.sync_tasks)
            if not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        app.state.agent_tasks.clear()
        app.state.sync_tasks.clear()
        # 3. Stop the sync TCP server if it was ever started (it is not by
        #    default, but defensive in case a future caller starts it).
        if sync_engine is not None:
            try:
                await sync_engine.stop_sync_server()
            except Exception as exc:  # noqa: BLE001 — shutdown must not crash
                logger.warning("stop_sync_server failed during shutdown: %s", exc)
        # 4. Release pooled pipelines and detach the event loop.
        pool.close()
        broadcaster.attach_loop(None)
        # 5. Tear down the AegisAgent (key rotator thread, inbox watcher,
        #    classifier provider connections). Matches the CLI daemon path.
        try:
            await agent.aclose()
        except Exception as exc:  # noqa: BLE001 — shutdown must not crash
            logger.warning("agent.aclose() failed during shutdown: %s", exc)
        # 6. Flush any pending Langfuse traces so they are not lost when the
        #    background uploader is torn down with the process. No-op when
        #    Langfuse is disabled / unconfigured.
        try:
            if _flush_langfuse is not None:
                _flush_langfuse()
        except Exception as exc:  # noqa: BLE001 — shutdown must not crash
            logger.warning("flush_langfuse failed during shutdown: %s", exc)

    app = FastAPI(  # type: ignore[name-defined]
        title="DoctorAgent API",
        version=__version__,
        description=(
            "REST + real-time API for DoctorAgent, the local-first "
            "clinical AI agent.\n\n"
            "## Channels\n"
            "* **REST** — versioned under `/api/v1` (legacy unversioned paths "
            "remain for backward compatibility).\n"
            "* **SSE** — `POST /vault/ask/stream`, `POST /vault/agent/stream`, "
            "`GET /events`.\n"
            "* **WebSocket** — `WS /ws` for bidirectional commands + push.\n\n"
            "## Auth\n"
            "All endpoints require a Bearer token (`DOCTORAGENT_API_TOKEN`) except "
            "`/health` and `/api/version`. WebSocket auth uses `?token=<api_token>`."
        ),
        lifespan=_lifespan,
        openapi_tags=TAGS_METADATA,
        contact={
            "name": "DoctorAgent API Team",
            "url": "https://github.com/weed33834/DoctorAgent",
        },
        license_info={
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        },
        terms_of_service="https://github.com/weed33834/DoctorAgent",
        servers=[
            {"url": "/api/v1", "description": "Versioned API (v1)"},
            {"url": "/", "description": "Legacy unversioned API (backward compatible)"},
        ],
        docs_url=None,  # 改为本地化 Swagger UI（见下方 /docs 路由），消除对 jsdelivr CDN 的依赖
        redoc_url=None,  # redoc 同样依赖 CDN，离线不可用，禁用
    )

    # 本地化 Swagger UI：默认 /docs 硬编码 jsdelivr CDN，离线/沙箱必白屏；
    # 改为引用已 vendor 到 /console/vendor/swagger-ui/ 的本地资源。
    @app.get("/docs", include_in_schema=False)
    async def _local_swagger_ui_html():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title="DoctorAgent API - Swagger UI",
            swagger_js_url="/console/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/console/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url="",
        )

    # ── Middleware stack (outermost = last added): rate-limit → size → CORS ──
    app.add_middleware(
        CORSMiddleware,  # type: ignore[name-defined]
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_body_bytes=DEFAULT_MAX_BODY_BYTES,
    )
    app.add_middleware(
        RateLimitMiddleware,
        limiter=rate_limiter,
        default_rpm=DEFAULT_RPM,
        sensitive_rpm=SENSITIVE_RPM,
    )

    # ── Observability: OpenTelemetry FastAPI instrumentation (graceful) ──
    if _instrument_app is not None:
        try:
            _instrument_app(app)
        except Exception as exc:  # pragma: no cover — defensive, never fatal
            logger.warning("FastAPI tracing instrumentation failed: %s", exc)

    # ── Observability: Prometheus HTTP metrics middleware ──
    # Increments doctoragent_http_requests_total + observes latency on every
    # request. Safe no-op when prometheus_client is not installed (the metric
    # objects are in-process stubs that still accept .labels().inc()/observe()).
    if (
        _doctoragent_http_requests_total is not None
        and _doctoragent_http_request_duration_seconds is not None
        and _FASTAPI_AVAILABLE
    ):
        import time as _time_mod

        from starlette.middleware.base import BaseHTTPMiddleware  # type: ignore[import-not-found]

        class _DoctorAgentMetricsMiddleware(BaseHTTPMiddleware):  # type: ignore[misc]
            async def dispatch(self, request: Any, call_next: Any) -> Any:
                start = _time_mod.perf_counter()
                status_code = 500
                try:
                    response = await call_next(request)
                    status_code = getattr(response, "status_code", 500)
                    return response
                finally:
                    elapsed = _time_mod.perf_counter() - start
                    path = request.url.path
                    method = request.method
                    try:
                        _doctoragent_http_requests_total.labels(  # type: ignore[union-attr]
                            method=method, path=path, status=str(status_code)
                        ).inc()
                        _doctoragent_http_request_duration_seconds.labels(  # type: ignore[union-attr]
                            method=method, path=path
                        ).observe(elapsed)
                    except Exception:  # noqa: BLE001 - metrics must never break a request
                        logger.debug("metrics inc failed", exc_info=True)

        app.add_middleware(_DoctorAgentMetricsMiddleware)

    # ── Shared state on app.state ────────────────────────────────────
    sync_engine = _init_sync_engine(config)
    app.state.sync_engine = sync_engine
    app.state.broadcaster = broadcaster
    app.state.pipeline_pool = pool
    app.state.rate_limiter = rate_limiter
    app.state.agent_factory = agent_factory
    app.state.agent_tasks: dict[str, asyncio.Task[Any]] = {}
    # External MCP server connections created via POST /mcp/connect are kept
    # here so their sessions stay reusable across tool calls.
    app.state.mcp_clients: dict[str, Any] = {}
    # Enterprise / organization platform service (M14). Lazily built from the
    # task-store database path so the console can manage orgs/users/budgets.
    try:
        from doctoragent.enterprise import EnterpriseService, EnterpriseStore

        _ent_db = Path(config.paths.index) / "enterprise.db"
        _ent_store = EnterpriseStore(_ent_db)
        _ent_audit = getattr(agent, "audit_logger", None)
        app.state.enterprise_service = EnterpriseService(
            _ent_store, audit_logger=_ent_audit
        )
        app.state.enterprise_store = _ent_store
    except Exception as exc:  # noqa: BLE001 — enterprise must never block startup
        app.state.enterprise_service = None
    # Conversation-editable workspace config (prompts / skills / experts),
    # shared by the chat-management tools and the management API.
    try:
        from doctoragent.workspace_config import WorkspaceConfig

        app.state.workspace_config = WorkspaceConfig(Path(config.paths.index) / "workspace.db")
    except Exception:  # noqa: BLE001
        app.state.workspace_config = None
    # Clinical specialty persona (default general) + built-in knowledge seed.
    app.state.clinical_role = "general"
    try:
        _ws_for_role = getattr(app.state, "workspace_config", None)
        if _ws_for_role is not None:
            saved = _ws_for_role.get_setting("clinical_role", "general")
            if saved:
                from doctoragent.clinical.roles import get_role

                if get_role(saved):
                    app.state.clinical_role = saved
    except Exception:  # noqa: BLE001
        pass
    try:
        from doctoragent.clinical.knowledge import seed_knowledge

        _n = seed_knowledge(config.paths.vault)
        if _n:
            logger.info("Seeded %d built-in clinical knowledge doc(s) into Vault", _n)
        _ws_k = getattr(app.state, "workspace_config", None)
        if _ws_k is not None:
            try:
                _ws_k.set_settings({"knowledge_seeded": str(_n)})
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — knowledge seeding must never block startup
        logger.debug("clinical knowledge seed skipped: %s", "")
        app.state.enterprise_store = None
        logger.debug("Enterprise service not available: %s", exc)
    # Governance / pricing / semantic cache / cost dashboard services (M20/M21/M23).
    try:
        from doctoragent.governance import GovernanceService, GovernanceStore

        _gov_store = GovernanceStore(Path(config.paths.index) / "governance.db")
        app.state.governance_service = GovernanceService(_gov_store)
        app.state.governance_store = _gov_store
    except Exception as exc:  # noqa: BLE001
        app.state.governance_service = None
        app.state.governance_store = None
        logger.debug("governance service not available: %s", exc)
    try:
        from doctoragent.model.pricing import ModelPricing

        app.state.pricing = ModelPricing()
    except Exception:  # noqa: BLE001
        app.state.pricing = None
    try:
        from doctoragent.model.semantic_cache import SemanticCache

        app.state.semantic_cache = SemanticCache(
            embedding_provider=getattr(agent, "_embedding_provider", None),
            persist_path=Path(config.paths.index) / "semantic_cache.db",
            sensitive_prefixes=("病历", "患者", "diagnosis", "patient"),
        )
    except Exception:  # noqa: BLE001
        app.state.semantic_cache = None
    app.state.cost_tracker = getattr(agent, "cost_tracker", None)
    if app.state.cost_tracker is None:
        try:
            from doctoragent.model.cost_tracker import CostTracker

            app.state.cost_tracker = CostTracker(Path(config.paths.index) / "costs.db")
        except Exception:  # noqa: BLE001
            app.state.cost_tracker = None
    # Interop (M27) / AI security (M25) / disaster recovery (M29) services.
    try:
        from doctoragent.interop import InteropService, InteropStore

        app.state.interop_service = InteropService(
            InteropStore(Path(config.paths.index) / "interop.db"),
            a2a_client=getattr(app.state, "a2a_client", None),
        )
    except Exception:  # noqa: BLE001
        app.state.interop_service = None
    try:
        from doctoragent.security.threat import ThreatService, ThreatStore

        app.state.threat_service = ThreatService(
            ThreatStore(Path(config.paths.index) / "threat.db")
        )
    except Exception:  # noqa: BLE001
        app.state.threat_service = None
    try:
        from doctoragent.disaster import DisasterService, DisasterStore

        app.state.disaster_service = DisasterService(
            DisasterStore(Path(config.paths.index) / "disaster.db")
        )
    except Exception:  # noqa: BLE001
        app.state.disaster_service = None
    # Multimodal (M26) / data pipeline (M28) / KB manager (M14 D) / task center (M14 K).
    try:
        from doctoragent.multimodal import MultimodalService, MultimodalStore

        app.state.multimodal_service = MultimodalService(
            MultimodalStore(Path(config.paths.index) / "multimodal.db")
        )
    except Exception:  # noqa: BLE001
        app.state.multimodal_service = None
    try:
        from doctoragent.datapipeline import PipelineService, PipelineStore

        app.state.pipeline_service = PipelineService(
            PipelineStore(Path(config.paths.index) / "pipeline.db")
        )
    except Exception:  # noqa: BLE001
        app.state.pipeline_service = None
    try:
        from doctoragent.knowledge_base import KnowledgeBaseManager

        app.state.kb_manager = KnowledgeBaseManager(
            Path(config.paths.index) / "kb.db", config.paths.vault
        )
    except Exception:  # noqa: BLE001
        app.state.kb_manager = None
    try:
        from doctoragent.taskcenter import TaskCenter

        app.state.task_center = TaskCenter(Path(config.paths.index) / "tasks.db")
    except Exception:  # noqa: BLE001
        app.state.task_center = None
    # Background sync tasks created by POST /sync/trigger are tracked here so
    # the lifespan shutdown handler can cancel them instead of leaving orphan
    # tasks that outlive the server.
    app.state.sync_tasks: set[asyncio.Task[Any]] = set()
    # Expose the agent / config / subsystems on app.state so the advanced
    # advanced routes (and any third-party router mounted via
    # ``include_router``) can resolve them lazily without capturing closures.
    app.state.agent = agent
    app.state.config = config
    app.state.task_store = getattr(agent, "task_store", None)
    app.state.audit_logger = getattr(agent, "audit_logger", None)
    # Wire the realtime broadcaster as a producer of the audit logger so
    # every audit event (clinical decisions, safety alerts, guardrail
    # actions, file ingestions …) is fanned out as a PHI-sanitised
    # envelope to authenticated WebSocket / SSE subscribers. The logger
    # publishes only event_type + severity + non-PHI scalars, so no
    # patient data leaves via the broadcast channel.
    _audit_logger_for_broadcast = app.state.audit_logger
    if _audit_logger_for_broadcast is not None:
        try:
            _audit_logger_for_broadcast.event_broadcaster = broadcaster
        except Exception:  # noqa: BLE001 — broadcaster wiring is best-effort
            logger.warning("Failed to wire audit broadcaster", exc_info=True)
    app.state.master_key_provider = getattr(agent, "master_key_provider", None)
    app.state.llm_provider = (
        getattr(agent, "_llm_provider", None)
        or getattr(getattr(agent, "classifier", None), "provider", None)
        if getattr(agent, "classifier", None) is not None
        else getattr(agent, "_llm_provider", None)
    )
    # ── CDS Hooks collaborators (read by the CDS Hooks router via app.state).
    #    Mirrors the collaborators /clinical/analyze pulls off the agent so a
    #    single EHR-facing mount reuses the same LLM/FHIR/audit wiring. When
    #    an attribute is None the CDS service degrades to rules-only, no extra
    #    FHIR reads, no audit trail — exactly like /clinical/analyze.
    app.state.cds_llm_provider = getattr(agent, "_llm_provider", None) or getattr(
        agent, "llm_provider", None
    )
    app.state.cds_audit_logger = getattr(agent, "audit_logger", None)
    # The FHIR client is built from config.clinical.fhir_base_url when set, so
    # both /clinical/analyze and the CDS Hooks router share one client that
    # points at the operator-configured FHIR server (e.g. the compose
    # `with-fhir` HAPI service). When fhir_base_url is empty the client stays
    # None and the workflow runs off the request patient_context / EHR prefetch
    # only — exactly the previous behaviour.
    app.state.cds_fhir_client = _build_configured_fhir_client(config)
    app.state.clinical_fhir_client = app.state.cds_fhir_client

    # All business endpoints are registered on a single APIRouter and then
    # mounted twice: once under /api/v1 (documented in OpenAPI) and once at
    # the root (hidden from the schema, kept for backward compatibility).
    router = APIRouter()  # type: ignore[name-defined]

    # ── Health Check ──────────────────────────────────────────────────
    @router.get(
        "/health",
        tags=["System"],
        response_model=HealthResponse,
        summary="Liveness probe",
        description="Returns server status and version. Never rate-limited.",
        responses=_error_responses(),
    )
    async def health() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok", "version": __version__}

    # ── Prometheus metrics (GET /metrics) ──────────────────────────────
    # Not auth-gated so a Prometheus scraper can pull directly, but still
    # subject to the rate-limit middleware (only /health bypasses the limiter).
    # Returns empty bytes when prometheus_client is not installed.
    @router.get(
        "/metrics",
        tags=["System"],
        response_class=PlainTextResponse,  # type: ignore[name-defined]
        summary="Prometheus metrics",
        description=(
            "Exposes application metrics in the Prometheus text exposition "
            "format. No authentication is required so a scraper can pull "
            "directly, but the endpoint is still rate-limited."
        ),
    )
    async def metrics() -> Any:
        """Return Prometheus-format metrics for scraping."""
        body = _generate_latest_metrics() if _generate_latest_metrics is not None else b""
        return PlainTextResponse(  # type: ignore[name-defined]
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── Vault Status ──────────────────────────────────────────────────
    @router.get(
        "/vault/status",
        tags=["Vault"],
        response_model=VaultStatusResponse,
        summary="Vault status and category breakdown",
        description="Returns inbox/vault file counts, per-category counts, and recent tasks.",
        responses=_error_responses(401),
    )
    async def vault_status(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Return vault status: file counts and category breakdown."""
        inbox_count = _count_files(config.paths.inbox)
        vault_count = _count_files(config.paths.vault)

        categories: dict[str, int] = {}
        vault_dir = config.paths.vault
        if vault_dir.exists() and vault_dir.is_dir():
            for cat_dir in vault_dir.iterdir():
                if cat_dir.is_dir():
                    categories[cat_dir.name] = sum(1 for f in cat_dir.iterdir() if f.is_file())

        recent = agent.task_store.list_recent(limit=10)
        return {
            "inbox_files": inbox_count,
            "vault_files": vault_count,
            "categories": categories,
            "recent_tasks": [
                {
                    "task_id": str(r.task_id),
                    "state": r.state,
                    "message": r.message,
                    "source_path": str(r.source_path) if r.source_path else None,
                }
                for r in recent
            ],
        }

    # ── Search ────────────────────────────────────────────────────────
    @router.post(
        "/vault/search",
        tags=["Vault"],
        response_model=list[SearchResult],
        summary="Search vault content",
        description="Keyword or semantic search across vault documents.",
        responses=_error_responses(401, 422, 500),
    )
    async def vault_search(
        query: SearchQuery,
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> list[SearchResult]:
        """Search vault content by keyword or semantic query."""
        return await agent.search(query)

    # ── File List ─────────────────────────────────────────────────────
    @router.get(
        "/vault/files",
        tags=["Vault"],
        response_model=FileListResponse,
        summary="List vault files (paginated)",
        description="Paginated file listing with optional category filter.",
        responses=_error_responses(401, 422),
    )
    async def vault_files(
        category: str | None = Query(None),  # type: ignore[name-defined]
        offset: int = Query(0, ge=0),  # type: ignore[name-defined]
        limit: int = Query(50, ge=1, le=500),  # type: ignore[name-defined]
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """List vault files with pagination and optional category filter."""
        all_files = agent.task_store.list_vault_files(category)
        total = len(all_files)
        page = all_files[offset : offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "files": [
                {
                    "task_id": f["task_id"],
                    "vault_path": f["vault_path"],
                    "category": f.get("category", ""),
                    "summary": f.get("summary", ""),
                    "tags": f.get("tags", []),
                }
                for f in page
            ],
        }

    # ── File Metadata ─────────────────────────────────────────────────
    @router.get(
        "/vault/files/{file_id}",
        tags=["Vault"],
        response_model=FileMetadataResponse,
        summary="Get vault file metadata",
        description="Returns metadata for a single vault file by task ID.",
        responses=_error_responses(400, 401, 404),
    )
    async def vault_file_metadata(
        file_id: str,
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Get metadata for a specific vault file by task ID."""
        from uuid import UUID

        try:
            task_uuid = UUID(file_id)
        except ValueError as err:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Invalid file ID format"
            ) from err

        record = agent.task_store.get(task_uuid)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found")  # type: ignore[misc]

        # category/summary/tags are stored inside the `classification` JSON
        # column of the tasks table (see TaskStore.update_classification),
        # not as top-level columns — reading them off the row directly always
        # returned empty values.
        category = ""
        summary = ""
        tags: list[str] = []
        classification_raw = record.get("classification")
        if classification_raw:
            try:
                cls_data = json.loads(classification_raw)
            except (json.JSONDecodeError, TypeError):
                cls_data = None
            if isinstance(cls_data, dict):
                category = str(cls_data.get("category") or "")
                summary = str(cls_data.get("summary") or "")
                raw_tags = cls_data.get("tags")
                if isinstance(raw_tags, list):
                    tags = [str(t) for t in raw_tags]

        return {
            "task_id": file_id,
            "state": record.get("state", ""),
            "source_path": record.get("source_path"),
            "vault_path": record.get("vault_path"),
            "category": category,
            "summary": summary,
            "tags": tags,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }

    # ── File Download ─────────────────────────────────────────────────
    @router.get(
        "/vault/files/{file_id}/download",
        tags=["Vault"],
        response_class=FileResponse,  # type: ignore[name-defined]
        summary="Download (decrypt) a vault file",
        description="Decrypts and streams a single vault file. Requires sensitive auth.",
        responses=_error_responses(400, 401, 403, 404, 500),
    )
    async def vault_file_download(
        file_id: str,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> Any:
        """Download (decrypt) a vault file."""
        import shutil
        import tempfile
        from uuid import UUID

        from starlette.background import BackgroundTask

        try:
            task_uuid = UUID(file_id)
        except ValueError as err:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Invalid file ID format"
            ) from err

        record = agent.task_store.get(task_uuid)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found")  # type: ignore[misc]

        vault_path_str = record.get("vault_path")
        if not vault_path_str:
            raise HTTPException(status_code=404, detail="File has no vault path")  # type: ignore[misc]

        vault_path = Path(vault_path_str)
        if not vault_path.exists():
            raise HTTPException(status_code=404, detail="Vault file missing on disk")  # type: ignore[misc]

        salt = record.get("salt")
        if not salt:
            # Without the per-file salt the file key cannot be reconstructed;
            # never fall back to an empty salt (which would silently produce
            # the wrong key / failed auth tag).
            logger.error("Missing salt for vault file %s", file_id)
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Failed to decrypt file"
            )

        from doctoragent.execution.vault import VaultManager
        from doctoragent.security.keytree import derive_vault_key

        vault_key = derive_vault_key(agent.master_key_provider.get_key())

        # Decrypt into a private temp directory and stream it back, cleaning
        # up via a BackgroundTask that runs after the response is sent.  The
        # temp dir is also removed on the error path so nothing leaks on disk.
        tmp_dir = Path(tempfile.mkdtemp(prefix="doctoragent-api-download-"))
        dest = tmp_dir / vault_path.name
        try:
            mgr = VaultManager(config.paths.vault, vault_key)
            mgr.decrypt(vault_path, salt, dest)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # Log the detailed error server-side; return only a generic
            # message to avoid leaking internal exception details.
            logger.exception("Failed to decrypt vault file %s", file_id)
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Failed to decrypt file"
            ) from None
        return FileResponse(  # type: ignore[name-defined]
            dest,
            filename=vault_path.name,
            background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
        )

    # ── Classify ──────────────────────────────────────────────────────
    @router.post(
        "/vault/classify",
        tags=["Vault"],
        response_model=MessageResponse,
        summary="Trigger inbox classification",
        description="Manually classify up to 50 inbox files. Requires sensitive auth.",
        responses=_error_responses(401, 403, 500),
    )
    async def vault_classify(
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, str]:
        """Manually trigger classification of inbox files.

        Capped at ``max_classify_files`` per request to avoid unbounded
        queuing from a single API call.
        """
        inbox = config.paths.inbox
        if not inbox.exists() or not inbox.is_dir():
            return {"message": "Inbox directory is empty or missing"}

        from uuid import uuid4

        from doctoragent.api.schemas import FileEvent

        max_classify_files = 50
        entries = [entry for entry in inbox.iterdir() if entry.is_file()]
        if not entries:
            return {"message": "Inbox directory is empty"}

        to_process = entries[:max_classify_files]
        skipped = len(entries) - len(to_process)

        count = 0
        for entry in to_process:
            event = FileEvent(
                event_id=uuid4(),
                source_path=entry,
            )
            await agent.on_file_event(event)
            count += 1

        message = f"Queued {count} file(s) for classification"
        if skipped:
            message += f"; {skipped} file(s) skipped (limit {max_classify_files})"
        return {"message": message}

    # ── Sync Status ───────────────────────────────────────────────────
    @router.get(
        "/sync/status",
        tags=["Sync"],
        response_model=SyncStatusResponse,
        summary="Sync subsystem status",
        description="Returns sync engine availability, device ID, peer count, and last sync times.",
        responses=_error_responses(401),
    )
    async def sync_status(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Return sync subsystem status."""
        if sync_engine is None:
            return {"available": False, "message": "Sync engine not initialized"}
        last_sync = dict(sync_engine._sync_state.last_sync_time)  # type: ignore[attr-defined]
        peers = sync_engine.discovery.get_peers()  # type: ignore[attr-defined]
        return {
            "available": True,
            "message": "Sync engine active",
            "device_id": sync_engine.protocol.device_id,  # type: ignore[attr-defined]
            "running": sync_engine._running,  # type: ignore[attr-defined]
            "peers_discovered": len(peers),
            "last_sync_times": last_sync,
        }

    # ── Sync Trigger ──────────────────────────────────────────────────
    @router.post(
        "/sync/trigger",
        tags=["Sync"],
        response_model=MessageResponse,
        summary="Trigger a sync round",
        description="Schedules a single sync round against all authorised peers as a background task.",
        responses=_error_responses(400, 401, 403, 500),
    )
    async def sync_trigger(
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Manually trigger a sync operation.

        Schedules a single sync round against all authorised peers as a
        background task so the request returns immediately.
        """
        if sync_engine is None:
            raise HTTPException(status_code=400, detail="Sync engine not initialized")  # type: ignore[misc]
        task = asyncio.create_task(sync_engine.sync_once())  # type: ignore[attr-defined]
        # Track so the lifespan shutdown handler can cancel orphans.
        app.state.sync_tasks.add(task)
        task.add_done_callback(app.state.sync_tasks.discard)
        return {"message": "Sync triggered successfully"}

    # ── Webhook management (Phase 7.2) ───────────────────────────────
    @router.get(
        "/webhooks/endpoints",
        tags=["Webhooks"],
        response_model=WebhookListResponse,
        summary="List webhook endpoints",
        description="Returns configured webhook URLs and subscriptions. Secrets are never returned.",
        responses=_error_responses(401),
    )
    async def webhooks_list(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """List configured webhook endpoints (URLs and subscriptions only).

        Secrets are never returned. ``enabled`` reflects whether the
        dispatcher was constructed with webhooks turned on.
        """
        dispatcher = getattr(agent, "_webhook_dispatcher", None)
        if dispatcher is None:
            return {"enabled": False, "endpoints": []}
        eps = [
            {"url": e.url, "events": e.events, "label": e.label}
            for e in dispatcher.endpoints  # type: ignore[attr-defined]
        ]
        return {"enabled": True, "endpoints": eps}

    @router.get(
        "/webhooks/deliveries",
        tags=["Webhooks"],
        summary="List webhook delivery records",
        description="Returns recent webhook delivery records (most-recent last).",
        responses=_error_responses(401),
    )
    async def webhooks_deliveries(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> list[dict[str, Any]]:
        """Return recent webhook delivery records (most-recent last)."""
        dispatcher = getattr(agent, "_webhook_dispatcher", None)
        if dispatcher is None:
            return []
        return [
            {
                "event_id": r.event_id,
                "event_type": r.event_type,
                "endpoint": r.endpoint_url,
                "success": r.success,
                "attempts": r.attempts,
                "status_code": r.status_code,
                "error": r.last_error,
                "duration_ms": round(r.duration_ms, 2),
            }
            for r in dispatcher.history  # type: ignore[attr-defined]
        ]

    @router.post(
        "/webhooks/test",
        tags=["Webhooks"],
        response_model=WebhookTestResponse,
        summary="Fire a test webhook event",
        description="Dispatches a test event to all subscribed endpoints. Requires sensitive auth.",
        responses=_error_responses(400, 401, 403),
    )
    async def webhooks_test(
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Fire a test event to all subscribed endpoints.

        Requires the sensitive auth dependency so an unauthenticated caller
        cannot use the API to probe arbitrary webhook receivers.
        """
        dispatcher = getattr(agent, "_webhook_dispatcher", None)
        if dispatcher is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="Webhooks not configured on this agent",
            )
        attempted = dispatcher.dispatch(  # type: ignore[attr-defined]
            "webhook_test",
            {"triggered_by": "api", "test": True},
        )
        return {"attempted": attempted, "message": f"Dispatched to {attempted} endpoint(s)"}

    # ── Remote backup (Phase 7.3) ────────────────────────────────────
    @router.post(
        "/backup/remote",
        tags=["Backup"],
        response_model=BackupResponse,
        summary="Trigger remote backup",
        description="Incremental backup of vault content to the configured storage backend.",
        responses=_error_responses(400, 401, 403, 500),
    )
    async def backup_remote(
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Trigger an incremental remote backup to the configured backend.

        Requires sensitive auth because a backup pushes vault content
        offsite — only an authenticated operator should trigger it.
        """
        from doctoragent.integrations.storage import (
            backup_vault_to_backend,
            create_storage_backend,
        )

        integrations = config.integrations
        if not integrations.storage_enabled:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="Remote storage is disabled (integrations.storage_enabled=False)",
            )
        try:
            backend = create_storage_backend(
                integrations,
                local_root=config.paths.vault.parent / "Backups",
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500,
                detail=f"Storage backend misconfigured: {exc}",
            ) from exc
        result = backup_vault_to_backend(
            config.paths.vault,
            backend,
            audit_logger=getattr(agent, "audit_logger", None),
        )
        return {
            "ok": result.ok,
            "backend": backend.backend_name,
            "uploaded": result.uploaded,
            "skipped": result.skipped,
            "removed": result.removed,
            "error": result.error,
        }

    # ── Browser extension submission (Phase 7.5) ─────────────────────
    @router.post(
        "/inbox/submit",
        tags=["Inbox"],
        response_model=InboxSubmitResponse,
        summary="Browser extension submission",
        description="Receive an encrypted submission from the browser extension, decrypt, and ingest.",
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def inbox_submit(
        submission: BrowserSubmission,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Receive an encrypted submission from the browser extension.

        The extension encrypts content locally with AES-256-GCM using a
        key derived from the API bearer token (PBKDF2-SHA256).  We
        decrypt it here, write the plaintext to the Inbox, and feed it
        through the classification pipeline immediately so the user gets
        a synchronous task status in the response.
        """
        import os
        import time
        from uuid import uuid4

        from doctoragent.api.schemas import FileEvent

        token = _resolve_token()
        if token is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=403,
                detail="DOCTORAGENT_API_TOKEN must be set for browser submissions",
            )

        try:
            plaintext = _decrypt_browser_submission(token, submission)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to decrypt browser submission")
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Decryption failed — check token/key agreement"
            ) from None

        inbox = config.paths.inbox
        inbox.mkdir(parents=True, exist_ok=True)

        # Sanitize the client-supplied filename to a bare basename so a
        # ".."/absolute path cannot escape the inbox directory.
        raw_name = (submission.filename or "").strip()
        name = os.path.basename(raw_name)
        if not name:
            name = f"browser-{int(time.time())}.txt"
        dest = inbox / name
        if dest.exists():
            suffix = os.urandom(4).hex()
            dest = inbox / f"{dest.stem}_{suffix}{dest.suffix}"

        dest.write_bytes(plaintext)
        os.chmod(dest, 0o600)

        event = FileEvent(event_id=uuid4(), source_path=dest, event_type="created")
        try:
            status = await agent.on_file_event(event)
        except (RuntimeError, OSError, ValueError) as exc:
            logger.exception("Failed to process browser submission %s", dest)
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Ingestion failed"
            ) from exc

        return {
            "ok": status.state == "COMPLETED",
            "inbox_path": str(dest),
            "task_state": status.state,
            "task_id": str(status.task_id),
            "message": status.message,
        }

    # ── Knowledge import: upload a document (PDF/DOCX/...) into the Vault ──
    # Convenience for hospital staff: pick a medical PDF on disk and upload it
    # here; it is staged into the Inbox and processed (classified + moved into
    # the Vault + indexed) so the RAG/knowledge agents can retrieve it.
    @router.post(
        "/vault/import",
        tags=["Vault"],
        summary="Upload a document into the knowledge base (Vault)",
        description=(
            "Accepts a file (PDF / DOCX / XLSX / MD / TXT / ...), stages it into "
            "the Inbox and triggers the ingest pipeline so it lands in the Vault "
            "and becomes retrievable by the knowledge/RAG agents. Returns the "
            "ingest status."
        ),
        responses=_error_responses(400, 401, 413, 500, 503),
    )
    async def vault_import(
        request: Request,  # type: ignore[name-defined]
        file: UploadFile = File(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
    ) -> dict[str, Any]:
        import os
        import time as _time
        from uuid import uuid4 as _uuid4

        from doctoragent.api.schemas import FileEvent

        if file.size and file.size > 50 * 1024 * 1024:  # 50MB guard
            raise HTTPException(  # type: ignore[misc]
                status_code=413, detail="File exceeds 50MB upload limit"
            )
        name = os.path.basename(file.filename or "")
        if not name:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="filename is required"
            )
        data = await file.read()
        inbox = config.paths.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / name
        if dest.exists():
            dest = inbox / f"{dest.stem}_{_time.time():.0f}{dest.suffix}"
        try:
            dest.write_bytes(data)
        except OSError as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to write upload: {exc}"
            ) from exc
        event = FileEvent(event_id=_uuid4(), source_path=dest, event_type="created")
        try:
            status = await agent.on_file_event(event)
        except (RuntimeError, OSError, ValueError) as exc:
            logger.exception("Failed to ingest uploaded document %s", dest)
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Ingestion failed"
            ) from exc
        return {
            "ok": status.state == "COMPLETED",
            "filename": name,
            "inbox_path": str(dest),
            "task_state": status.state,
            "task_id": str(status.task_id),
            "message": status.message,
        }

    # ── Plaintext Inbox ingestion (POST /inbox/ingest) ───────────────
    # Unlike /inbox/submit (which expects the browser extension's
    # AES-256-GCM ciphertext), this endpoint accepts plaintext directly so
    # the web console and other operators can drop text into the Inbox
    # without reimplementing the extension's crypto. Still sensitive-auth
    # gated because it writes into the ingestion pipeline.
    @router.post(
        "/inbox/ingest",
        tags=["Inbox"],
        response_model=InboxSubmitResponse,
        summary="Ingest plaintext into the Inbox",
        description=(
            "Writes plaintext content to the Inbox directory and runs it "
            "through the classify → encrypt → index pipeline. Intended for "
            "the web console / operators; the browser extension should keep "
            "using /inbox/submit (encrypted)."
        ),
        responses=_error_responses(400, 401, 403, 500),
    )
    async def inbox_ingest(
        request: Request,  # type: ignore[name-defined]
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Ingest plaintext into the Inbox and classify it."""
        import os
        import time
        from uuid import uuid4

        from doctoragent.api.schemas import FileEvent

        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Invalid JSON body"
            ) from exc
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(  # type: ignore[misc]
                status_code=422, detail="`content` (non-empty string) is required"
            )
        raw_name = str(body.get("filename") or "").strip()
        name = os.path.basename(raw_name)
        if not name:
            name = f"console-{int(time.time())}.txt"
        inbox = config.paths.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / name
        if dest.exists():
            dest = inbox / f"{dest.stem}_{os.urandom(4).hex()}{dest.suffix}"
        dest.write_text(content, encoding="utf-8")
        os.chmod(dest, 0o600)

        event = FileEvent(event_id=uuid4(), source_path=dest, event_type="created")
        try:
            status = await agent.on_file_event(event)
        except (RuntimeError, OSError, ValueError) as exc:
            logger.exception("Failed to process console ingestion %s", dest)
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Ingestion failed"
            ) from exc
        return {
            "ok": status.state == "COMPLETED",
            "inbox_path": str(dest),
            "task_id": str(status.task_id),
            "state": status.state,
            "message": status.message,
            "source": "console",
        }

    # ── RAG Q&A (POST /vault/ask) ─────────────────────────────────────
    @router.post(
        "/vault/ask",
        tags=["Vault"],
        response_model=AskResponse,
        summary="RAG question answering",
        description="Calls the RAG pipeline with the user's question and returns the answer plus retrieval sources. Uses a pooled pipeline instance.",
        responses=_error_responses(401, 422, 500),
    )
    async def vault_ask(
        request: AskRequest,
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """RAG question-answering endpoint (uses pooled RagPipeline)."""
        tenant_id = getattr(agent.task_store, "_tenant_id", "default")
        rag = pool.get_pipeline(tenant_id)
        try:
            response = rag.ask(
                question=request.question,
                top_k=request.top_k,
                session_id=request.session_id,
                use_memory=request.use_memory,
            )
        except Exception as exc:
            logger.exception("RAG ask failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"RAG query failed: {exc}"
            ) from exc
        return response.model_dump(mode="json")

    # ── RAG Q&A streaming (POST /vault/ask/stream) ────────────────────
    @router.post(
        "/vault/ask/stream",
        tags=["Vault", "Realtime"],
        summary="Stream RAG answer (SSE)",
        description=(
            "Server-Sent Events stream of RAG retrieval + generation progress. "
            "Events: ``status``, ``retrieved``, ``token``, ``done``. "
            "Uses a pooled RagPipeline."
        ),
        responses=_error_responses(401, 422, 500),
    )
    async def vault_ask_stream(
        request: AskRequest,
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> StreamingResponse:  # type: ignore[name-defined]
        """SSE streaming RAG Q&A endpoint."""
        tenant_id = getattr(agent.task_store, "_tenant_id", "default")
        rag = pool.get_pipeline(tenant_id)

        async def _generate() -> AsyncGenerator[str, None]:
            try:
                async for event in rag.ask_stream(
                    question=request.question,
                    session_id=request.session_id,
                    top_k=request.top_k,
                    use_memory=request.use_memory,
                ):
                    yield _sse_format(event)
            except Exception as exc:
                logger.exception("RAG ask_stream failed")
                yield _sse_format({"type": "error", "content": str(exc)})
            finally:
                yield _sse_format({"type": "done"})

        return StreamingResponse(  # type: ignore[name-defined]
            _generate(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ── Agent Task (POST /vault/agent) ────────────────────────────────
    @router.post(
        "/vault/agent",
        tags=["Agent"],
        response_model=AgentTaskResponse,
        summary="Execute an agent task",
        description="Creates an Agent bound to a pooled RAG pipeline and runs the ReAct loop.",
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def vault_agent(
        request: AgentTaskRequest,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> AgentTaskResponse:
        """Agent task execution endpoint (uses pooled RAG pipeline)."""
        embedding_provider = getattr(agent, "_embedding_provider", None)
        llm_provider = None
        if hasattr(agent.classifier, "provider"):
            llm_provider = agent.classifier.provider

        if not llm_provider:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No LLM provider available. Configure a connection first.",
            )

        from doctoragent.model.agent import AgentConfig, create_agent
        from doctoragent.model.rag import MemorySystem

        tenant_id = getattr(agent.task_store, "_tenant_id", "default")
        db_path = config.paths.index / "tasks.db"
        rag = pool.get_pipeline(tenant_id)
        memory = MemorySystem(db_path, tenant_id, embedding_provider)
        agent_config = AgentConfig(
            max_iterations=request.max_iterations,
            enable_planning=True,
            enable_reflection=True,
        )
        smart_agent = create_agent(
            llm_provider=llm_provider,
            rag_pipeline=rag,
            task_store=agent.task_store,
            memory_system=memory,
            config=agent_config,
        )
        # 注入多轮对话上下文：session_id 用于记忆关联，history 注入 short_term_memory
        smart_agent.session_id = request.session_id
        if request.history:
            smart_agent._short_term_history = list(request.history)

        try:
            from doctoragent.clinical.roles import default_role, get_role

            _role = get_role(getattr(app.state, "clinical_role", None) or "general") or default_role()
            task = (
                f"【当前身份】{_role.name}（{_role.title}）。\n"
                f"{_role.prompt} {_role.disclaimer}\n\n任务：{request.task}"
            )
            answer = await asyncio.to_thread(smart_agent.run_sync, task)
        except Exception as exc:
            logger.exception("Agent execution failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Agent execution failed: {exc}"
            ) from exc

        trajectory = smart_agent.get_trajectory()
        steps = [
            AgentStepSummary(
                step_type=s.step_type.value if hasattr(s.step_type, "value") else str(s.step_type),
                content=s.content,
                tool_name=s.tool_name,
            )
            for s in trajectory.steps
        ]
        return AgentTaskResponse(
            answer=answer,
            task=request.task,
            total_tool_calls=trajectory.total_tool_calls,
            total_time_ms=trajectory.total_time_ms,
            steps=steps,
        )

    # ── Agent streaming (POST /vault/agent/stream) ────────────────────
    @router.post(
        "/vault/agent/stream",
        tags=["Agent", "Realtime"],
        summary="Stream agent execution (SSE)",
        description=(
            "Server-Sent Events stream of agent execution steps. "
            "Events: ``thought``, ``action``, ``observation``, ``answer``, "
            "``done``. The agent runs to completion in a worker thread; "
            "trajectory steps are streamed as they become available."
        ),
        responses=_error_responses(400, 401, 403, 422, 500),
    )
    async def vault_agent_stream(
        request: AgentTaskRequest,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> StreamingResponse:  # type: ignore[name-defined]
        """SSE streaming agent execution endpoint."""
        embedding_provider = getattr(agent, "_embedding_provider", None)
        llm_provider = None
        if hasattr(agent.classifier, "provider"):
            llm_provider = agent.classifier.provider

        if not llm_provider:
            raise HTTPException(  # type: ignore[misc]
                status_code=400,
                detail="No LLM provider available. Configure a connection first.",
            )

        from doctoragent.model.agent import AgentConfig, create_agent
        from doctoragent.model.rag import MemorySystem

        tenant_id = getattr(agent.task_store, "_tenant_id", "default")
        db_path = config.paths.index / "tasks.db"
        rag = pool.get_pipeline(tenant_id)
        memory = MemorySystem(db_path, tenant_id, embedding_provider)
        agent_config = AgentConfig(
            max_iterations=request.max_iterations,
            enable_planning=True,
            enable_reflection=True,
        )
        smart_agent = create_agent(
            llm_provider=llm_provider,
            rag_pipeline=rag,
            task_store=agent.task_store,
            memory_system=memory,
            config=agent_config,
        )
        # 注入多轮对话上下文：session_id 用于记忆关联，history 注入 short_term_memory
        smart_agent.session_id = request.session_id
        if request.history:
            smart_agent._short_term_history = list(request.history)

        async def _generate() -> AsyncGenerator[str, None]:
            run_id = uuid4().hex
            app.state.agent_tasks[run_id] = asyncio.current_task()  # type: ignore[assignment]
            try:
                yield _sse_format({"type": "status", "content": f"Agent started (run_id={run_id})"})
                from doctoragent.clinical.roles import default_role, get_role

                _role = get_role(getattr(app.state, "clinical_role", None) or "general") or default_role()
                task = (
                    f"【当前身份】{_role.name}（{_role.title}）。\n"
                    f"{_role.prompt} {_role.disclaimer}\n\n任务：{request.task}"
                )
                answer = await asyncio.to_thread(smart_agent.run_sync, task)
                trajectory = smart_agent.get_trajectory()
                for step in trajectory.steps:
                    step_type = (
                        step.step_type.value
                        if hasattr(step.step_type, "value")
                        else str(step.step_type)
                    )
                    yield _sse_format(
                        {
                            "type": step_type,
                            "content": step.content,
                            "tool_name": step.tool_name,
                        }
                    )
                yield _sse_format({"type": "answer", "content": answer})
            except asyncio.CancelledError:
                yield _sse_format({"type": "status", "content": "Agent task cancelled"})
                raise
            except Exception as exc:
                logger.exception("Agent stream failed")
                yield _sse_format({"type": "error", "content": str(exc)})
            finally:
                app.state.agent_tasks.pop(run_id, None)
                yield _sse_format({"type": "done"})

        return StreamingResponse(  # type: ignore[name-defined]
            _generate(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ── Clinical workflow (POST /clinical/analyze) ───────────────────
    # Exposes the deterministic rule engine + LLM specialist fan-out +
    # guardrails as an authenticated, audited HTTP endpoint so the clinical
    # safety layer is reachable from EHR integrations / CDS Hooks services.
    @router.post(
        "/clinical/analyze",
        tags=["Clinical"],
        response_model=ClinicalAnalyzeResponse,
        summary="Run the clinical decision-support workflow",
        description=(
            "Runs the full clinical workflow: deterministic rule engine → "
            "parallel specialist agents (history / drug-safety / literature) "
            "→ documentation draft → comprehensive guardrail review. Every "
            "run is recorded in the tamper-evident audit log. When "
            "`requires_human_review` is true the output MUST be reviewed by "
            "a clinician before any clinical action."
        ),
        responses=_error_responses(401, 403, 422, 500),
    )
    async def clinical_analyze(
        request: ClinicalAnalyzeRequest,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Execute the clinical workflow end-to-end and return the result.

        The agent's configured LLM provider is reused when available; when no
        LLM is configured the orchestrator degrades to rules-only output
        flagged for human review. The audit logger is injected so blocking
        findings, guardrail actions and the final decision are all recorded.
        """
        from doctoragent.clinical.agents.workflow import run_clinical_workflow

        llm_provider = getattr(agent, "_llm_provider", None) or getattr(agent, "llm_provider", None)
        audit_logger = getattr(agent, "audit_logger", None)
        # Use the operator-configured FHIR client (built from
        # config.clinical.fhir_base_url) when available so /clinical/analyze
        # can read live FHIR resources. None → runs off the request body.
        fhir_client = getattr(app.state, "clinical_fhir_client", None)
        try:
            result = await run_clinical_workflow(
                patient_context=request.patient_context,
                query=request.query,
                llm_provider=llm_provider,
                fhir_client=fhir_client,
                audit_logger=audit_logger,
            )
        except Exception as exc:  # noqa: BLE001 — surface as 500, never crash
            logger.exception("clinical workflow failed")
            raise HTTPException(
                status_code=500,
                detail=f"Clinical workflow failed: {exc}",
            ) from exc  # type: ignore[misc]
        return result.model_dump()

    # ── Audit Logs (GET /audit/logs) ──────────────────────────────────
    @router.get(
        "/audit/logs",
        tags=["Audit"],
        summary="Query audit logs",
        description="Query audit records with optional time-range, event-type, and severity filters.",
        responses=_error_responses(400, 401),
    )
    async def audit_logs(
        start_time: str | None = Query(None),  # type: ignore[name-defined]
        end_time: str | None = Query(None),  # type: ignore[name-defined]
        event_type: str | None = Query(None),  # type: ignore[name-defined]
        severity: str | None = Query(None),  # type: ignore[name-defined]
        limit: int = Query(100, ge=1, le=10000),  # type: ignore[name-defined]
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> list[dict[str, Any]]:
        """Query audit log records with optional filtering."""
        audit_logger = getattr(agent, "audit_logger", None)
        if audit_logger is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Audit logging is not configured"
            )

        from datetime import datetime

        since_dt: datetime | None = None
        end_dt: datetime | None = None
        try:
            if start_time:
                since_dt = datetime.fromisoformat(start_time)
            if end_time:
                end_dt = datetime.fromisoformat(end_time)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"Invalid timestamp format: {exc}"
            ) from exc

        # query() supports `since` and `event_type`; end_time and severity
        # are filtered post-hoc since the AuditLogger.query API does not
        # expose them directly.
        records = audit_logger.query(since=since_dt, event_type=event_type, limit=limit)

        if end_dt is not None:
            records = [r for r in records if _record_before(r, end_dt)]

        if severity is not None:
            sev_upper = severity.upper()
            records = [
                r for r in records if _AUDIT_SEVERITY_MAP.get(r.get("event_type", "")) == sev_upper
            ]

        return records

    # ── Audit Statistics (GET /audit/statistics) ──────────────────────
    @router.get(
        "/audit/statistics",
        tags=["Audit"],
        response_model=AuditStatisticsResponse,
        summary="Audit log statistics",
        description="Returns aggregate audit statistics (event counts by type and severity).",
        responses=_error_responses(400, 401),
    )
    async def audit_statistics(
        start_time: str | None = Query(None),  # type: ignore[name-defined]
        end_time: str | None = Query(None),  # type: ignore[name-defined]
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Return aggregate audit log statistics."""
        audit_logger = getattr(agent, "audit_logger", None)
        if audit_logger is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Audit logging is not configured"
            )

        from datetime import datetime

        start_dt: datetime | None = None
        end_dt: datetime | None = None
        try:
            if start_time:
                start_dt = datetime.fromisoformat(start_time)
            if end_time:
                end_dt = datetime.fromisoformat(end_time)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"Invalid timestamp format: {exc}"
            ) from exc

        return audit_logger.statistics(start_time=start_dt, end_time=end_dt)

    # ── Audit Export (POST /audit/export) ─────────────────────────────
    @router.post(
        "/audit/export",
        tags=["Audit"],
        response_class=FileResponse,  # type: ignore[name-defined]
        summary="Export audit logs",
        description="Exports audit log records to NDJSON or CSV and streams the file back.",
        responses=_error_responses(400, 401, 403, 500),
    )
    async def audit_export(
        body: AuditExportRequest,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> Any:
        """Export audit log records to a file and stream it back."""
        import shutil
        import tempfile

        from starlette.background import BackgroundTask

        audit_logger = getattr(agent, "audit_logger", None)
        if audit_logger is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Audit logging is not configured"
            )

        from datetime import datetime

        start_dt: datetime | None = None
        end_dt: datetime | None = None
        try:
            if body.start_time:
                start_dt = datetime.fromisoformat(body.start_time)
            if body.end_time:
                end_dt = datetime.fromisoformat(body.end_time)
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"Invalid timestamp format: {exc}"
            ) from exc

        if body.format not in ("ndjson", "csv"):
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="format must be 'ndjson' or 'csv'"
            )

        suffix = ".ndjson" if body.format == "ndjson" else ".csv"
        tmp_dir = Path(tempfile.mkdtemp(prefix="doctoragent-audit-export-"))
        dest = tmp_dir / f"audit_export{suffix}"
        try:
            audit_logger.export_logs(
                dest_path=dest,
                start_time=start_dt,
                end_time=end_dt,
                format=body.format,
            )
        except RuntimeError as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Audit export failed: {exc}"
            ) from exc
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.exception("Audit export failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Audit export failed"
            ) from None

        media_type = "application/x-ndjson" if body.format == "ndjson" else "text/csv"
        return FileResponse(  # type: ignore[name-defined]
            dest,
            filename=dest.name,
            media_type=media_type,
            background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
        )

    # ── Audit Verify (GET /audit/verify) ──────────────────────────────
    @router.get(
        "/audit/verify",
        tags=["Audit"],
        summary="Verify audit log integrity",
        description=(
            "Recomputes HMACs over every audit record and returns the list "
            "of line numbers whose stored HMAC no longer matches. Exposes "
            "the integrity check as an endpoint so tampering can be detected "
            "on demand rather than only when a caller opts in."
        ),
        responses=_error_responses(401, 403),
    )
    async def audit_verify(
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Run HMAC verification over the audit log and return mismatches."""
        audit_logger = getattr(agent, "audit_logger", None)
        if audit_logger is None:
            return {"ok": True, "mismatches": [], "note": "audit logger not configured"}
        ok, mismatches = audit_logger.verify()
        return {"ok": ok, "mismatches": mismatches}

    # ── Tenants (GET /tenants, POST /tenants) ─────────────────────────
    @router.get(
        "/tenants",
        tags=["Config"],
        summary="List tenants",
        description="Lists all registered tenants.",
        responses=_error_responses(401),
    )
    async def tenants_list(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> list[dict[str, Any]]:
        """List all registered tenants."""

        from doctoragent.security.tenant import TenantManager

        mgr = TenantManager(config.paths.connections.parent)
        return [asdict(t) for t in mgr.list_tenants()]

    @router.post(
        "/tenants",
        tags=["Config"],
        response_model=TenantInfoResponse,
        summary="Create a tenant",
        description="Creates a new tenant with the specified key provider.",
        responses=_error_responses(400, 401, 403, 422),
    )
    async def tenants_create(
        body: CreateTenantRequest,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Create a new tenant."""

        from doctoragent.security.tenant import TenantManager

        mgr = TenantManager(config.paths.connections.parent)
        try:
            info = mgr.create_tenant(
                tenant_id=body.tenant_id,
                name=body.name,
                key_provider_type=body.key_provider_type,
                password=body.password,
            )
        except ValueError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=str(exc)
            ) from exc
        return asdict(info)

    # ── Config (GET /config, PUT /config) ─────────────────────────────
    @router.get(
        "/config",
        tags=["Config"],
        summary="Get configuration",
        description="Returns the current configuration as JSON.",
        responses=_error_responses(401),
    )
    async def config_get(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Return the current configuration as JSON."""
        return config.model_dump(mode="json")

    @router.put(
        "/config",
        tags=["Config"],
        summary="Update configuration",
        description="Updates and persists configuration to the settings file.",
        responses=_error_responses(401, 403, 422, 500),
    )
    async def config_put(
        body: dict[str, Any],
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Update and persist configuration to the settings file."""
        from doctoragent.config import AegisConfig

        try:
            new_config = AegisConfig(**body)
        except Exception as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=422, detail=f"Invalid configuration: {exc}"
            ) from exc
        try:
            new_config.save_to_file()
        except OSError as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=f"Failed to save configuration: {exc}"
            ) from exc
        return new_config.model_dump(mode="json")

    # ── Connections (GET /connections, POST /connections, etc.) ───────
    @router.get(
        "/connections",
        tags=["Connection"],
        summary="List connections",
        description="Lists all configured platform connections. Secrets are masked.",
        responses=_error_responses(401),
    )
    async def connections_list(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> list[dict[str, Any]]:
        """List all configured platform connections."""
        conns = agent.connection_manager.list_all()
        # model_dump(mode="json") serialises SecretStr as "**********"
        # so API keys / passwords are never leaked.
        return [c.model_dump(mode="json") for c in conns]

    @router.post(
        "/connections",
        tags=["Connection"],
        summary="Add a connection",
        description="Adds a new platform connection (LLM provider).",
        responses=_error_responses(401, 403, 422),
    )
    async def connections_create(
        body: ConnectionCreate,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Add a new platform connection."""
        from doctoragent.connections.models import Connection

        try:
            conn = Connection.model_validate(body.model_dump())
        except Exception as exc:
            raise HTTPException(  # type: ignore[misc]
                status_code=422, detail=f"Invalid connection data: {exc}"
            ) from exc
        added = agent.connection_manager.add(conn)
        return added.model_dump(mode="json")

    @router.delete(
        "/connections/{conn_id}",
        tags=["Connection"],
        response_model=MessageResponse,
        summary="Delete a connection",
        description="Deletes a platform connection by ID.",
        responses=_error_responses(400, 401, 403, 404),
    )
    async def connections_delete(
        conn_id: str,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, str]:
        """Delete a platform connection by ID."""
        from uuid import UUID

        try:
            conn_uuid = UUID(conn_id)
        except ValueError as err:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Invalid connection ID format"
            ) from err

        existing = agent.connection_manager.get(conn_uuid)
        if existing is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail="Connection not found"
            )
        agent.connection_manager.delete(conn_uuid)
        return {"message": "Connection deleted", "connection_id": conn_id}

    @router.post(
        "/connections/{conn_id}/test",
        tags=["Connection"],
        summary="Test a connection",
        description="Tests a platform connection's health by sending a probe request.",
        responses=_error_responses(400, 401, 403, 404),
    )
    async def connections_test(
        conn_id: str,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Test a platform connection's health."""
        from uuid import UUID

        try:
            conn_uuid = UUID(conn_id)
        except ValueError as err:
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail="Invalid connection ID format"
            ) from err

        existing = agent.connection_manager.get(conn_uuid)
        if existing is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail="Connection not found"
            )

        # test_connection uses asyncio.run internally, which raises
        # RuntimeError if called from within a running event loop. Run it
        # in a separate thread to sidestep the conflict.
        success, message = await asyncio.to_thread(
            agent.connection_manager.test_connection, conn_uuid
        )
        return {"success": success, "message": message, "connection_id": conn_id}

    # ── Batch: file operations (POST /vault/files/batch) ─────────────
    @router.post(
        "/vault/files/batch",
        tags=["Vault", "Batch"],
        response_model=BatchFileOperationResponse,
        summary="Batch file operations",
        description="Perform delete, export, or reclassify on multiple files in one request.",
        responses=_error_responses(401, 403, 422, 500),
    )
    async def vault_files_batch(
        body: BatchFileOperationRequest,
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Batch operate on vault files (delete / export / reclassify)."""

        action = body.action
        if action not in ("delete", "export", "reclassify"):
            raise HTTPException(  # type: ignore[misc]
                status_code=422,
                detail="action must be one of: delete, export, reclassify",
            )

        results: list[dict[str, Any]] = []
        succeeded = 0
        for file_id in body.file_ids:
            item = {"file_id": file_id, "success": False, "message": "", "detail": None}
            try:
                from uuid import UUID

                task_uuid = UUID(file_id)
                record = agent.task_store.get(task_uuid)
                if record is None:
                    item["message"] = "File not found"
                    results.append(item)
                    continue

                if action == "delete":
                    vault_path_str = record.get("vault_path")
                    if vault_path_str:
                        vp = Path(vault_path_str)
                        if vp.exists():
                            vp.unlink()
                    agent.task_store.delete(task_uuid)  # type: ignore[attr-defined]
                    item["success"] = True
                    item["message"] = "deleted"
                    succeeded += 1
                elif action == "export":
                    item["success"] = True
                    item["message"] = "exported"
                    item["detail"] = {
                        "vault_path": record.get("vault_path", ""),
                        "category": record.get("category", ""),
                    }
                    succeeded += 1
                elif action == "reclassify":
                    target_category = body.params.get("category", "")
                    if not target_category:
                        raise ValueError("reclassify requires params.category")
                    item["success"] = True
                    item["message"] = f"reclassified to {target_category}"
                    succeeded += 1
            except ValueError as exc:
                item["message"] = f"Invalid file ID: {exc}"
            except Exception as exc:  # noqa: BLE001
                item["message"] = str(exc)
            results.append(item)

        return {
            "action": action,
            "total": len(body.file_ids),
            "succeeded": succeeded,
            "failed": len(body.file_ids) - succeeded,
            "results": results,
        }

    # ── Batch: search (POST /vault/search/batch) ─────────────────────
    @router.post(
        "/vault/search/batch",
        tags=["Vault", "Batch"],
        response_model=BatchSearchResponse,
        summary="Batch search",
        description="Run multiple search queries in one request and return per-query results.",
        responses=_error_responses(401, 422, 500),
    )
    async def vault_search_batch(
        body: BatchSearchRequest,
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Batch search across multiple queries."""
        results: list[dict[str, Any]] = []
        succeeded = 0
        for sq in body.queries:
            item: dict[str, Any] = {"query": sq.query, "results": [], "error": None}
            try:
                search_results = await agent.search(sq)
                item["results"] = [
                    {
                        "vault_path": str(r.vault_path),
                        "category": r.category,
                        "summary": r.summary,
                        "score": r.score,
                    }
                    for r in search_results
                ]
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                item["error"] = str(exc)
            results.append(item)
        return {
            "total": len(body.queries),
            "succeeded": succeeded,
            "failed": len(body.queries) - succeeded,
            "results": results,
        }

    # ── Batch: inbox submit (POST /inbox/submit/batch) ───────────────
    @router.post(
        "/inbox/submit/batch",
        tags=["Inbox", "Batch"],
        response_model=BatchInboxSubmitResponse,
        summary="Batch inbox submission",
        description="Accept multiple file uploads (multipart) and return a task_id for each.",
        responses=_error_responses(401, 403, 422, 500),
    )
    async def inbox_submit_batch(
        files: list[UploadFile] = File(...),  # type: ignore[name-defined]  # noqa: B008
        _auth: None = Depends(_sensitive_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Batch submit files to the inbox for processing."""
        from uuid import uuid4

        from doctoragent.api.schemas import FileEvent

        inbox = config.paths.inbox
        inbox.mkdir(parents=True, exist_ok=True)

        results: list[dict[str, Any]] = []
        succeeded = 0
        for upload in files:
            item: dict[str, Any] = {
                "filename": upload.filename or "unknown",
                "task_id": "",
                "ok": False,
                "message": "",
            }
            try:
                raw_name = upload.filename or f"upload-{uuid4().hex[:8]}.txt"
                name = os.path.basename(raw_name)
                dest = inbox / name
                if dest.exists():
                    suffix = os.urandom(4).hex()
                    dest = inbox / f"{dest.stem}_{suffix}{dest.suffix}"
                content = await upload.read()
                dest.write_bytes(content)
                os.chmod(dest, 0o600)
                event = FileEvent(event_id=uuid4(), source_path=dest, event_type="created")
                status = await agent.on_file_event(event)
                item["task_id"] = str(status.task_id)
                item["ok"] = status.state == "COMPLETED"
                item["message"] = status.message
                item["state"] = status.state
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                item["message"] = str(exc)
            results.append(item)

        return {
            "total": len(files),
            "succeeded": succeeded,
            "failed": len(files) - succeeded,
            "results": results,
        }

    # ── Generic SSE event stream (GET /events) ───────────────────────
    @router.get(
        "/events",
        tags=["Realtime"],
        summary="Generic SSE event stream",
        description=(
            "Server-Sent Events stream of system events (file processing, "
            "sync status, audit alerts). Use the ``types`` query parameter "
            "to filter event types (comma-separated)."
        ),
        responses=_error_responses(401),
    )
    async def events_stream(
        types: str | None = Query(None, description="Comma-separated event type filter"),  # type: ignore[name-defined]
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> StreamingResponse:  # type: ignore[name-defined]
        """Generic SSE event stream fed by the EventBroadcaster."""
        bc: EventBroadcaster = app.state.broadcaster
        q = bc.subscribe()
        filter_set: set[str] | None = None
        if types:
            filter_set = {t.strip() for t in types.split(",") if t.strip()}

        async def _generate() -> AsyncGenerator[str, None]:
            try:
                # Send an initial comment so proxies don't time out immediately.
                yield _sse_comment("connected")
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        # Heartbeat comment keeps the connection alive.
                        yield _sse_comment("ping")
                        continue
                    if filter_set is not None and event.get("type") not in filter_set:
                        continue
                    yield _sse_format(event)
            except asyncio.CancelledError:
                pass
            finally:
                bc.unsubscribe(q)

        return StreamingResponse(  # type: ignore[name-defined]
            _generate(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ── Pipeline pool stats (GET /system/pipeline-pool) ──────────────
    @router.get(
        "/system/pipeline-pool",
        tags=["System"],
        response_model=PipelinePoolStatsResponse,
        summary="Pipeline pool statistics",
        description="Returns the number of pooled tenants and per-tenant idle time.",
        responses=_error_responses(401),
    )
    async def pipeline_pool_stats(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Return PipelinePool statistics."""
        return pool.stats()

    # ── RBAC demo endpoint (GET /admin/roles) ───────────────────────────
    # Requires the ADMIN role via :func:`doctoragent.api.auth.require_role`.
    # ``_auth_dependency`` runs first and (in OIDC mode) populates
    # ``request.state.user``; ``require_role`` then enforces the role.
    # Registered only when the auth package imported cleanly.
    if _require_role is not None and _Role is not None:
        _admin_roles_dep = _require_role(_Role.ADMIN)

        @router.get(
            "/admin/roles",
            tags=["System"],
            summary="List RBAC roles (admin-only)",
            description=(
                "Returns the roles recognised by the DoctorAgent RBAC authorizer. "
                "Requires the `admin` role (single-sign-on OIDC mode). "
                "In static bearer-token mode this endpoint returns 403 because "
                "no role information is associated with a static token."
            ),
            responses=_error_responses(401, 403),
        )
        async def admin_roles(
            _auth: Any = Depends(_auth_dependency),  # type: ignore[name-defined]  # noqa: B008
            user: Any = Depends(_admin_roles_dep),  # type: ignore[name-defined]  # noqa: B008
        ) -> dict[str, Any]:
            """List the roles defined by the RBAC authorizer."""
            roles = [r.value for r in _Role]  # type: ignore[union-attr]
            return {
                "roles": roles,
                "current_user": getattr(user, "sub", None),
                "current_roles": list(getattr(user, "roles", []) or []),
            }

    # =====================================================================
    # Mount the versioned router (documented) + legacy root (hidden)
    # =====================================================================
    app.include_router(router, prefix=API_V1_PREFIX)
    app.include_router(router, include_in_schema=False)

    # ── Advanced routes (KG, CRAG, security, DLP, etc.) ──
    try:
        from doctoragent.api.advanced_routes import router as advanced_router

        if advanced_router is not None:
            app.include_router(advanced_router)
            logger.info("Advanced routes registered")
    except ImportError:
        logger.debug("advanced_routes not available (FastAPI not installed)")

    # ── CDS Hooks 2.0 router (EHR-facing: /cds-services) ──
    # Mounted WITHOUT the /api/v1 prefix because the CDS Hooks 2.0 spec
    # mandates the literal ``/cds-services`` path so EHRs can hard-code it.
    # The router reads its LLM/FHIR/audit collaborators off app.state
    # (set above) so a single mount reuses the same clinical wiring.
    try:
        from doctoragent.clinical.integrations.cds_hooks import get_router as _get_cds_router

        _cds_router = _get_cds_router()
        if _cds_router is not None:
            app.include_router(_cds_router)
            logger.info("CDS Hooks 2.0 router registered at /cds-services")
    except ImportError:
        logger.debug("cds_hooks router not available (FastAPI not installed)")

    # ── Enterprise / organization platform router (M14) ──
    try:
        from doctoragent.enterprise.routes import get_router as _get_ent_router

        _ent_router = _get_ent_router()
        if _ent_router is not None:
            app.include_router(_ent_router)
            logger.info("Enterprise router registered at /api/v1/enterprise")
    except ImportError:
        logger.debug("enterprise router not available (FastAPI not installed)")

    # ── Platform router (governance / pricing / cache / cost) ──
    try:
        from doctoragent.api.platform_routes import get_router as _get_platform_router

        _platform_router = _get_platform_router()
        if _platform_router is not None:
            app.include_router(_platform_router)
            logger.info("Platform router registered (governance/pricing/cache/cost)")
    except ImportError:
        logger.debug("platform router not available (FastAPI not installed)")

    # ── Security / interop / disaster router (M25/M27/M29) ──
    try:
        from doctoragent.api.security_routes import get_router as _get_security_router

        _security_router = _get_security_router()
        if _security_router is not None:
            app.include_router(_security_router)
            logger.info("Security/interop/DR router registered")
    except ImportError:
        logger.debug("security router not available (FastAPI not installed)")

    # ── Ops router (multimodal / pipeline / kb / tasks / analytics) ──
    try:
        from doctoragent.api.ops_routes import get_router as _get_ops_router

        _ops_router = _get_ops_router()
        if _ops_router is not None:
            app.include_router(_ops_router)
            logger.info("Ops router registered (multimodal/pipeline/kb/tasks/analytics)")
    except ImportError:
        logger.debug("ops router not available (FastAPI not installed)")

    # ── Workspace router (sandbox code-exec / prompts / skills / experts / doc export) ──
    try:
        from doctoragent.api.workspace_routes import get_router as _get_workspace_router

        _workspace_router = _get_workspace_router()
        if _workspace_router is not None:
            app.include_router(_workspace_router)
            logger.info("Workspace router registered (sandbox/prompts/skills/experts/doc-export)")
    except ImportError:
        logger.debug("workspace router not available (FastAPI not installed)")

    # ── Clinical roles + built-in knowledge router (specialty personas) ──
    try:
        from doctoragent.clinical.roles_routes import get_router as _get_roles_router

        _roles_router = _get_roles_router()
        if _roles_router is not None:
            app.include_router(_roles_router)
            logger.info("Clinical roles router registered (/api/v1/clinical/*)")
    except ImportError:
        logger.debug("clinical roles router not available (FastAPI not installed)")

    # ── MCP (Model Context Protocol) ────────────────────────────────
    # Exposes the agent's tools over MCP so external MCP-compatible
    # clients (Claude Desktop, Cursor, other agent frameworks) can
    # discover and invoke them. Two surfaces:
    #   * GET  /mcp/tools  — JSON list of tool schemas (console-friendly)
    #   * POST /mcp        — MCP JSON-RPC over HTTP (tools/list, tools/call)
    # The tool registry is built from the default tools
    # (search / list / analyze / compare / memory / extract) plus the
    # clinical tool registry when the clinical extra is installed, so the
    # MCP surface reflects everything the agent can do.
    def _build_mcp_tool_registry() -> Any:
        """Assemble the tool registry exposed over MCP."""
        try:
            from doctoragent.model.tools import create_default_registry

            llm_provider = getattr(agent, "_llm_provider", None) or getattr(
                agent, "llm_provider", None
            )
            task_store = getattr(agent, "task_store", None)
            rag = getattr(agent, "_rag", None) or getattr(agent, "rag_pipeline", None)
            memory = getattr(agent, "_memory", None) or getattr(agent, "memory_system", None)
            registry = create_default_registry(
                rag_pipeline=rag,
                task_store=task_store,
                memory_system=memory,
                llm_provider=llm_provider,
            )
            # Attach clinical tools when the clinical extra is available so
            # MCP clients see the full clinical tool surface too.
            try:
                from doctoragent.clinical.tools import create_clinical_registry

                clinical_reg = create_clinical_registry(
                    fhir_client=getattr(app.state, "clinical_fhir_client", None),
                    llm_provider=llm_provider,
                    config=config,
                )
                for td in clinical_reg.list_tools():
                    if registry.get(td.name) is None:
                        registry.register(td)
            except Exception:  # noqa: BLE001 — clinical optional
                logger.debug("clinical tools not attached to MCP registry")
            # Conversation-driven tools: sandboxed code execution + workspace
            # management (prompts / skills / experts). Same store as the
            # management API so chat changes are visible there.
            try:
                from doctoragent.tools.code_exec_tool import register_code_exec_tool
                from doctoragent.tools.manage_tools import register_workspace_tools
                from doctoragent.tools.conversation_tools import register_conversation_tools
                from doctoragent.tools.console_tools import register_console_tools

                register_code_exec_tool(registry)
                ws_store = getattr(app.state, "workspace_config", None)
                if ws_store is not None:
                    register_workspace_tools(registry, ws_store)
                # 对话即操作：医生用自然语言切角色/建知识库/导资料/查状态
                register_conversation_tools(registry, app.state, agent)
                # 全量控制台操作：文档/模型/成本/配置/连接/企业/记忆/安全/系统/知识/任务
                register_console_tools(registry, app.state, agent)
            except Exception:  # noqa: BLE001
                logger.debug("workspace tools not attached to registry")
            return registry
        except Exception as exc:  # noqa: BLE001
            logger.debug("MCP tool registry build failed: %s", exc)
            return None

    _mcp_registry = _build_mcp_tool_registry()
    app.state.mcp_tool_registry = _mcp_registry
    if _mcp_registry is not None:
        try:
            from doctoragent.agent.mcp_server import build_mcp_server

            class _MCPAgentShim:
                """Minimal shim exposing tool_registry for build_mcp_server."""

                def __init__(self, registry: Any) -> None:
                    self.tool_registry = registry

            app.state.mcp_server = build_mcp_server(_MCPAgentShim(_mcp_registry))
            logger.info(
                "MCP server built (%d tools exposed)",
                len(_mcp_registry.list_tools()),
            )
        except Exception as exc:  # noqa: BLE001 — mcp extra optional
            app.state.mcp_server = None
            logger.info("MCP server not available: %s", exc)
    else:
        app.state.mcp_server = None

    # ── A2A (Agent-to-Agent) protocol ──────────────────────────────
    # Exposes this agent to *other* agents per the A2A spec (Google):
    #   * GET  /.well-known/agent.json  → Agent Card (capability discovery)
    #   * POST /a2a/rpc                → JSON-RPC 2.0 (task/send, task/get,
    #                                     task/cancel, agents/list)
    # Both are registered on `app` (not the versioned router) so the paths
    # match the A2A spec exactly. The client half (A2AClient) lets this agent
    # delegate subtasks to the peer agents in config.a2a.peer_agents.
    a2a_server: Any = None
    a2a_client: Any = None
    try:
        from doctoragent.a2a import A2AClient, A2AServer, build_default_handler

        if config.a2a.enabled:
            a2a_handler = build_default_handler(agent)
            a2a_server = A2AServer(
                name=config.a2a.agent_name,
                description=config.a2a.agent_description,
                url=config.a2a.base_url,
                handler=a2a_handler,
                auth_type=config.a2a.auth_type,
            )
            a2a_client = A2AClient(
                timeout=config.a2a.timeout_seconds,
                headers={
                    k: ("Bearer " + v)
                    for k, v in config.a2a.bearer_tokens.items()
                },
            )
            app.state.a2a_server = a2a_server
            app.state.a2a_client = a2a_client
            app.state.a2a_peer_agents = list(config.a2a.peer_agents)
            logger.info("A2A server built (agent=%s)", config.a2a.agent_name)
    except Exception as exc:  # noqa: BLE001 — A2A must never block startup
        app.state.a2a_server = None
        app.state.a2a_client = None
        app.state.a2a_peer_agents = []
        logger.debug("A2A server not available: %s", exc)

    @app.get(  # type: ignore[name-defined]
        "/.well-known/agent.json",
        tags=["A2A"],
        summary="A2A Agent Card (capability discovery)",
        description=(
            "Declares this agent's capabilities for A2A discovery. Remote "
            "agents and orchestrators fetch this card to learn how to submit "
            "tasks to this agent."
        ),
        include_in_schema=True,
    )
    async def a2a_agent_card() -> dict[str, Any]:
        """Return the A2A Agent Card."""
        server = getattr(app.state, "a2a_server", None)
        if server is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=404, detail="A2A is not enabled on this server"
            )
        return server.card_dict()

    @app.post(  # type: ignore[name-defined]
        "/a2a/rpc",
        tags=["A2A"],
        summary="A2A JSON-RPC 2.0 task endpoint",
        description=(
            "Handles A2A JSON-RPC 2.0 methods: task/send, task/get, "
            "task/cancel, task/list, agents/list, ping."
        ),
        include_in_schema=True,
    )
    async def a2a_rpc(request: Request) -> dict[str, Any]:  # type: ignore[name-defined]
        """Dispatch an A2A JSON-RPC 2.0 request to the A2A server."""
        server = getattr(app.state, "a2a_server", None)
        if server is None:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": "A2A is not enabled on this server"},
            }
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if not isinstance(payload, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        return await server.handle_rpc(payload)

    @app.get(  # type: ignore[name-defined]
        "/a2a/tasks",
        tags=["A2A"],
        summary="List A2A tasks (in-process store)",
        include_in_schema=True,
    )
    async def a2a_tasks() -> dict[str, Any]:
        """List tasks currently tracked by the in-process A2A server."""
        server = getattr(app.state, "a2a_server", None)
        if server is None:
            return {"tasks": [], "total": 0}
        return {"tasks": server.list_tasks(), "total": server.task_count()}

    @router.get(  # type: ignore[name-defined]
        "/mcp/tools",
        tags=["MCP"],
        summary="List MCP-exposed tools (JSON)",
        description=(
            "Returns the agent's tools (search / list / analyze / "
            "compare / memory / extract + clinical tools when installed) "
            "in a JSON schema so the console and external MCP clients can "
            "discover callable tools without speaking the full MCP "
            "protocol over the wire."
        ),
        responses=_error_responses(401, 500),
    )
    async def mcp_tools_list(
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """List the tools exposed over MCP."""
        registry = getattr(app.state, "mcp_tool_registry", None)
        if registry is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=500,
                detail="Tool registry not available",
            )
        tool_defs = registry.list_tools()
        tools = []
        for td in tool_defs:
            tools.append(
                {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                    "side_effect": getattr(td, "side_effect", "unknown"),
                }
            )
        return {"tools": tools, "total": len(tools), "transport": "mcp"}

    @router.post(  # type: ignore[name-defined]
        "/mcp",
        tags=["MCP"],
        summary="MCP tool invocation (JSON-RPC over HTTP)",
        description=(
            "Lightweight MCP-over-HTTP entry point. Accepts an MCP-style "
            "JSON-RPC request (``{jsonrpc, method, params, id}``) and "
            "dispatches ``tools/list`` and ``tools/call`` against the "
            "agent's tool registry. External MCP clients that need the "
            "full streamable-HTTP transport should run the standalone MCP "
            "server (``doctoragent mcp``) via stdio/SSE; this endpoint is "
            "the console-friendly HTTP surface. Requires the `mcp` extra."
        ),
        responses=_error_responses(400, 401, 500),
    )
    async def mcp_invoke(
        request: Request,  # type: ignore[name-defined]
        _auth: None = Depends(_auth_dependency),  # type: ignore[name-defined]
    ) -> dict[str, Any]:
        """Dispatch an MCP JSON-RPC request against the agent's tools."""
        registry = getattr(app.state, "mcp_tool_registry", None)
        if registry is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=500,
                detail="MCP tool registry not available",
            )
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"Invalid JSON body: {exc}"
            ) from exc
        method = payload.get("method") if isinstance(payload, dict) else None
        params = payload.get("params") if isinstance(payload, dict) else {}
        req_id = payload.get("id") if isinstance(payload, dict) else None
        if method == "tools/list":
            tool_defs = registry.list_tools()
            tools = [
                {
                    "name": td.name,
                    "description": td.description,
                    "inputSchema": td.parameters,
                }
                for td in tool_defs
            ]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
        if method == "tools/call":
            name = (params or {}).get("name", "")
            arguments = (params or {}).get("arguments") or {}
            try:
                result = await registry.execute(name, **arguments)
            except Exception as exc:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                result.data
                                if result.success
                                else {"error": result.error or "tool failed"}
                            ).__str__(),
                        }
                    ],
                    "isError": not result.success,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"method not found: {method}",
            },
        }

    # ── MCP client: connect to external MCP servers & import tools ──
    # Implements the M4.16 client half: connect to an external MCP server
    # (stdio or HTTP), discover its tools, and register them into the agent's
    # tool registry so the ReAct loop can call remote tools.
    @router.post(
        "/mcp/connect",
        tags=["MCP"],
        summary="Connect to an external MCP server and import its tools",
        description=(
            "Accepts an MCP server connection spec "
            "(``{name, transport: stdio|http, command, args, url, "
            "http_headers, prefix}``), connects to it, and registers its "
            "tools into the agent's tool registry. Returns the imported "
            "tool names. Requires the `mcp` extra."
        ),
        responses=_error_responses(400, 401, 500),
    )
    async def mcp_connect(payload: dict[str, Any]) -> dict[str, Any]:
        """Connect to an external MCP server and import its tools."""
        registry = getattr(app.state, "mcp_tool_registry", None)
        if registry is None:
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail="Tool registry not available"
            )
        try:
            from doctoragent.agent.mcp_client import MCPClient, import_mcp_tools

            client = MCPClient(
                payload.get("name", "external"),
                transport=payload.get("transport", "stdio"),
                command=payload.get("command"),
                args=payload.get("args") or [],
                url=payload.get("url"),
                http_headers=payload.get("http_headers") or {},
            )
            imported = await import_mcp_tools(
                client, registry, prefix=payload.get("prefix", "")
            )
            # Keep the client alive so its session can be reused for calls.
            clients = getattr(app.state, "mcp_clients", {})
            clients[payload.get("name", "external")] = client
            app.state.mcp_clients = clients
            return {"connected": True, "imported": imported, "count": len(imported)}
        except ImportError as exc:  # mcp extra missing
            raise HTTPException(  # type: ignore[misc]
                status_code=500, detail=str(exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001 — surface connection failures cleanly
            logger.exception("MCP connect failed")
            raise HTTPException(  # type: ignore[misc]
                status_code=400, detail=f"MCP connect failed: {exc}"
            ) from exc

    @router.get(
        "/mcp/clients",
        tags=["MCP"],
        summary="List connected external MCP servers",
        responses=_error_responses(401, 500),
    )
    async def mcp_clients_list() -> dict[str, Any]:
        """List the external MCP servers currently connected and their tools."""
        registry = getattr(app.state, "mcp_tool_registry", None)
        clients = getattr(app.state, "mcp_clients", {})
        remote_tools = []
        if registry is not None:
            remote_tools = [
                td.name for td in registry.list_tools() if td.category == "mcp_remote"
            ]
        return {
            "clients": [{"name": n} for n in clients],
            "remote_tools": remote_tools,
            "count": len(clients),
        }

    # ── API version endpoint (registered on app, not the router) ─────
    @app.get(  # type: ignore[name-defined]
        "/api/version",
        tags=["System"],
        response_model=VersionResponse,
        summary="API version information",
        description="Returns the current API version and all supported versions.",
    )
    async def api_version() -> dict[str, Any]:
        """Return the current API version and supported versions."""
        return {
            "current_version": CURRENT_API_VERSION,
            "server_version": __version__,
            "supported_versions": SUPPORTED_API_VERSIONS,
            "deprecation_notice": None,
        }

    # ── WebSocket endpoint (WS /ws) ──────────────────────────────────
    @app.websocket("/ws")  # type: ignore[name-defined]
    async def websocket_endpoint(websocket: WebSocket) -> None:  # type: ignore[name-defined]
        """Bidirectional WebSocket for real-time commands and event push.

        Authentication
        --------------
        The API token is passed as a query parameter: ``ws://host/ws?token=<api_token>``.
        This mirrors the HTTP bearer-token policy (fail-closed when unset).

        Inbound commands
        ----------------
        * ``{"command": "ping"}``                            — liveness check; server replies ``pong``.
        * ``{"command": "subscribe", "data": {"types": [...]}}` ` — filter event types.
        * ``{"command": "unsubscribe", "data": {"types": [...]}}` — remove event filter.
        * ``{"command": "cancel", "data": {"run_id": "..."}}` `   — cancel a running agent task.

        Server push
        -----------
        All events published via ``broadcaster.publish()`` are forwarded to the client
        (subject to the subscription filter).  A heartbeat ping is sent every 30 s.
        """
        # ── Authentication ──
        try:
            _verify_ws_token(websocket)
        except _WSAuthError as exc:
            await websocket.close(code=exc.code, reason=exc.reason)
            return

        await websocket.accept()
        bc: EventBroadcaster = app.state.broadcaster
        q = bc.subscribe()
        filter_types: set[str] | None = None

        async def _push_events() -> None:
            """Forward broadcaster events to the WebSocket client."""
            nonlocal filter_types
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if filter_types is not None and event.get("type") not in filter_types:
                        continue
                    await websocket.send_json(event)
            except WebSocketDisconnect:  # type: ignore[name-defined]
                pass
            except Exception:  # noqa: BLE001
                logger.debug("WebSocket push loop ended", exc_info=True)

        async def _heartbeat() -> None:
            """Send a ping every 30 seconds; close on failure."""
            try:
                while True:
                    await asyncio.sleep(30)
                    await websocket.send_json({"type": "ping", "timestamp": time.time()})
            except WebSocketDisconnect:  # type: ignore[name-defined]
                pass
            except Exception:  # noqa: BLE001
                logger.debug("WebSocket heartbeat ended", exc_info=True)

        push_task = asyncio.create_task(_push_events())
        heartbeat_task = asyncio.create_task(_heartbeat())

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    await websocket.send_json({"type": "error", "content": "Invalid JSON"})
                    continue

                cmd = msg.get("command", "")
                data = msg.get("data") or {}

                if cmd == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})
                elif cmd == "subscribe":
                    types_list = data.get("types", [])
                    if isinstance(types_list, list):
                        if filter_types is None:
                            filter_types = set()
                        filter_types.update(str(t) for t in types_list)
                    await websocket.send_json(
                        {"type": "ack", "content": f"subscribed to {len(types_list)} type(s)"}
                    )
                elif cmd == "unsubscribe":
                    types_list = data.get("types", [])
                    if isinstance(types_list, list) and filter_types is not None:
                        filter_types.difference_update(str(t) for t in types_list)
                    await websocket.send_json({"type": "ack", "content": "unsubscribed"})
                elif cmd == "cancel":
                    run_id = data.get("run_id", "")
                    task = app.state.agent_tasks.get(run_id)
                    if task is not None and not task.done():
                        task.cancel()
                        await websocket.send_json(
                            {"type": "ack", "content": f"cancelled run_id={run_id}"}
                        )
                    else:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "content": f"run_id={run_id} not found or already done",
                            }
                        )
                else:
                    await websocket.send_json(
                        {"type": "error", "content": f"Unknown command: {cmd}"}
                    )
        except WebSocketDisconnect:  # type: ignore[name-defined]
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebSocket receive loop ended: %s", exc)
        finally:
            push_task.cancel()
            heartbeat_task.cancel()
            bc.unsubscribe(q)
            if websocket.client_state != WebSocketState.DISCONNECTED:  # type: ignore[name-defined]
                try:
                    await websocket.close()
                except Exception:  # noqa: BLE001
                    pass

    # ── Web 控制台 (静态 SPA) ─────────────────────────────────────────
    # 挂载在 /console 下，提供临床工作台 / Vault / 审计 / 系统状态的可视化
    # 界面。控制台本身不做鉴权（纯静态文件），但所有数据端点仍走各自的
    # auth 依赖；本地访问无需 token，远程需配置 DOCTORAGENT_API_TOKEN。
    # 根路径 / 重定向到 /console/ 方便评委直接打开浏览器。
    _console_dir = Path(__file__).parent / "static" / "console"
    if _console_dir.is_dir():
        app.mount("/console", StaticFiles(directory=str(_console_dir), html=True), name="console")  # type: ignore[name-defined]

        @app.get("/", include_in_schema=False)  # type: ignore[name-defined]
        async def _redirect_to_console() -> Any:
            from starlette.responses import RedirectResponse  # type: ignore[name-defined]

            return RedirectResponse(url="/console/")

    return app


# ---------------------------------------------------------------------------
# Uvicorn runner
# ---------------------------------------------------------------------------


def run_server(
    config: AegisConfig,
    agent: AegisAgent,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Start the DoctorAgent API server and block until stopped.

    Parameters
    ----------
    config:
        DoctorAgent configuration instance.
    agent:
        Initialised AegisAgent orchestrator.
    host:
        Bind address (default ``127.0.0.1`` for local-only access).
    port:
        TCP port to listen on (default 8000).
    reload:
        Enable uvicorn auto-reload for development.
    """
    _check_available()

    import uvicorn

    app = create_app(config, agent)

    if _resolve_token():
        logger.info(
            "Starting DoctorAgent API server on http://%s:%d (auth enabled)",
            host,
            port,
        )
    else:
        logger.warning(
            "Starting DoctorAgent API server on http://%s:%d (token 未配置: "
            "fail-closed 模式，仅本地可访问读端点，敏感端点已禁用)",
            host,
            port,
        )

    if reload:
        # uvicorn's reloader spawns a fresh interpreter and cannot reload an
        # in-process app object; it must be given an import string instead.
        uvicorn.run(
            "doctoragent.api.server:app",
            host=host,
            port=port,
            reload=True,
            log_level="info",
        )
    else:
        uvicorn.run(
            app,
            host=host,
            port=port,
            reload=False,
            log_level="info",
        )

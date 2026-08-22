"""Configuration management for DoctorAgent."""

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ModelConfig(BaseSettings):
    """Model service configuration."""

    base_url: str = "http://127.0.0.1:11434/v1"
    model_name: str = "qwen3:8b"
    ctx_size: int = 32768
    temperature: float = 0.3
    timeout: float = 120.0
    fallback_model_name: str | None = None


class SecurityConfig(BaseSettings):
    """Security configuration."""

    kdf: str = "Argon2id"
    encryption: str = "AES-256-GCM"
    master_key_provider: str = "FilePassword"  # FilePassword | DPAPI | TPM
    master_key_password: str | None = None  # Only for FilePassword provider
    windows_hello_enabled: bool = False  # Require Windows Hello before unlocking
    enable_semantic_search: bool = False
    semantic_model: str = "all-MiniLM-L6-v2"
    # Embedding backend: "local" (sentence-transformers) or "openai" (any
    # OpenAI-compatible /v1/embeddings endpoint — TEI, Ollama, Infinity, …).
    semantic_backend: str = "local"
    semantic_embedding_base_url: str = ""  # e.g. http://127.0.0.1:8080/v1
    semantic_embedding_model: str = ""  # e.g. BAAI/bge-m3 (required for openai)
    semantic_embedding_api_key: str = ""  # optional; sent as Bearer when set
    cloud_fallback_enabled: bool = False
    # Paths whose audit-log entries should be basename-redacted. When non-empty
    # the AuditLogger redacts the ``_PATH_FIELDS`` (e.g. vault_path) to their
    # basename so usernames / directory structure are not leaked. An empty list
    # (default) leaves path fields unredacted for backwards compatibility.
    # Consumed via bool(...) in security/audit_log.py.
    audit_redact_paths: list[str] = []

    # ── Security features ────────────────────────────────────
    # Field-level encryption for structured metadata (summary, tags, …).
    # When enabled, FieldEncryptor encrypts eligible fields with per-field
    # AES-256-GCM keys derived from the master key via HKDF.
    enable_field_encryption: bool = False
    # Sandboxed execution for untrusted extractors/plugins.  Requires system
    # privileges (unshare on Linux, sandbox-exec on macOS); disabled by default.
    enable_sandbox: bool = False
    # Alert delivery: webhook URL and shared secret for HMAC-signed POSTs.
    # When set, the AlertManager forwards CRITICAL/WARNING alerts to this URL.
    alert_webhook_url: str | None = None  # never persisted
    alert_webhook_secret: str | None = None  # never persisted


class AutoKeyRotationConfig(BaseSettings):
    """Automatic master key rotation configuration.

    When ``enabled`` is True, ``AutoKeyRotator`` runs a background daemon
    thread that periodically checks whether the active master key has exceeded
    ``rotation_interval_days`` and, if so, performs a full rotation.  The
    previous vault key is retained for ``grace_period_days`` so existing vault
    files remain decryptable during the transition.

    An emergency rotation is triggered when the decrypt-failure counter crosses
    ``auto_rotate_on_failures_threshold``.
    """

    enabled: bool = False
    rotation_interval_days: int = 90
    grace_period_days: int = 7
    auto_rotate_on_failures_threshold: int = 5
    # Background poll interval in seconds (default: 1 hour).
    check_interval_seconds: float = 3600.0


class ComplianceConfig(BaseSettings):
    """GDPR / CCPA compliance configuration.

    ``retention_policies`` maps document categories to a retention period in
    days.  When ``enable_auto_expiry`` is True, ``ComplianceManager`` applies
    these policies on a schedule, marking or deleting expired documents.
    """

    enable_auto_expiry: bool = False
    # category -> retention days (e.g. {"invoice": 2555, "temporary": 30}).
    retention_policies: dict[str, int] = {}
    # When True, expired documents are deleted; when False they are only
    # quarantined for human review.
    auto_delete_expired: bool = False


class PathConfig(BaseSettings):
    """Path configuration."""

    inbox: Path = Path.home() / "DoctorAgent" / "Inbox"
    vault: Path = Path.home() / "DoctorAgent" / "Vault"
    index: Path = Path.home() / "DoctorAgent" / "Index"
    logs: Path = Path.home() / "DoctorAgent" / "Logs"
    connections: Path = Path.home() / "DoctorAgent" / "Config" / "connections.json"
    settings: Path = Path.home() / "DoctorAgent" / "Config" / "settings.json"


class ResourcesConfig(BaseSettings):
    """Resource limits and backpressure configuration (Phase 6.6).

    These knobs protect the agent from accepting work faster than it can
    process it and from filling the disk holding the Vault. Defaults are
    conservative so that existing single-file workloads behave exactly as
    before; the limits only engage under sustained load.
    """

    # Maximum number of files processed concurrently end-to-end.
    max_inflight_files: int = 8
    # Maximum number of concurrent LLM classification calls.
    classify_concurrency: int = 4
    # Pause ingestion when this many events are queued (scheduled but not
    # yet started). Set to 0 to disable backlog protection.
    inbox_backlog_high_watermark: int = 1000
    # Resume ingestion once the queued backlog drains to this level.
    inbox_backlog_low_watermark: int = 800
    # Emit a disk watermark alert when Vault device usage reaches this percent.
    disk_watermark_percent: float = 90.0


class IntegrationsConfig(BaseSettings):
    """Ecosystem integration configuration (Phase 7).

    Three surfaces:

    * ``webhooks`` — outbound event delivery (Phase 7.2). Endpoints receive
      an HMAC-SHA256 signed JSON POST for each subscribed event type.
    * ``storage`` — remote storage backends (Phase 7.3). Used for encrypted
      offsite backup and remote sync targets.

    Secrets (``webhook_secret``, ``s3_secret_key``, ``webdav_password``)
    are never persisted by :meth:`AegisConfig.save_to_file` because they
    live in the ``integrations`` block which is filtered the same way as
    ``security``. Operators set them via environment variables instead.
    """

    # ── Webhooks (7.2) ─────────────────────────────────────────────────
    webhooks_enabled: bool = False
    # List of {"url": "...", "events": ["classified", ...], "secret": "..."}
    # Entries without a secret use ``webhook_default_secret``.
    webhook_endpoints: list[dict[str, Any]] = []
    webhook_default_secret: str | None = None
    # Max delivery attempts per event (exponential backoff between retries).
    webhook_max_retries: int = 3
    # Per-request timeout in seconds.
    webhook_timeout_seconds: float = 10.0

    # ── Storage backends (7.3) ─────────────────────────────────────────
    storage_enabled: bool = False
    # Which backend to use for backup: "s3" | "webdav" | "local" (default).
    storage_backup_backend: str = "local"
    # S3 / MinIO settings. ``s3_endpoint`` empty → AWS S3 default.
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None  # never persisted
    s3_use_path_style: bool = True  # required for MinIO; harmless for AWS
    # WebDAV settings.
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: str | None = None  # never persisted
    # Prefix applied to all remote objects (lets one bucket host multiple vaults).
    storage_key_prefix: str = "doctoragent/"

    # ── External MCP servers (M4.16) ───────────────────────────────────
    # Each entry is a dict describing an external MCP server to connect to at
    # startup and import its tools into the agent's registry:
    #   {
    #     "name": "pubmed",                     # used to namespace imported tools
    #     "transport": "stdio" | "http",
    #     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-..."] (stdio),
    #     "url": "http://host:port/mcp"          (http),
    #     "http_headers": {"Authorization": "Bearer ..."} (http, optional),
    #     "prefix": "pubmed_"                    # optional name prefix
    #   }
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)


class HooksConfig(BaseSettings):
    """Pre/Post-Consume hook configuration.

    Hooks are external scripts or Python callables that run at well-defined
    points in the processing pipeline:

    * **Pre-consume** — before classification; can modify the classification
      result or reject a file (by raising).
    * **Post-consume** — after the file has been encrypted and indexed; used
      for notifications, external system updates, etc.

    Each hook receives ``file_path``, ``classification_result``, and
    ``vault_path``. Hook failures are isolated: a failing hook logs a
    warning but does not abort the main pipeline (unless the hook raises
    a :class:`SystemExit`).
    """

    pre_consume_hooks: list[str] = []
    post_consume_hooks: list[str] = []


class ClinicalConfig(BaseSettings):
    """Clinical decision-support configuration.

    Wires the clinical layer's external endpoints (FHIR R4 server, SMART-on-FHIR
    issuer, openFDA / RxNorm / PubMed knowledge sources, SNOMED CT Snowstorm
    terminology server) so operators can point the agent at their own
    infrastructure without editing source code.

    All fields are optional — when ``fhir_base_url`` is empty the clinical
    workflow runs off the patient_context supplied in the request (or the
    synthetic fixtures), exactly like before. Knowledge-source URLs default to
    the public NLM / FDA / NCBI endpoints; override only for on-premise mirrors.
    """

    # ── FHIR R4 server ────────────────────────────────────────────────
    # Empty → no FHIR client is constructed; /clinical/analyze runs on the
    # patient_context in the request body. Set to a real FHIR endpoint
    # (e.g. http://hapi-fhir:8080/fhir) to enable live FHIR reads.
    fhir_base_url: str = ""
    # Bearer token for the FHIR server (SMART-on-FHIR access_token or a static
    # API key). Leave empty for unauthenticated FHIR servers (e.g. public HAPI).
    fhir_auth_token: str | None = None
    fhir_timeout_seconds: float = 30.0
    # SMART-on-FHIR launch issuer (e.g. https://fhir-ehr-code.cerner.com).
    # When set, the SMART launch flow (smart.py) can discover auth endpoints.
    smart_issuer: str = ""
    smart_client_id: str = ""
    smart_client_secret: str | None = None  # never persisted to settings.json

    # ── Knowledge sources ─────────────────────────────────────────────
    # Defaults point at the public NLM / FDA / NCBI endpoints. Override for
    # on-premise mirrors or air-gapped deployments.
    openfda_base_url: str = "https://api.fda.gov"
    rxnorm_base_url: str = "https://rxnorm.nlm.nih.gov"
    pubmed_base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    # ── Terminology ───────────────────────────────────────────────────
    # Snowstorm SNOMED CT server. Public default is rate-limited; production
    # deployments should run their own Snowstorm instance.
    snowstorm_base_url: str = "https://browser.ihtsdotools.org/snowstorm/snomedct"
    # Optional paths to bulk ICD-10 / LOINC tables (CSV/TSV). When set the
    # terminology service loads them at startup instead of the curated maps.
    icd10_table_path: str = ""
    loinc_table_path: str = ""


class A2AConfig(BaseSettings):
    """A2A (Agent-to-Agent) protocol configuration (Google A2A).

    Controls whether DoctorAgent exposes an A2A Agent Card and JSON-RPC task
    server, and pre-registers remote agent base URLs that the orchestrator may
    delegate subtasks to. See ``doctoragent/a2a/``.
    """

    # Expose this agent over A2A: serves /.well-known/agent.json + /a2a/rpc.
    # Defaults to off: an unconfigured deployment must not silently accept
    # remote task submissions; opt in explicitly.
    enabled: bool = False
    agent_name: str = "DoctorAgent"
    agent_description: str = (
        "Clinical decision-support agent: deterministic safety rules + "
        "LLM/RAG literature reasoning, FHIR/CDS Hooks integrations."
    )
    base_url: str = "http://127.0.0.1:8000"
    # auth_type exposed on the Agent Card ("none" | "bearer").
    auth_type: str = "none"
    # Remote agents (base URLs) this agent may delegate tasks to.
    peer_agents: list[str] = Field(default_factory=list)
    # Optional bearer token map: peer base URL -> token.
    bearer_tokens: dict[str, str] = Field(default_factory=dict)
    # Client timeout for outbound A2A calls (seconds).
    timeout_seconds: float = 30.0


class VoiceConfig(BaseSettings):
    """Voice conversation configuration (ASR + TTS).

    Both layers plug into any OpenAI-compatible endpoint so a single gateway
    (e.g. Ollama + a speech model, or a cloud provider) can serve both. When no
    endpoint is configured the voice API is disabled (endpoints return 501).
    """

    enabled: bool = True
    # Transcription (speech-to-text) — OpenAI /v1/audio/transcriptions.
    transcribe_base_url: str = ""
    transcribe_model: str = ""
    transcribe_api_key: str = ""
    # Synthesis (text-to-speech) — OpenAI /v1/audio/speech.
    tts_base_url: str = ""
    tts_model: str = ""
    tts_voice: str = "alloy"
    tts_api_key: str = ""
    # Max upload size for an audio clip (bytes).
    max_audio_bytes: int = 10 * 1024 * 1024


class AegisConfig(BaseSettings):
    """Global application settings."""

    model_config = SettingsConfigDict(
        env_prefix="DOCTORAGENT_",
        env_nested_delimiter="__",
    )

    app_name: str = "DoctorAgent"
    # Runtime profile: dev (permissive) | staging (prod-like, warnings) |
    # prod (strict — validate_environment() enforces hard requirements).
    env: str = "dev"
    debug: bool = False
    # mDNS/UDP device discovery defaults to off: when enabled it broadcasts
    # this host's presence on the LAN; users opt in only when multi-device
    # sync is needed.
    discovery_enabled: bool = False
    # ── RAG vector backend ────────────────────────────────────────
    # "sqlite" keeps the legacy inline dense path (vectors in vault_chunks).
    # "chroma" dual-writes chunk embeddings into a Chroma persistent store
    # and serves dense queries from it; SQLite remains the metadata source
    # of truth. Requires the ``chromadb`` package when set to "chroma".
    rag_vector_backend: str = "sqlite"
    rag_vector_backend_path: str = ""  # empty → <index>/vectorstore
    model: ModelConfig = Field(default_factory=ModelConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    paths: PathConfig = Field(default_factory=PathConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    # Security: automatic key rotation and GDPR/CCPA compliance.
    auto_key_rotation: AutoKeyRotationConfig = Field(default_factory=AutoKeyRotationConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    clinical: ClinicalConfig = Field(default_factory=ClinicalConfig)
    a2a: A2AConfig = Field(default_factory=A2AConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    def validate_environment(self) -> list[str]:
        """Return a list of environment-specific configuration problems.

        An empty list means OK. Only ``prod`` enforces hard security
        requirements; ``dev`` and ``staging`` are permissive (callers may
        still surface these as warnings).
        """
        problems: list[str] = []
        if self.env != "prod":
            return problems
        if (
            self.security.master_key_provider == "FilePassword"
            and not self.security.master_key_password
        ):
            problems.append(
                "prod requires DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD when "
                "master_key_provider=FilePassword"
            )
        if self.security.cloud_fallback_enabled:
            problems.append("prod forbids cloud_fallback_enabled=true")
        if self.auto_key_rotation.enabled and self.auto_key_rotation.rotation_interval_days > 90:
            problems.append("prod requires key rotation interval <= 90 days")
        return problems

    def save_to_file(self, path: Path | None = None) -> None:
        """Serialize the current configuration to *path* as JSON."""
        target = path or self.paths.settings
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        # Never persist secrets to disk in plaintext.
        security = data.get("security", {})
        security.pop("master_key_password", None)
        # Alert webhook secret is also stripped so a saved settings file
        # never contains outbound alert-delivery credentials.
        security.pop("alert_webhook_secret", None)
        # Integration secrets (webhook shared secret, S3 secret key, WebDAV
        # password) are stripped per-entry so a saved settings file never
        # contains outbound credentials.
        integrations = data.get("integrations", {})
        integrations.pop("webhook_default_secret", None)
        for endpoint in integrations.get("webhook_endpoints", []) or []:
            endpoint.pop("secret", None)
        integrations.pop("s3_secret_key", None)
        integrations.pop("webdav_password", None)
        # Clinical secrets: SMART client secret and FHIR bearer token carry
        # EHR access credentials and must never be persisted to settings.json.
        clinical = data.get("clinical", {})
        clinical.pop("smart_client_secret", None)
        clinical.pop("fhir_auth_token", None)
        content = json.dumps(data, indent=2, default=str)
        tmp_path = target.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(target)

    @classmethod
    def load_from_file(cls, path: Path | None = None) -> "AegisConfig":
        """Load configuration from *path*, falling back to defaults.

        If the file is missing or contains invalid JSON, a default configuration
        is returned and a warning is logged.
        """
        target = path or (Path.home() / "DoctorAgent" / "Config" / "settings.json")
        if not target.exists():
            logger.info("Settings file not found at %s; using defaults", target)
            return cls()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load settings from %s: %s; using defaults",
                target,
                exc,
            )
            return cls()
        return cls(**data)

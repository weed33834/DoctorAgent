"""Agent orchestrator using the processing pipeline."""

import asyncio
import concurrent.futures
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from doctoragent.api.schemas import FileEvent, SearchQuery, SearchResult, TaskStatus
from doctoragent.compat import UTC
from doctoragent.config import AegisConfig
from doctoragent.connections.manager import ConnectionManager
from doctoragent.execution.inbox_watcher import InboxWatcher
from doctoragent.execution.vault import VaultManager
from doctoragent.model.classifier import Classifier
from doctoragent.model.embedding import LocalEmbeddingProvider, SentenceTransformersProvider
from doctoragent.model.rag import BM25Search  # 延迟初始化，避免重复 import
from doctoragent.orchestration.pipeline import ProcessingPipeline
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.security.audit_log import AlertManager, AuditLogger
from doctoragent.security.master_key import (
    AutoKeyRotator,
    MasterKeyProvider,
    create_master_key_provider,
    should_rotate_key,
)
from doctoragent.security.resources import BackpressureGuard

logger = logging.getLogger(__name__)


class AegisAgent:
    """Main agent orchestrator."""

    def __init__(
        self,
        config: AegisConfig,
        connection_manager: ConnectionManager | None = None,
        task_store: TaskStore | None = None,
        classifier: Classifier | None = None,
        master_key_provider: MasterKeyProvider | None = None,
        vault_manager: VaultManager | None = None,
        watcher: InboxWatcher | None = None,
        audit_logger: AuditLogger | None = None,
        embedding_provider: LocalEmbeddingProvider | None = None,
        notifier: object | None = None,
    ) -> None:
        self.config = config
        self.connection_manager = connection_manager or ConnectionManager(config.paths.connections)
        self.task_store = task_store or TaskStore(config.paths.index / "tasks.db")
        # allow_cloud_fallback=True so that connections explicitly flagged
        # is_cloud_authorized=True are used when no trusted local connection
        # is available. The is_cloud_authorized flag is the user's explicit
        # opt-in to cloud processing, so honoring it here matches user intent
        # while keeping the default fail-closed behavior for unauthorized
        # cloud connections.
        self.classifier = classifier or Classifier.from_manager(
            self.connection_manager,
            allow_cloud_fallback=True,
        )
        self.audit_logger = audit_logger
        self._notifier = notifier
        # Create and attach AlertManager for real alert delivery (desktop
        # notifications + webhook).  When an AuditLogger is present the
        # AlertManager gains rate limiting, aggregation and history on top
        # of the legacy callback fan-out.
        self._alert_manager: AlertManager | None = None
        if self.audit_logger is not None:
            self._setup_alert_manager()
            # Construction-time marker only: this is *not* an authentication
            # event. The agent is created without performing any credential
            # check, so emitting "login_attempt" would mislead audit consumers.
            self.audit_logger.log(
                "agent_initialized",
                {"component": "AegisAgent"},
            )
        self.master_key_provider = master_key_provider or create_master_key_provider(
            config.security.master_key_provider,
            config.paths.connections.parent / "master_key.bin",
            password=config.security.master_key_password,
        )
        # Auto key rotation background thread.  When enabled, a daemon thread
        # periodically checks key age and performs progressive rotation with
        # a grace-key window.  When disabled, fall back to the log-only
        # check so existing behaviour is preserved.
        self._key_rotator: AutoKeyRotator | None = None
        self._embedding_provider = self._create_embedding_provider(embedding_provider)
        vault_key = self.master_key_provider.get_key()
        # Start the auto key rotator when enabled; otherwise fall back to
        # the log-only rotation-due check.
        if self.config.auto_key_rotation.enabled:
            self._start_auto_key_rotation(vault_key)
        else:
            self._check_key_rotation_due(self.config.paths.connections.parent)
        self.pipeline = ProcessingPipeline(
            config=config,
            classifier=self.classifier,
            task_store=self.task_store,
            vault_key=vault_key,
            vault_manager=vault_manager,
            audit_logger=self.audit_logger,
            embedding_provider=self._embedding_provider,
        )
        self.watcher = watcher
        self._loop: asyncio.AbstractEventLoop | None = None
        # Resource governance (Phase 6.6): cap concurrent processing and
        # pause ingestion when the Inbox backlog grows too large.
        self._backpressure = BackpressureGuard(
            config.resources.inbox_backlog_high_watermark,
            config.resources.inbox_backlog_low_watermark,
        )
        self._inflight_sem = asyncio.Semaphore(config.resources.max_inflight_files)
        self._watcher_paused = False
        # Phase 7.2: outbound webhook dispatcher. Lazily constructed via
        # :meth:`attach_webhooks` so the agent remains usable without the
        # integrations package configured. When set, the audit logger's
        # security-alert channel is bridged into webhook delivery.
        self._webhook_dispatcher: object | None = None

    def attach_webhooks(self, dispatcher: object) -> None:
        """Wire an outbound webhook dispatcher into the agent.

        Bridges the audit logger's security-alert channel into *dispatcher*
        so CRITICAL/HIGH/MEDIUM alerts are forwarded to subscribed webhook
        endpoints. Idempotent: calling twice re-registers the bridge (the
        dispatcher de-duplicates delivery per endpoint).
        """
        from doctoragent.integrations.webhooks import attach_security_alert_webhook

        self._webhook_dispatcher = dispatcher
        if self.audit_logger is not None:
            attach_security_alert_webhook(self.audit_logger, dispatcher)  # type: ignore[arg-type]

    @property
    def webhook_dispatcher(self) -> object | None:
        """Return the attached webhook dispatcher, if any."""
        return self._webhook_dispatcher

    @property
    def llm_provider(self) -> Any:
        """The active LLM provider backing the classifier, or ``None``.

        Exposed so the clinical workflow (``/clinical/analyze``), CDS Hooks,
        and other API endpoints can reach the provider without poking at
        ``classifier.provider`` internals. Mirrors the attribute name
        (``llm_provider`` / ``_llm_provider``) those endpoints already look
        up via ``getattr`` — wiring this property fixes the previously
        degraded rules-only mode on a stock :class:`AegisAgent`.
        """
        if self.classifier is not None and hasattr(self.classifier, "provider"):
            return self.classifier.provider
        return None

    @property
    def _llm_provider(self) -> Any:
        """Back-compat alias for code paths that read ``agent._llm_provider``.

        ``server.py`` historically probes ``_llm_provider`` first, then
        ``llm_provider``. Defining both here means either lookup resolves to
        :attr:`llm_provider` so the clinical workflow always receives the
        provider instead of silently degrading.
        """
        return self.llm_provider

    def dispatch_webhook(self, event_type: str, payload: dict[str, object]) -> int:
        """Forward an event to attached webhooks. No-op if none attached.

        Returns the number of endpoints attempted. Used by the pipeline
        (``classified``) and sync engine (``sync_*``) to surface events
        without those components depending on the integrations package.
        """
        if self._webhook_dispatcher is None:
            return 0
        dispatch = getattr(self._webhook_dispatcher, "dispatch", None)
        if dispatch is None:
            return 0
        try:
            return int(dispatch(event_type, payload))
        except Exception:  # pragma: no cover — dispatch never raises, but guard
            logger.exception("Webhook dispatch for %s failed", event_type)
            return 0

    def _check_key_rotation_due(self, config_dir: Path) -> None:
        """Log a warning if the master key is older than the recommended rotation age."""
        key_file = config_dir / "master_key.bin"
        marker_file = config_dir / ".rotation_marker"
        try:
            creation_time: datetime | None = None
            # Prefer the rotation marker file which stores the real creation
            # timestamp; fall back to the key file mtime for legacy installs.
            if marker_file.exists():
                try:
                    raw = marker_file.read_text(encoding="utf-8").strip()
                    creation_time = datetime.fromisoformat(raw)
                    if creation_time.tzinfo is None:
                        creation_time = creation_time.replace(tzinfo=UTC)
                except (ValueError, OSError):
                    creation_time = None
            if creation_time is None and key_file.exists():
                mtime = key_file.stat().st_mtime
                creation_time = datetime.fromtimestamp(mtime, tz=UTC)
            if creation_time is None:
                return

            if should_rotate_key(creation_time):
                logger.warning(
                    "Master key is older than 90 days and should be rotated. "
                    "Use rotate_master_key() to perform the rotation."
                )
                if self.audit_logger is not None:
                    self.audit_logger.log(
                        "policy_violation",
                        {
                            "reason": "master_key_rotation_recommended",
                            "key_age_days": str((datetime.now(UTC) - creation_time).days),
                        },
                    )
        except OSError:
            logger.debug("Could not check master key age: %s", key_file, exc_info=True)

    def _setup_alert_manager(self) -> None:
        """Create an :class:`AlertManager` and attach it to the audit logger.

        The AlertManager receives a desktop notifier (the one passed to the
        agent, or a freshly created :class:`DesktopNotifier`) and optional
        webhook credentials from the security config.  Once attached, every
        alert fired by the :class:`AuditLogger` is also routed through the
        manager for rate-limited delivery and history tracking.
        """
        assert self.audit_logger is not None
        notifier = self._notifier
        if notifier is None:
            try:
                from doctoragent.connections.notifications import DesktopNotifier

                notifier = DesktopNotifier()
            except Exception:
                notifier = None
        self._alert_manager = AlertManager(
            notifier=notifier,
            webhook_url=self.config.security.alert_webhook_url,
            webhook_secret=self.config.security.alert_webhook_secret,
            audit_logger=self.audit_logger,
        )
        self.audit_logger.alert_manager = self._alert_manager

    def _start_auto_key_rotation(self, vault_key: bytes) -> None:
        """Create and start the :class:`AutoKeyRotator` background thread.

        The rotator's factory generates a provider with fresh key material:
        for FilePasswordProvider a random password is used so the derived key
        differs from the current one; for hardware-backed providers a new
        storage path ensures a new random key is generated.
        """
        import os
        import time

        config_dir = self.config.paths.connections.parent
        storage_path = config_dir / "master_key.bin"
        provider_name = self.config.security.master_key_provider
        password = self.config.security.master_key_password

        def _factory() -> MasterKeyProvider:
            if provider_name.lower() == "filepassword":
                # A random password guarantees a different derived key even
                # when the persistent salt is reused.
                new_password = os.urandom(32).hex()
                return create_master_key_provider(
                    provider_name, storage_path, password=new_password
                )
            # For DPAPI/TPM/Keychain: use a new storage path so a fresh
            # random key is generated by the provider.
            new_path = storage_path.with_suffix(f".{int(time.time())}.bin")
            return create_master_key_provider(provider_name, new_path, password=password)

        self._key_rotator = AutoKeyRotator(
            current_provider=self.master_key_provider,
            new_provider_factory=_factory,
            vault_key=vault_key,
            storage_path=storage_path,
            vault_dir=self.config.paths.vault,
            audit_logger=self.audit_logger,
            rotation_interval_days=self.config.auto_key_rotation.rotation_interval_days,
            grace_period_days=self.config.auto_key_rotation.grace_period_days,
            check_interval_seconds=self.config.auto_key_rotation.check_interval_seconds,
            auto_rotate_on_failures_threshold=self.config.auto_key_rotation.auto_rotate_on_failures_threshold,
        )
        self._key_rotator.start()
        logger.info(
            "Auto key rotator started (interval=%d days, grace=%d days)",
            self.config.auto_key_rotation.rotation_interval_days,
            self.config.auto_key_rotation.grace_period_days,
        )

    @property
    def key_rotator(self) -> AutoKeyRotator | None:
        """Return the active :class:`AutoKeyRotator`, if any."""
        return self._key_rotator

    @property
    def alert_manager(self) -> AlertManager | None:
        """Return the attached :class:`AlertManager`, if any."""
        return self._alert_manager

    def _create_embedding_provider(
        self,
        injected: LocalEmbeddingProvider | None,
    ) -> LocalEmbeddingProvider | None:
        """Resolve the embedding provider, falling back to FTS if unavailable."""
        if injected is not None:
            return injected
        if not self.config.security.enable_semantic_search:
            return None
        try:
            return SentenceTransformersProvider(self.config.security.semantic_model)
        except Exception:
            logger.warning(
                "Semantic search is enabled but the embedding provider is unavailable. "
                "Falling back to full-text search.",
                exc_info=True,
            )
            return None

    async def on_file_event(self, event: FileEvent) -> TaskStatus:
        """Handle a new file event end-to-end."""
        status = await self.pipeline.process(event)
        # Phase 7.2: surface classification-complete events to webhooks.
        # Only dispatched when a dispatcher is attached; otherwise a no-op.
        if status.state == "COMPLETED" and self._webhook_dispatcher is not None:
            record = self.task_store.get(status.task_id) or {}
            self.dispatch_webhook(
                "classified",
                {
                    "task_id": str(status.task_id),
                    "source_path": str(event.source_path),
                    "category": record.get("category", ""),
                    "sensitivity": record.get("sensitivity", ""),
                    "vault_path": record.get("vault_path"),
                },
            )
        return status

    def _on_file_event_sync(self, event: FileEvent) -> None:
        """Schedule async file processing on the running event loop."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            logger.warning("No event loop configured; dropping file event %s", event.event_id)
            return
        # Track queued work and pause ingestion when the backlog high
        # watermark is crossed. The pause runs on the loop thread to avoid
        # joining the watchdog dispatcher from within its own thread.
        if self._backpressure.on_schedule():
            loop.call_soon_threadsafe(self._pause_watcher)
        future = asyncio.run_coroutine_threadsafe(self._handle_event(event), loop)
        future.add_done_callback(self._on_schedule_done)

    @staticmethod
    def _on_schedule_done(future: concurrent.futures.Future[None]) -> None:
        """Capture exceptions from scheduled coroutines so they don't vanish."""
        try:
            future.result()
        except Exception:
            logger.exception("Scheduled file event processing failed")

    async def _handle_event(self, event: FileEvent) -> None:
        """Process a file event and log failures."""
        should_resume = False
        async with self._inflight_sem:
            self._backpressure.on_start()
            try:
                await self.on_file_event(event)
                if self._notifier is not None and hasattr(
                    self._notifier, "notify_classification_done"
                ):
                    self._notifier.notify_classification_done(str(event.source_path.name))
            except Exception as exc:
                logger.exception("Failed to process file event %s", event.event_id)
                if self._notifier is not None and hasattr(self._notifier, "notify_security_alert"):
                    self._notifier.notify_security_alert(
                        "processing_failure",
                        f"Failed to process '{event.source_path.name}': {exc}",
                    )
            finally:
                should_resume = self._backpressure.on_done()
        if should_resume:
            self._resume_watcher()

    def _pause_watcher(self) -> None:
        """Pause Inbox ingestion because the backlog high watermark was reached.

        Runs on the event loop thread (scheduled via ``call_soon_threadsafe``)
        so the watchdog observer thread is never asked to join itself.
        """
        if self._watcher_paused:
            return
        self._watcher_paused = True
        logger.warning(
            "Inbox backlog reached high watermark (%d pending); pausing ingestion",
            self._backpressure.pending,
        )
        if self.audit_logger is not None:
            self.audit_logger.log(
                "resource_backpressure",
                {"action": "pause", "pending": self._backpressure.pending},
            )
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:
                logger.exception("Failed to pause Inbox watcher")

    def _resume_watcher(self) -> None:
        """Resume Inbox ingestion once the backlog has drained."""
        if not self._watcher_paused:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            # Loop torn down while paused; leave paused until monitoring restarts.
            return
        self._watcher_paused = False
        logger.info("Inbox backlog drained; resuming ingestion")
        if self.audit_logger is not None:
            self.audit_logger.log(
                "resource_backpressure",
                {"action": "resume", "pending": self._backpressure.pending},
            )
        if self.watcher is not None:
            try:
                self.watcher.start()
            except Exception:
                logger.exception("Failed to resume Inbox watcher")
                self._watcher_paused = True

    def start_monitoring(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start watching the configured Inbox directory."""
        self._loop = loop or asyncio.get_running_loop()
        if self.watcher is None:
            self.watcher = InboxWatcher(self.config.paths.inbox, self._on_file_event_sync)
        self.watcher.start()
        if self.audit_logger is not None and self._loop is not None:
            # Use create_task instead of run_coroutine_threadsafe for cleaner
            # scheduling when we already have a loop reference.  loop.create_task
            # works whether or not the loop is currently running.
            self._loop.create_task(self._test_connection())

    def stop_monitoring(self) -> None:
        """Stop watching the Inbox directory."""
        # Join the observer thread before clearing the loop reference so that
        # any in-flight callbacks complete before the loop becomes unusable.
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self._watcher_paused = False
        self._loop = None

    async def _test_connection(self) -> None:
        """Test the active classifier connection and audit the result."""
        if self.audit_logger is None:
            return
        try:
            healthy = await self.classifier.provider.health()
            self.audit_logger.log(
                "connection_tested",
                {
                    "connection": self.classifier.connection.name,
                    "healthy": healthy,
                },
            )
        except Exception as exc:
            self.audit_logger.log(
                "connection_tested",
                {
                    "connection": self.classifier.connection.name,
                    "healthy": False,
                    "error": str(exc),
                },
            )

    def resume_incomplete(self) -> list[TaskStatus]:
        """Rebuild in-memory state for tasks that were incomplete at startup.

        Calls ``task_store.load_incomplete()`` and reconstructs task statuses
        from the persisted state.  Tasks in the FAILED state are transitioned
        to IDLE so they can be retried.
        """
        incomplete = self.task_store.load_incomplete()
        resumed: list[TaskStatus] = []
        for record in incomplete:
            task_id = UUID(record["task_id"])
            state_str = record.get("state", TaskState.IDLE.name)
            try:
                state = TaskState[state_str]
            except KeyError:
                state = TaskState.IDLE
            # FAILED tasks can transition to IDLE (per the state machine) to
            # support retry.
            if state == TaskState.FAILED:
                self.task_store.update_state(task_id, TaskState.IDLE, "resumed for retry")
                state = TaskState.IDLE
            resumed.append(
                TaskStatus(
                    task_id=task_id,
                    state=state.name,
                    message=str(record.get("message", "")),
                )
            )
        return resumed

    def get_status(self, task_id: UUID) -> TaskStatus | None:
        """Fetch task status from the store."""
        record = self.task_store.get(task_id)
        if record is None:
            return None
        return TaskStatus(
            task_id=task_id,
            state=str(record["state"]),
            message=str(record.get("message", "")),
        )

    async def search(self, query: SearchQuery) -> list[SearchResult]:
        """Search vault metadata by keywords, chunk content, and semantic similarity.

        When ``--semantic`` is explicitly requested, and an embedding provider
        is available, the search uses the weighted hybrid fusion from
        ``TaskStore.hybrid_search``.  Otherwise it falls back to FTS on
        metadata AND chunk-level BM25 content search, merging results.
        """
        if query.semantic and self._embedding_provider is not None:
            provider = self._embedding_provider

            def _hybrid() -> list[SearchResult]:
                return self.task_store.hybrid_search(
                    query=query.query,
                    top_k=query.top_k,
                    fts_weight=0.3,
                    semantic_weight=0.7,
                    provider=provider,
                )

            return await asyncio.to_thread(_hybrid)

        # ── Non-semantic: merge metadata FTS + chunk BM25 content search ──
        def _merged_search() -> list[SearchResult]:
            meta_results = self.task_store.search(query.query, top_k=query.top_k)
            try:
                bm25 = BM25Search(self.task_store.db_path, self.task_store._tenant_id)
                chunk_hits = bm25.search(query.query, top_k=query.top_k)
            except Exception:
                chunk_hits = []
            # 合并：chunk 命中优先（text 字段有内容），去重 by vault_path
            seen: set[str] = set()
            merged: list[SearchResult] = []
            for ch in chunk_hits:
                vp = ch.get("vault_path", "")
                if vp and vp not in seen:
                    seen.add(vp)
                    merged.append(SearchResult(
                        vault_path=Path(vp),
                        category=ch.get("category", ""),
                        summary=ch.get("text", ch.get("summary", "")),
                        score=ch.get("score", 1.0),
                        text=ch.get("text", ""),
                    ))
            for mr in meta_results:
                key = str(mr.vault_path)
                if key and key not in seen:
                    seen.add(key)
                    merged.append(mr)
            return merged[: query.top_k]

        return await asyncio.to_thread(_merged_search)

    async def delegate(self, task: str, role: str) -> str:
        """Delegate a task to this agent in a specialist role.

        Uses the classifier's LLM provider (same path as document classification,
        proven working with HCNSEC gateway) to generate a response in the
        persona of the named specialist role.
        """
        provider = getattr(self.classifier, "provider", None)
        if provider is None:
            return "No LLM provider configured. Add a connection first."

        system_prompt = (
            f"You are a {role}. "
            "Provide a concise, professional response to the task below."
        )
        try:
            raw = await provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ],
            )
            if hasattr(raw, "content"):
                return str(raw.content).strip()
            if hasattr(raw, "choices") and raw.choices:
                return str(raw.choices[0].message.content).strip()
            return str(raw).strip() if raw else ""
        except Exception as e:
            return f"[delegation error: {e}]"

    async def aclose(self) -> None:
        """Clean up resources."""
        # Stop the auto key rotator background thread first so it does not
        # fire a rotation while other components are shutting down.
        if self._key_rotator is not None:
            self._key_rotator.stop()
            self._key_rotator = None
        self.stop_monitoring()
        if hasattr(self.classifier, "provider") and hasattr(self.classifier.provider, "close"):
            try:
                await self.classifier.provider.close()
            except Exception:
                pass

    async def __aenter__(self) -> "AegisAgent":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

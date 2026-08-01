"""End-to-end file processing pipeline: Inbox -> Classify -> Encrypt -> Vault."""

import asyncio
import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

from doctoragent.api.schemas import (
    ClassificationResult,
    EncryptResult,
    FileEvent,
    TaskStatus,
)
from doctoragent.config import AegisConfig
from doctoragent.execution.vault import VaultManager
from doctoragent.model.classifier import Classifier
from doctoragent.model.embedding import LocalEmbeddingProvider
from doctoragent.model.extractors import ExtractionManager
from doctoragent.model.rag import SemanticChunker
from doctoragent.orchestration.state_machine import StateMachine, TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.policy import SecurityPolicyError, require_trusted_local_connection
from doctoragent.security.resources import DiskWatermarkChecker

logger = logging.getLogger(__name__)

# Chunk size (1 MB) used when overwriting files during secure delete.
_SECURE_DELETE_CHUNK = 1024 * 1024


class HookExecutor:
    """Execute Pre/Post-Consume hooks with error isolation.

    Hooks are configured as script paths (strings). Each hook script
    receives the hook context as JSON via ``stdin`` and may return a
    modified classification result (pre-consume only) as JSON on ``stdout``.

    Hook failures are **isolated**: a failing or non-existent hook logs a
    warning but does not abort the main pipeline. This ensures that a
    misconfigured external script never prevents a file from being
    processed.

    Hook context JSON schema::

        {
            "file_path": "/path/to/source/file",
            "classification": { ... },   # ClassificationResult dict or null
            "vault_path": "/path/to/vault/file"  # or null for pre-consume
        }

    For pre-consume hooks, if the script writes valid JSON to stdout
    containing classification fields (``sensitivity``, ``category``,
    ``disguise_name``, ``disguise_extension``), the returned dict is used
    to override the classification result.
    """

    def __init__(
        self,
        pre_hooks: list[str] | None = None,
        post_hooks: list[str] | None = None,
    ) -> None:
        self._pre_hooks = pre_hooks or []
        self._post_hooks = post_hooks or []

    @property
    def has_pre_hooks(self) -> bool:
        return bool(self._pre_hooks)

    @property
    def has_post_hooks(self) -> bool:
        return bool(self._post_hooks)

    def run_pre_consume(
        self,
        file_path: Path,
        classification: ClassificationResult | None,
        vault_path: Path | None = None,
    ) -> ClassificationResult | None:
        """Run pre-consume hooks.

        Returns a (possibly modified) :class:`ClassificationResult` when a
        hook outputs valid classification JSON, or ``None`` when no hook
        modified the result. Hook errors are logged and swallowed.
        """
        modified: ClassificationResult | None = None
        context = self._build_context(file_path, classification, vault_path)
        for hook_path in self._pre_hooks:
            try:
                output = self._execute_hook(hook_path, context)
                if output is not None and modified is None:
                    parsed = self._try_parse_classification(output)
                    if parsed is not None:
                        modified = parsed
            except Exception as exc:  # noqa: BLE001 — error isolation
                logger.warning("Pre-consume hook %s failed: %s", hook_path, exc)
        return modified

    def run_post_consume(
        self,
        file_path: Path,
        classification: ClassificationResult,
        vault_path: Path,
    ) -> None:
        """Run post-consume hooks.

        Hook errors are logged and swallowed so they never prevent the
        pipeline from completing.
        """
        context = self._build_context(file_path, classification, vault_path)
        for hook_path in self._post_hooks:
            try:
                self._execute_hook(hook_path, context)
            except Exception as exc:  # noqa: BLE001 — error isolation
                logger.warning("Post-consume hook %s failed: %s", hook_path, exc)

    @staticmethod
    def _build_context(
        file_path: Path,
        classification: ClassificationResult | None,
        vault_path: Path | None,
    ) -> dict[str, Any]:
        """Build the JSON context dict passed to hook scripts via stdin."""
        return {
            "file_path": str(file_path),
            "classification": (classification.model_dump(mode="json") if classification else None),
            "vault_path": str(vault_path) if vault_path else None,
        }

    @staticmethod
    def _execute_hook(
        hook_path: str,
        context: dict[str, Any],
    ) -> str | None:
        """Execute a single hook script.

        The script receives the context as JSON on stdin. If the script
        writes to stdout, the raw stdout string is returned. Returns
        ``None`` when the script produces no output.

        Raises ``FileNotFoundError`` when the script does not exist and
        ``subprocess.CalledProcessError`` when it exits non-zero.
        """
        path = Path(hook_path)
        if not path.is_file():
            raise FileNotFoundError(f"Hook script not found: {hook_path}")

        input_json = json.dumps(context, ensure_ascii=False, default=str)
        result = subprocess.run(  # noqa: S603 — trusted local config path
            [str(path)],
            input=input_json,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "Hook %s exited with code %d: %s",
                hook_path,
                result.returncode,
                result.stderr.strip()[:500],
            )
        return result.stdout.strip() if result.stdout.strip() else None

    @staticmethod
    def _try_parse_classification(output: str) -> ClassificationResult | None:
        """Try to parse hook stdout as a ClassificationResult.

        Returns ``None`` when the output is not valid JSON or lacks the
        required fields.
        """
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        required = {"sensitivity", "category", "disguise_name", "disguise_extension"}
        if not required.issubset(data.keys()):
            return None
        try:
            return ClassificationResult(**data)
        except Exception:  # noqa: BLE001
            return None


class ProcessingPipeline:
    """Orchestrate the full lifecycle of an Inbox file."""

    def __init__(
        self,
        config: AegisConfig,
        classifier: Classifier,
        task_store: TaskStore,
        vault_key: bytes,
        vault_manager: VaultManager | None = None,
        audit_logger: AuditLogger | None = None,
        embedding_provider: LocalEmbeddingProvider | None = None,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.task_store = task_store
        self.audit_logger = audit_logger
        self.embedding_provider = embedding_provider
        self.vault_manager = vault_manager or VaultManager(
            config.paths.vault,
            vault_key,
            audit_logger=audit_logger,
        )
        # Track source paths currently being processed to deduplicate
        # concurrent events for the same file.
        self._in_progress: set[str] = set()
        self._in_progress_lock = threading.Lock()
        # Cap concurrent LLM classification calls (Phase 6.6).
        self._classify_sem = asyncio.Semaphore(config.resources.classify_concurrency)
        # Alert when the Vault device approaches capacity (Phase 6.6).
        self._disk_checker = DiskWatermarkChecker(
            config.paths.vault,
            config.resources.disk_watermark_percent,
            audit_logger=audit_logger,
        )
        # Pre/Post-Consume hook executor with error isolation.
        self._hook_executor = HookExecutor(
            pre_hooks=config.hooks.pre_consume_hooks,
            post_hooks=config.hooks.post_consume_hooks,
        )

    async def process(self, event: FileEvent) -> TaskStatus:
        """Process a file event from creation to Vault storage."""
        task_id = event.event_id
        source_key = str(event.source_path)

        # Deduplicate based on the source file path so that multiple events
        # for the same file (e.g. created + moved) are not processed twice.
        with self._in_progress_lock:
            if source_key in self._in_progress:
                logger.info("Skipping duplicate event for %s (task %s)", source_key, task_id)
                return TaskStatus(
                    task_id=task_id,
                    state=TaskState.IDLE.name,
                    message="duplicate event skipped",
                )
            self._in_progress.add(source_key)
        try:
            return await self._process_inner(event, task_id)
        finally:
            with self._in_progress_lock:
                self._in_progress.discard(source_key)

    async def _process_inner(self, event: FileEvent, task_id: UUID) -> TaskStatus:
        """Inner processing logic that runs after dedup admission."""
        sm = StateMachine(task_id, TaskState.IDLE)

        self.task_store.create(task_id, event.source_path)
        sm.transition(TaskState.CLASSIFYING)
        self.task_store.update_state(task_id, TaskState.CLASSIFYING)

        if self.audit_logger is not None:
            self.audit_logger.log(
                "file_ingested",
                {
                    "task_id": str(task_id),
                    "source_path": str(event.source_path),
                },
            )

        encrypt_result: EncryptResult | None = None
        try:
            async with self._classify_sem:
                classification = await self._classify(event.source_path)

            # ── Pre-consume hooks ───────────────────────────────────────
            # Run after classification (so hooks can inspect/modify the
            # result) but before encryption (so modifications take effect).
            if self._hook_executor.has_pre_hooks:
                modified = self._hook_executor.run_pre_consume(
                    event.source_path, classification, vault_path=None
                )
                if modified is not None:
                    classification = modified

            self.task_store.update_classification(task_id, classification)
            if self.audit_logger is not None:
                self.audit_logger.log(
                    "classified",
                    {
                        "task_id": str(task_id),
                        "sensitivity": classification.sensitivity.value,
                        "category": classification.category,
                    },
                )

            sm.transition(TaskState.ENCRYPTING)
            self.task_store.update_state(task_id, TaskState.ENCRYPTING)

            # Warn (non-blocking) when the Vault device is near capacity.
            self._disk_checker.check()

            encrypt_result = self._encrypt(event.source_path, classification, task_id)
            self.task_store.update_vault_result(
                task_id, encrypt_result.vault_path, encrypt_result.salt, encrypt_result.nonce
            )

            sm.transition(TaskState.INDEXING)
            self.task_store.update_state(task_id, TaskState.INDEXING)
            self._index(classification, encrypt_result, event.source_path)

            # Confirm COMPLETED before secure-deleting the original so that
            # a crash between delete and state-persist doesn't lose the file.
            sm.transition(TaskState.COMPLETED)
            status = self.task_store.update_state(task_id, TaskState.COMPLETED)

            # ── Post-consume hooks ──────────────────────────────────────
            # Run after the file is fully processed (classified, encrypted,
            # indexed) but before the source is secure-deleted so hooks can
            # still access the original file if needed.
            if self._hook_executor.has_post_hooks:
                self._hook_executor.run_post_consume(
                    event.source_path, classification, encrypt_result.vault_path
                )

            # Only secure-delete the source after the task is durably COMPLETED.
            try:
                self._secure_delete(event.source_path)
            except OSError as exc:
                logger.warning("Secure delete failed for %s: %s", event.source_path, exc)

            return status

        except SecurityPolicyError as exc:
            if self.audit_logger is not None:
                self.audit_logger.log(
                    "policy_violation",
                    {
                        "task_id": str(task_id),
                        "error": str(exc),
                    },
                )
            sm.transition(TaskState.QUARANTINED)
            return self.task_store.update_state(task_id, TaskState.QUARANTINED, str(exc))
        except Exception as exc:  # noqa: BLE001
            # Clean up any vault file that was created before the failure so
            # we don't leave orphaned encrypted artefacts in the Vault.
            if encrypt_result is not None:
                try:
                    if encrypt_result.vault_path.exists():
                        encrypt_result.vault_path.unlink()
                except OSError:
                    logger.warning(
                        "Failed to clean up vault file %s after pipeline failure",
                        encrypt_result.vault_path,
                    )
            if sm.can_transition_to(TaskState.FAILED):
                sm.transition(TaskState.FAILED)
            logger.exception("Pipeline failed for task %s", task_id)
            return self.task_store.update_state(task_id, TaskState.FAILED, str(exc))

    async def _classify(self, source_path: Path) -> ClassificationResult:
        """Classify the file using the configured model connection."""
        return await self.classifier.classify(source_path)

    def _encrypt(
        self,
        source_path: Path,
        classification: ClassificationResult,
        task_id: UUID,
    ) -> EncryptResult:
        """Encrypt file into Vault after validating the connection is trusted local."""
        require_trusted_local_connection(
            self.classifier.connection,
            audit_logger=self.audit_logger,
            operation="encrypt",
        )
        encrypt_result = self.vault_manager.encrypt(
            source_path,
            classification,
            task_id,
        )
        if self.audit_logger is not None:
            self.audit_logger.log(
                "encrypted",
                {
                    "task_id": str(task_id),
                    "vault_path": str(encrypt_result.vault_path),
                },
            )
        return EncryptResult(
            task_id=task_id,
            vault_path=encrypt_result.vault_path,
            salt=encrypt_result.salt,
            nonce=encrypt_result.nonce,
        )

    def _index(
        self,
        classification: ClassificationResult,
        result: EncryptResult,
        source_path: Path | None = None,
    ) -> None:
        """Index classification metadata for full-text and semantic search.

        Also extracts and indexes content chunks for RAG retrieval when
        a source path is provided.
        """
        self.task_store.index_classification(
            task_id=result.task_id,
            classification=classification,
            vault_path=result.vault_path,
        )
        if self.embedding_provider is not None:
            self.task_store.index_embedding(
                task_id=result.task_id,
                vault_path=result.vault_path,
                classification=classification,
                provider=self.embedding_provider,
            )

        # Extract and index content chunks for RAG
        if source_path is not None:
            try:
                self._index_content_chunks(
                    result.task_id, result.vault_path, classification, source_path
                )
            except Exception as e:
                logger.warning("Failed to index content chunks for %s: %s", result.task_id, e)

    def _index_content_chunks(
        self,
        task_id: UUID,
        vault_path: Path,
        classification: ClassificationResult,
        source_path: Path,
    ) -> None:
        """Extract text content and index chunks for RAG retrieval."""
        try:
            # Extract text from source file
            extractor = ExtractionManager()
            extraction = extractor.extract(source_path)

            if not extraction.text or extraction.method == "none":
                logger.debug("No text content extracted from %s", source_path)
                return

            # Split into semantic chunks
            chunker = SemanticChunker()
            raw_chunks = chunker.chunk_text(extraction.text)

            if not raw_chunks:
                logger.debug("No chunks generated from %s", source_path)
                return

            # Index chunks with embeddings
            self.task_store.index_content_chunks(
                task_id=task_id,
                vault_path=vault_path,
                classification=classification,
                chunks=raw_chunks,
                provider=self.embedding_provider,
            )

            logger.info(
                "Indexed %d content chunks from %s for task %s",
                len(raw_chunks),
                source_path.name,
                task_id,
            )
        except Exception as e:
            logger.warning("Content chunk indexing failed for %s: %s", task_id, e)

    def _secure_delete(self, source_path: Path) -> None:
        """Overwrite and delete original Inbox file.

        On systems with full-disk encryption this is a best-effort wipe;
        rely on FDE for the underlying security boundary.

        The file is overwritten in 1 MB chunks to avoid loading large files
        entirely into memory, fsync'd to flush the writes to disk, and then
        unlinked.  ``O_NOFOLLOW`` is used to prevent symlink attacks.
        """
        if not source_path.exists():
            return
        # Reject symlinks outright so we never overwrite a different file.
        if source_path.is_symlink():
            source_path.unlink()
            return

        size = source_path.stat().st_size
        # Open with O_NOFOLLOW as defence-in-depth against symlink races.
        _nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(source_path), os.O_RDWR | _nofollow_flag)
        except OSError:
            fd = os.open(str(source_path), os.O_RDWR)

        try:
            f = os.fdopen(fd, "r+b", closefd=True)
        except OSError:
            os.close(fd)
            raise

        with f:
            remaining = size
            while remaining > 0:
                chunk = os.urandom(min(_SECURE_DELETE_CHUNK, remaining))
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        source_path.unlink()

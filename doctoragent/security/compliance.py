"""GDPR / CCPA data-subject compliance.

Implements the four core data-subject rights plus retention policy and consent
management.  All data access goes through :class:`TaskStore` (for the document
index, vault file metadata and chunk memory) and :class:`AuditLogger` (for the
append-only audit trail) — the compliance layer never opens the SQLite
database or writes audit records directly.

* **Right of access (DSAR)** — :meth:`ComplianceManager.export_subject_data`
  collects every document and metadata record that mentions the subject and
  returns it as a structured JSON export.
* **Right to erasure** — :meth:`ComplianceManager.erase_subject_data` removes
  matching task records, their full-text index rows, vector/chunk memory and
  the on-disk encrypted vault files, then records the erasure to the audit log.
* **Right to portability** — :meth:`ComplianceManager.export_portable` produces
  a standard JSON or CSV export of a subject's data.
* **Retention policy** — :class:`RetentionPolicy` maps document categories to a
  retention window; :meth:`ComplianceManager.apply_retention_policies` expires
  (marks or deletes) documents past their window.
* **Consent management** — :class:`ConsentRecord` tracks the subject's
  agreement to each data-processing operation.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from doctoragent.compat import UTC

if TYPE_CHECKING:
    from doctoragent.orchestration.task_store import TaskStore
    from doctoragent.security.audit_log import AuditLogger

logger = logging.getLogger(__name__)

# Default retention windows by category (days).  These mirror common
# regulatory anchors: financial/tax records 7 years, contracts 7 years,
# invoices 7 years, temporary/transient files 30 days.
_DEFAULT_RETENTION_POLICIES: dict[str, int] = {
    "invoice": 7 * 365,
    "financial": 7 * 365,
    "contract": 7 * 365,
    "receipt": 7 * 365,
    "temporary": 30,
    "temp": 30,
    "cache": 7,
}


@dataclass
class RetentionPolicy:
    """Per-category document retention configuration."""

    # Map of category -> retention days.  Documents whose category is absent
    # are governed by ``default_days``.
    category_days: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_RETENTION_POLICIES))
    # Applied when no category-specific rule matches.
    default_days: int = 365 * 3
    # When True expired documents are deleted; when False they are only marked
    # (state set to ``QUARANTINED``) so a human can review before deletion.
    auto_delete: bool = False


@dataclass
class ConsentRecord:
    """A single consent grant or withdrawal for a data-processing operation."""

    subject_id: str
    operation: str
    granted: bool
    timestamp: str = ""
    purpose: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()


class ComplianceManager:
    """Coordinate data-subject rights, retention and consent.

    Parameters
    ----------
    task_store:
        The :class:`TaskStore` used for all document/index/chunk queries and
        deletions.
    audit_logger:
        Optional :class:`AuditLogger` for recording compliance operations.
    vault_dir:
        Root directory holding the encrypted vault files.  Needed so erasure
        can unlink the on-disk encrypted file once its DB record is gone.
    retention:
        :class:`RetentionPolicy` used by :meth:`apply_retention_policies`.
    consent_store_path:
        Optional JSON file path for persisting :class:`ConsentRecord` entries.
    """

    def __init__(
        self,
        task_store: TaskStore,
        audit_logger: AuditLogger | None = None,
        vault_dir: Path | None = None,
        retention: RetentionPolicy | None = None,
        consent_store_path: Path | None = None,
    ) -> None:
        self._store = task_store
        self._audit = audit_logger
        self._vault_dir = vault_dir
        self._retention = retention or RetentionPolicy()
        self._consent_path = consent_store_path
        self._consents: list[ConsentRecord] = []
        if self._consent_path is not None and self._consent_path.exists():
            self._load_consents()

    # ── Right of access (DSAR) ───────────────────────────────────────────

    def export_subject_data(self, subject_id: str) -> dict[str, Any]:
        """Collect all data associated with *subject_id*.

        A document is considered to "belong" to a subject when the subject
        identifier appears in the source path, the classification summary,
        the tags, or any indexed chunk content.  The returned dict is a
        structured JSON-ready export.
        """
        records = self._find_subject_records(subject_id)
        export: dict[str, Any] = {
            "subject_id": subject_id,
            "exported_at": datetime.now(UTC).isoformat(),
            "document_count": len(records),
            "documents": records,
        }
        if self._audit is not None:
            self._audit.log(
                "storage_backend_operation",
                {
                    "operation": "dsar_access",
                    "subject_id": subject_id,
                    "document_count": len(records),
                    "severity": "MEDIUM",
                },
            )
        return export

    def _find_subject_records(self, subject_id: str) -> list[dict[str, Any]]:
        """Return all task/vault records that mention *subject_id*."""
        needle = subject_id.lower()
        vault_files = self._store.list_vault_files()
        matched: list[dict[str, Any]] = []
        # Track by vault_path (the common key across list_vault_files and
        # SearchResult) to avoid duplicates between the two matching passes.
        seen: set[str] = set()
        for entry in vault_files:
            haystack_parts = [
                str(entry.get("vault_path", "")),
                str(entry.get("summary", "")),
                str(entry.get("category", "")),
                " ".join(entry.get("tags", []) or []),
                str(entry.get("task_id", "")),
            ]
            haystack = " ".join(haystack_parts).lower()
            if needle in haystack:
                detail = self._enrich_record(entry)
                matched.append(detail)
                seen.add(str(entry.get("vault_path", "")))
        # Also search the full-text index for any chunk content mentioning the
        # subject that did not surface via list_vault_files (e.g. mid-chunk).
        # ``SearchResult`` carries ``vault_path`` but not ``task_id``, so we
        # match by vault_path and then resolve the owning task record.
        try:
            search_results = self._store.search(subject_id, top_k=200)
        except Exception:  # noqa: BLE001 — search must not abort a DSAR
            search_results = []
        for hit in search_results:
            vault_path = str(getattr(hit, "vault_path", ""))
            if not vault_path or vault_path in seen:
                continue
            record = self._lookup_task_by_vault_path(vault_path)
            if record is None:
                continue
            detail = self._record_to_export(record)
            matched.append(detail)
            seen.add(vault_path)
        return matched

    def _lookup_task_by_vault_path(self, vault_path: str) -> dict[str, Any] | None:
        """Find the task record that owns *vault_path* via list_vault_files."""
        for entry in self._store.list_vault_files():
            if str(entry.get("vault_path", "")) == vault_path:
                from uuid import UUID

                try:
                    return self._store.get(UUID(str(entry["task_id"])))
                except (ValueError, KeyError, TypeError):
                    return None
        return None

    def _enrich_record(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Augment a list_vault_files entry with task-level metadata."""
        from uuid import UUID

        enriched = {
            "task_id": entry.get("task_id"),
            "vault_path": entry.get("vault_path"),
            "category": entry.get("category", "unknown"),
            "summary": entry.get("summary", ""),
            "tags": entry.get("tags", []),
        }
        try:
            chunks = self._store.get_content_chunks(UUID(str(entry["task_id"])))
            enriched["chunk_count"] = len(chunks)
        except Exception:  # noqa: BLE001 — chunks are best-effort
            enriched["chunk_count"] = 0
        return enriched

    def _record_to_export(self, record: dict[str, Any]) -> dict[str, Any]:
        classification_raw = record.get("classification") or "{}"
        try:
            cls = json.loads(classification_raw)
        except (json.JSONDecodeError, TypeError):
            cls = {}
        return {
            "task_id": record.get("task_id"),
            "vault_path": record.get("vault_path"),
            "category": cls.get("category", "unknown"),
            "summary": cls.get("summary", ""),
            "tags": cls.get("tags", []),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
        }

    # ── Right to erasure ──────────────────────────────────────────────────

    def erase_subject_data(self, subject_id: str, delete_files: bool = True) -> int:
        """Erase every record and file associated with *subject_id*.

        Returns the number of documents erased.  Each erased document has its
        task record, full-text/vector/chunk index rows and (optionally) the
        on-disk encrypted vault file removed.
        """
        records = self._find_subject_records(subject_id)
        erased = 0
        from uuid import UUID

        for entry in records:
            task_id_str = str(entry["task_id"])
            try:
                task_uuid = UUID(task_id_str)
            except (ValueError, AttributeError):
                continue
            # Remove chunk memory first so the index is consistent.
            try:
                self._store.delete_content_chunks(task_uuid)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.debug("Failed to delete chunks for %s", task_id_str, exc_info=True)
            # Remove the DB task + FTS/vector rows via the TaskStore API.
            try:
                self._store.delete(task_uuid)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to delete task %s during erasure", task_id_str)
            # Remove the on-disk encrypted vault file.
            if delete_files:
                vault_path_raw = entry.get("vault_path")
                if vault_path_raw:
                    self._unlink_vault_file(Path(str(vault_path_raw)))
            erased += 1

        if self._audit is not None:
            self._audit.log(
                "storage_backend_operation",
                {
                    "operation": "dsar_erasure",
                    "subject_id": subject_id,
                    "documents_erased": erased,
                    "files_deleted": erased if delete_files else 0,
                    "severity": "HIGH",
                },
            )
        return erased

    def _unlink_vault_file(self, vault_path: Path) -> None:
        """Remove the encrypted vault file from disk (best-effort)."""
        try:
            vault_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove vault file %s during erasure", vault_path)

    # ── Right to portability ───────────────────────────────────────────────

    def export_portable(
        self,
        subject_id: str,
        dest_path: Path,
        fmt: str = "json",
    ) -> Path:
        """Export a subject's data in a portable format (JSON or CSV)."""
        data = self.export_subject_data(subject_id)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "csv":
            self._write_csv(data, dest_path)
        elif fmt == "json":
            dest_path.write_text(
                json.dumps(data, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            raise ValueError(f"Unsupported portable format: {fmt!r}")
        return dest_path

    @staticmethod
    def _write_csv(data: dict[str, Any], dest_path: Path) -> None:
        with dest_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["task_id", "vault_path", "category", "summary", "tags", "created_at"])
            for doc in data.get("documents", []):
                tags = doc.get("tags", [])
                writer.writerow(
                    [
                        doc.get("task_id", ""),
                        doc.get("vault_path", ""),
                        doc.get("category", ""),
                        doc.get("summary", ""),
                        ";".join(tags) if isinstance(tags, list) else str(tags),
                        doc.get("created_at", ""),
                    ]
                )

    # ── Retention policy ──────────────────────────────────────────────────

    def apply_retention_policies(self, now: datetime | None = None) -> dict[str, int]:
        """Apply retention policies; mark or delete expired documents.

        Returns a summary ``{"marked": n, "deleted": n, "errors": n}``.
        """
        now = now or datetime.now(UTC)
        vault_files = self._store.list_vault_files()
        marked = deleted = errors = 0
        from uuid import UUID

        from doctoragent.orchestration.state_machine import TaskState

        for entry in vault_files:
            category = str(entry.get("category", "unknown")).lower()
            retention_days = self._retention.category_days.get(
                category, self._retention.default_days
            )
            created_at = self._lookup_created_at(entry)
            if created_at is None:
                continue
            expiry = created_at + timedelta(days=retention_days)
            if now < expiry:
                continue
            task_id_str = str(entry.get("task_id"))
            try:
                task_uuid = UUID(task_id_str)
            except (ValueError, AttributeError):
                errors += 1
                continue
            if self._retention.auto_delete:
                try:
                    self._store.delete_content_chunks(task_uuid)
                    self._store.delete(task_uuid)
                    vault_path_raw = entry.get("vault_path")
                    if vault_path_raw:
                        self._unlink_vault_file(Path(str(vault_path_raw)))
                    deleted += 1
                except Exception:  # noqa: BLE001
                    errors += 1
            else:
                try:
                    self._store.update_state(task_uuid, TaskState.QUARANTINED, "retention_expired")
                    marked += 1
                except Exception:  # noqa: BLE001
                    errors += 1

        if self._audit is not None and (marked or deleted):
            self._audit.log(
                "storage_backend_operation",
                {
                    "operation": "retention_expiry",
                    "marked": marked,
                    "deleted": deleted,
                    "errors": errors,
                    "severity": "MEDIUM",
                },
            )
        return {"marked": marked, "deleted": deleted, "errors": errors}

    def _lookup_created_at(self, entry: dict[str, Any]) -> datetime | None:
        """Best-effort recovery of a document's creation timestamp."""
        # list_vault_files does not include timestamps, so look up the task.
        from uuid import UUID

        try:
            task_uuid = UUID(str(entry.get("task_id")))
        except (ValueError, AttributeError, TypeError):
            return None
        record = self._store.get(task_uuid)
        if not record:
            return None
        raw = record.get("created_at") or record.get("updated_at")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts

    # ── Consent management ─────────────────────────────────────────────────

    def record_consent(
        self,
        subject_id: str,
        operation: str,
        granted: bool,
        purpose: str = "",
    ) -> ConsentRecord:
        """Record a consent grant or withdrawal for *operation*."""
        record = ConsentRecord(
            subject_id=subject_id,
            operation=operation,
            granted=granted,
            purpose=purpose,
        )
        self._consents.append(record)
        self._save_consents()
        if self._audit is not None:
            self._audit.log(
                "storage_backend_operation",
                {
                    "operation": "consent_recorded",
                    "subject_id": subject_id,
                    "data_operation": operation,
                    "granted": granted,
                    "severity": "MEDIUM",
                },
            )
        return record

    def has_consent(self, subject_id: str, operation: str) -> bool:
        """Return True iff the most recent consent for the operation is a grant."""
        latest: ConsentRecord | None = None
        for record in self._consents:
            if record.subject_id == subject_id and record.operation == operation:
                if latest is None or record.timestamp >= latest.timestamp:
                    latest = record
        return latest.granted if latest is not None else False

    def list_consents(self, subject_id: str | None = None) -> list[ConsentRecord]:
        """Return consent records, optionally filtered by *subject_id*."""
        if subject_id is None:
            return list(self._consents)
        return [r for r in self._consents if r.subject_id == subject_id]

    def _save_consents(self) -> None:
        if self._consent_path is None:
            return
        self._consent_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "subject_id": r.subject_id,
                "operation": r.operation,
                "granted": r.granted,
                "timestamp": r.timestamp,
                "purpose": r.purpose,
                "notes": r.notes,
            }
            for r in self._consents
        ]
        self._consent_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _load_consents(self) -> None:
        assert self._consent_path is not None
        try:
            payload = json.loads(self._consent_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in payload:
            self._consents.append(
                ConsentRecord(
                    subject_id=item.get("subject_id", ""),
                    operation=item.get("operation", ""),
                    granted=bool(item.get("granted", False)),
                    timestamp=item.get("timestamp", ""),
                    purpose=item.get("purpose", ""),
                    notes=item.get("notes", ""),
                )
            )


__all__ = [
    "ComplianceManager",
    "ConsentRecord",
    "RetentionPolicy",
]

"""Append-only audit logger with per-entry HMAC integrity checks.

The log is written as newline-delimited JSON (NDJSON) to
``<logs>/audit.log.ndjson``.  Each record contains an HMAC-SHA256 over the
canonical JSON of the record (excluding the ``hmac`` field itself) so that
tampering with the log file can be detected offline.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from doctoragent.compat import UTC
from doctoragent.config import AegisConfig

AlertCallback = Callable[[str, dict[str, Any]], None]

# Severity hierarchy used by the alert pipeline.  ``AlertManager`` maps the
# legacy AuditLogger severities onto these levels.
ALERT_LEVELS: dict[str, str] = {
    "CRITICAL": "CRITICAL",
    "HIGH": "WARNING",
    "MEDIUM": "WARNING",
    "INFO": "INFO",
}


@dataclass
class AlertRecord:
    """A single alert raised by the alert pipeline."""

    event_type: str
    severity: str
    details: dict[str, Any]
    timestamp: str = ""
    status: str = "notified"  # notified | acknowledged | resolved
    count: int = 1
    notified: bool = False
    alert_id: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()
        if not self.alert_id:
            digest = hashlib.sha256(f"{self.event_type}:{self.timestamp}".encode()).hexdigest()
            self.alert_id = digest[:16]


ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "file_ingested",
        "classified",
        "encrypted",
        "decrypted",
        "connection_tested",
        "policy_violation",
        "offline_policy_violation",
        "cloud_fallback_used",
        "login_attempt",
        "agent_initialized",
        "master_key_changed",
        "sandbox_escape_attempt",
        "audit_write_failed",
        "password_store_operation",
        "sandbox_run_failed",
        "resource_backpressure",
        "disk_watermark_exceeded",
        "plugin_loaded",
        "plugin_load_failed",
        "webhook_dispatched",
        "webhook_failed",
        "storage_backend_operation",
        # ── Clinical events (FDA SaMD / 21 CFR Part 11 / HIPAA audit) ──
        # A blocking rule finding (critical/contraindicated) was produced by
        # the deterministic safety layer.
        "clinical_safety_alert",
        # An LLM-output guardrail fired (block or flag) on a specialist's
        # answer or the synthesised combined output.
        "clinical_guardrail_action",
        # A clinical workflow run completed (always recorded so the full
        # decision chain is reconstructable from the audit trail).
        "clinical_decision",
        # A FHIR resource was read from an external EHR — PHI access must be
        # auditable per HIPAA §164.312(b).
        "fhir_resource_access",
        # A FHIR resource write was attempted (create/update). Captured even
        # when the write is gated behind human review.
        "fhir_write_attempted",
        # ── EHR integration events ──────────────────────────────────────────
        # A CDS Hooks 2.0 service was invoked by an EHR (HL7 cds-hooks spec).
        # Captured so the EHR-side decision trail (which hook, which patient,
        # which service id) is reconstructable for FDA SaMD / 21 CFR Part 11.
        "cds_hooks_invocation",
    }
)

_MAX_LOG_SIZE = 100 * 1024 * 1024  # 100 MB
_MAX_LOG_ROTATIONS = 5  # Keep at most 5 rotated log files (audit.log.1..5.ndjson)

# 启用路径脱敏时，这些字段的值只保留 basename，避免泄露用户名/目录结构。
_PATH_FIELDS: frozenset[str] = frozenset({"vault_path", "destination"})

# Canonical per-event-type severity for the audit fan-out (realtime
# WebSocket / SSE broadcaster + the /audit/logs severity filter). This is the
# single source of truth — server.py imports it instead of re-declaring a
# parallel map. Mirrors the alert rules in ``_check_alert_rules``.
AUDIT_EVENT_SEVERITY: dict[str, str] = {
    "decrypted": "CRITICAL",
    "master_key_changed": "CRITICAL",
    "sandbox_escape_attempt": "CRITICAL",
    "cloud_fallback_used": "HIGH",
    "policy_violation": "HIGH",
    "offline_policy_violation": "HIGH",
    "sandbox_run_failed": "MEDIUM",
    "password_store_operation": "MEDIUM",
    "connection_tested": "MEDIUM",
    "resource_backpressure": "HIGH",
    "disk_watermark_exceeded": "HIGH",
    "plugin_loaded": "MEDIUM",
    "plugin_load_failed": "HIGH",
    "webhook_dispatched": "MEDIUM",
    "webhook_failed": "MEDIUM",
    "storage_backend_operation": "MEDIUM",
    # Clinical events — blocking safety findings and guardrail blocks are
    # CRITICAL (potential patient harm); a guardrail flag and the routine
    # decision record are INFO/HIGH so the audit trail stays complete.
    "clinical_safety_alert": "CRITICAL",
    "clinical_guardrail_action": "HIGH",
    "clinical_decision": "INFO",
    "fhir_resource_access": "HIGH",
    "fhir_write_attempted": "CRITICAL",
}

# Detail-field allowlist forwarded to the realtime broadcaster. Only
# non-PHI scalars (counts / booleans / short enums) are surfaced — never
# patient_id, free-text queries, clinical findings or warning lists, which
# would leak PHI to every authenticated WS/SSE subscriber.
_BROADCAST_SAFE_FIELDS: frozenset[str] = frozenset(
    {
        "severity",
        "action",
        "success",
        "is_local",
        "requires_human_review",
        "blocking_finding",
        "finding_count",
        "citation_count",
        "count",
    }
)

# Rate-limit window: the same event type is only fanned out to desktop +
# webhook channels once within this window (seconds).
_ALERT_RATE_LIMIT_SECONDS = 5 * 60
# Maximum number of alert records kept in the in-memory history ring.
_ALERT_HISTORY_LIMIT = 500


class AlertManager:
    """Real alert delivery with rate limiting, aggregation and history.

    The :class:`AuditLogger` builds alert *records*; this manager turns them
    into actual side effects:

    * **Desktop notification** via
      :func:`doctoragent.connections.notifications.DesktopNotifier.notify_security_alert`.
    * **Webhook notification** — an HMAC-signed JSON ``POST`` to
      *webhook_url* when configured.
    * **Rate limiting** — the same ``event_type`` is only notified once per
      :data:`_ALERT_RATE_LIMIT_SECONDS` window, with subsequent occurrences
      aggregated into a ``count`` on the original alert.
    * **Severity routing** — ``CRITICAL`` alerts are delivered immediately;
      ``WARNING`` alerts are delivered but batched via the rate limiter;
      ``INFO`` alerts are only recorded, never notified.
    * **History** — every alert is appended to an in-memory ring with a
      processing status (``notified`` / ``acknowledged`` / ``resolved``).
    """

    def __init__(
        self,
        notifier: Any | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        rate_limit_seconds: float = _ALERT_RATE_LIMIT_SECONDS,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._notifier = notifier
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._rate_limit = rate_limit_seconds
        self._audit = audit_logger
        self._lock = threading.Lock()
        self._history: list[AlertRecord] = []
        # event_type -> (last_notified_monotonic, aggregated_count)
        self._rate_state: dict[str, tuple[float, int]] = {}

    # ── public API ────────────────────────────────────────────────────────

    def handle_alert(self, severity: str, event_type: str, details: dict[str, Any]) -> AlertRecord:
        """Process one alert; deliver per severity & rate-limit rules."""
        level = ALERT_LEVELS.get(str(severity).upper(), "INFO")
        record = AlertRecord(
            event_type=event_type,
            severity=level,
            details=dict(details),
        )
        should_notify = level in ("CRITICAL", "WARNING")
        with self._lock:
            now_mono = time.monotonic()
            last_notified, agg_count = self._rate_state.get(event_type, (0.0, 0))
            within_window = (now_mono - last_notified) < self._rate_limit
            if within_window:
                # Aggregate: bump the count on the existing record instead of
                # re-notifying.  Avoids alert storms from a flapping rule.
                agg_count += 1
                self._rate_state[event_type] = (last_notified, agg_count)
                record.count = agg_count
                record.notified = False
                record.status = "aggregated"
            else:
                self._rate_state[event_type] = (now_mono, 1)
                record.notified = should_notify
                record.status = "notified" if should_notify else "logged"
            self._append_history(record)
        if record.notified:
            self._deliver(record)
        return record

    def pending_count(self) -> int:
        """Return the number of alerts that are notified but not yet resolved."""
        with self._lock:
            return sum(
                1
                for r in self._history
                if r.status in ("notified", "aggregated", "acknowledged")
                and r.severity in ("CRITICAL", "WARNING")
            )

    def acknowledge(self, alert_id: str) -> bool:
        """Mark an alert acknowledged.  Returns True if found."""
        with self._lock:
            for r in self._history:
                if r.alert_id == alert_id:
                    r.status = "acknowledged"
                    return True
        return False

    def resolve(self, alert_id: str) -> bool:
        """Mark an alert resolved.  Returns True if found."""
        with self._lock:
            for r in self._history:
                if r.alert_id == alert_id:
                    r.status = "resolved"
                    return True
        return False

    def history(self, limit: int = 100) -> list[AlertRecord]:
        """Return the most recent alert records (newest first)."""
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    # ── delivery ──────────────────────────────────────────────────────────

    def _deliver(self, record: AlertRecord) -> None:
        """Fan the alert out to desktop + webhook channels."""
        # Desktop notification.
        if self._notifier is not None:
            handler = getattr(self._notifier, "notify_security_alert", None)
            if handler is not None:
                try:
                    handler(
                        record.event_type,
                        self._format_desktop_message(record),
                    )
                except Exception:  # noqa: BLE001 — never let a notifier break alerts
                    logging.exception("Desktop alert notification failed")
        # Webhook notification.
        if self._webhook_url:
            self._post_webhook(record)

    def _format_desktop_message(self, record: AlertRecord) -> str:
        details = record.details
        # Prefer a short human-readable summary when available.
        summary = details.get("reason") or details.get("operation") or record.event_type
        suffix = f" (x{record.count})" if record.count > 1 else ""
        return f"[{record.severity}] {summary}{suffix}"

    def _post_webhook(self, record: AlertRecord) -> None:
        """Send an HMAC-signed JSON POST to the configured webhook URL.

        Delivery is best-effort and runs synchronously; a failure is logged but
        never raised into the caller so a flaky webhook cannot stall the audit
        pipeline.  Uses ``httpx`` (a hard project dependency) with a short
        timeout to avoid blocking.
        """
        try:
            import httpx
        except ImportError:  # pragma: no cover — httpx is a hard dep
            logging.warning("httpx unavailable; skipping webhook alert delivery")
            return
        payload = {
            "event_type": record.event_type,
            "severity": record.severity,
            "details": record.details,
            "count": record.count,
            "timestamp": record.timestamp,
            "alert_id": record.alert_id,
        }
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._webhook_secret:
            sig = hmac.new(
                self._webhook_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-DoctorAgent-Signature"] = f"sha256={sig}"
        try:
            with httpx.Client(timeout=5.0, follow_redirects=False) as client:
                resp = client.post(self._webhook_url, content=body, headers=headers)
            if resp.status_code >= 400 and self._audit is not None:
                self._audit.log(
                    "webhook_failed",
                    {
                        "url": self._webhook_url,
                        "status_code": resp.status_code,
                        "event_type": record.event_type,
                    },
                )
        except Exception:  # noqa: BLE001 — webhook must never break alerts
            logging.exception("Webhook alert delivery failed")
            if self._audit is not None:
                self._audit.log(
                    "webhook_failed",
                    {"url": self._webhook_url, "error": "delivery_exception"},
                )

    # ── internals ─────────────────────────────────────────────────────────

    def _append_history(self, record: AlertRecord) -> None:
        self._history.append(record)
        if len(self._history) > _ALERT_HISTORY_LIMIT:
            # Drop the oldest entries beyond the ring limit.
            del self._history[: len(self._history) - _ALERT_HISTORY_LIMIT]


class AuditLogger:
    """Append-only NDJSON audit logger with HMAC integrity checks."""

    def __init__(
        self,
        config: AegisConfig,
        hmac_key: bytes | None = None,
        redact_paths: bool = False,
        alert_manager: AlertManager | None = None,
    ) -> None:
        self.log_path = config.paths.logs / "audit.log.ndjson"
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._hmac_key = hmac_key if hmac_key is not None else self._load_or_create_key()
        self._alert_callbacks: list[AlertCallback] = []
        self._decrypt_failures: dict[str, int] = {}
        self._first_cloud_connection = True
        # 路径脱敏开关：显式参数优先；未指定时回退读取 config.security.audit_redact_paths
        # （config.py 可能由其他代理新增该字段；不存在时默认 False 保持向后兼容）。
        security_cfg = getattr(config, "security", None)
        self._redact_paths = bool(redact_paths) or bool(
            getattr(security_cfg, "audit_redact_paths", False)
        )
        # Optional AlertManager for real desktop/webhook delivery.  When set,
        # every fired alert is also routed through it so the alert pipeline
        # gains rate limiting, aggregation and history on top of the legacy
        # callback fan-out.
        self._alert_manager = alert_manager
        # Optional realtime broadcaster (WebSocket / SSE fan-out). When set
        # (the API server injects its ``app.state.broadcaster``), every
        # successfully appended audit record is also published as a
        # PHI-sanitised envelope so authenticated WS/SSE subscribers receive
        # real-time signal. Only non-PHI scalar fields (severity + a small
        # allowlist of counts/booleans) are forwarded — never patient_id,
        # free-text queries or clinical findings.
        self._event_broadcaster: Any = None

    def _key_path(self) -> Path:
        return self.log_path.parent / ".audit.key"

    def _load_or_create_key(self) -> bytes:
        key_path = self._key_path()
        try:
            return key_path.read_bytes()
        except FileNotFoundError:
            pass
        key = os.urandom(32)
        try:
            fd = os.open(
                str(key_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            # Another process raced ahead and created the key.
            return key_path.read_bytes()
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    @property
    def hmac_key(self) -> bytes:
        """Return the HMAC key used for integrity checks."""
        return self._hmac_key

    @property
    def alert_manager(self) -> AlertManager | None:
        """Return the attached :class:`AlertManager`, if any."""
        return self._alert_manager

    @alert_manager.setter
    def alert_manager(self, value: AlertManager | None) -> None:
        """Attach or replace the alert manager after construction."""
        self._alert_manager = value

    @property
    def event_broadcaster(self) -> Any:
        """Return the attached realtime broadcaster, if any."""
        return self._event_broadcaster

    @event_broadcaster.setter
    def event_broadcaster(self, value: Any) -> None:
        """Attach or replace the realtime broadcaster after construction.

        Accepts an :class:`~doctoragent.api.broadcaster.EventBroadcaster` (or
        any duck-typed object exposing ``publish(event_type, data)``). When
        set, every successfully appended audit record is also published as a
        PHI-sanitised envelope. ``None`` disables the fan-out.
        """
        self._event_broadcaster = value

    @property
    def pending_alert_count(self) -> int:
        """Number of pending (unresolved) alerts.

        Delegates to the attached :class:`AlertManager` when present, else
        falls back to counting tasks that accumulated enough decrypt failures
        to trigger a CRITICAL alert (the legacy heuristic used by the tray).
        """
        if self._alert_manager is not None:
            return self._alert_manager.pending_count()
        return sum(1 for count in self._decrypt_failures.values() if count >= 3)

    def register_alert(self, callback: AlertCallback) -> None:
        """Register an alert callback.

        The callback receives ``(severity, event_type, details)`` when an
        alert rule fires.  Severity is one of ``CRITICAL``, ``HIGH``, or
        ``MEDIUM``.
        """
        self._alert_callbacks.append(callback)

    def _fire_alert(self, severity: str, event_type: str, details: dict[str, Any]) -> None:
        """Invoke all registered alert callbacks and the alert manager."""
        for cb in self._alert_callbacks:
            try:
                cb(severity, {"event_type": event_type, "details": details})
            except Exception:
                logging.exception("Alert callback raised an exception")
        # Route through the AlertManager for rate-limited desktop/webhook
        # delivery, aggregation and history.  Failures here must never break
        # the audit write path.
        if self._alert_manager is not None:
            try:
                self._alert_manager.handle_alert(severity, event_type, details)
            except Exception:
                logging.exception("AlertManager raised an exception")

    @staticmethod
    def _canonical(record: dict[str, Any]) -> str:
        """Canonical JSON representation for stable HMAC computation."""
        return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)

    def _sign(self, record: dict[str, Any]) -> str:
        canonical = self._canonical(record)
        return hmac.new(
            self._hmac_key,
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _last_hmac(self) -> str | None:
        """Return the HMAC of the most recent audit record, if any.

        Used to link each new record to its predecessor so the audit log
        forms a tamper-evident hash chain (delete/reorder/replay of whole
        entries becomes detectable). See issue #16.
        """
        last: str | None = None
        for _lineno, rec in self._iter_records():
            h = rec.get("hmac")
            if h:
                last = h
        return last

    def _redact_paths_in_details(self, details: dict[str, Any]) -> dict[str, Any]:
        """对敏感路径字段只保留 basename，避免泄露用户名/目录结构。

        仅处理 ``_PATH_FIELDS`` 中的顶层字段；其他字段原样保留。脱敏在
        计算 HMAC 之前完成，因此落盘记录即为脱敏后的形式，``verify()`` 仍能通过。
        """
        redacted: dict[str, Any] = {}
        for key, value in details.items():
            if key in _PATH_FIELDS and isinstance(value, str | Path):
                name = Path(str(value)).name
                redacted[key] = name if name else "<redacted>"
            else:
                redacted[key] = value
        return redacted

    def log(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        """Append an audit record to the log."""
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"Unsupported audit event type: {event_type!r}")

        details_resolved = details or {}
        if self._redact_paths:
            details_resolved = self._redact_paths_in_details(details_resolved)
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "details": details_resolved,
        }
        prev = self._last_hmac()
        if prev is not None:
            record["prev_hash"] = prev
        record["hmac"] = self._sign(record)

        try:
            self._append(record)
        except OSError:
            self._fire_alert("HIGH", event_type, details_resolved)
            raise

        # Realtime fan-out: publish a PHI-sanitised envelope so any
        # authenticated WebSocket / SSE subscriber receives a live signal.
        # Only ``event_type`` + ``severity`` + a small allowlist of non-PHI
        # scalar fields (counts/booleans) are forwarded — never patient_id,
        # free-text queries or clinical findings. Broadcast failures must
        # never break the audit write path.
        if self._event_broadcaster is not None:
            try:
                self._event_broadcaster.publish(
                    event_type,
                    self._sanitize_for_broadcast(event_type, details_resolved),
                )
            except Exception:  # noqa: BLE001 — broadcaster must never break audit
                logging.debug("audit broadcast failed", exc_info=True)

        self._check_alert_rules(event_type, details_resolved)

    @staticmethod
    def _sanitize_for_broadcast(event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        """Build a PHI-safe payload for the realtime broadcaster.

        Returns ``{severity, ...}`` where ``...`` is the intersection of
        *details* and :data:`_BROADCAST_SAFE_FIELDS` — counts, booleans and
        short enums only. PHI-bearing fields (``patient_id``, ``query``,
        ``findings``, ``warnings`` …) are deliberately dropped.
        """
        payload: dict[str, Any] = {
            "severity": AUDIT_EVENT_SEVERITY.get(event_type, "INFO"),
        }
        for key, value in details.items():
            if key in _BROADCAST_SAFE_FIELDS and isinstance(value, (str, int, float, bool)):
                payload[key] = value
        return payload

    def _rotated_path(self, index: int) -> Path:
        """Return the path for the *index*-th rotated log file (1-based).

        For ``audit.log.ndjson`` this yields ``audit.log.1.ndjson``,
        ``audit.log.2.ndjson``, …  preserving the original extension.
        """
        name = self.log_path.name
        stem, _, ext = name.rpartition(".")
        return self.log_path.with_name(f"{stem}.{index}.{ext}")

    def _rotate_log(self) -> None:
        """Rotate the current log file with incremental numbering.

        Existing rotated files are shifted up by one (``.1`` → ``.2``,
        ``.2`` → ``.3``, …) and the oldest file beyond
        ``_MAX_LOG_ROTATIONS`` is deleted.  The current log is then renamed
        to ``.1``.  This prevents data loss that would occur if every
        rotation simply overwrote ``.1``.
        """
        # Delete the oldest rotation if it exists (highest index).
        oldest = self._rotated_path(_MAX_LOG_ROTATIONS)
        if oldest.exists():
            oldest.unlink()
        # Shift each rotated file up by one, starting from the highest
        # surviving index down to 1, so we never clobber a file we still
        # need.
        for i in range(_MAX_LOG_ROTATIONS - 1, 0, -1):
            src = self._rotated_path(i)
            if src.exists():
                src.replace(self._rotated_path(i + 1))
        # Rotate the current log to .1.
        self.log_path.replace(self._rotated_path(1))

    def _append(self, record: dict[str, Any]) -> None:
        """Append a record to the audit log file."""
        line = json.dumps(record, default=str) + "\n"
        # Rotate if the log file exceeds the max size.
        if self.log_path.exists() and self.log_path.stat().st_size > _MAX_LOG_SIZE:
            self._rotate_log()
        # Ensure the log file is created with owner-only permissions (0o600)
        # so other users on the system cannot read the audit trail.  When the
        # file already exists its permissions are left untouched.
        if not self.log_path.exists():
            fd = os.open(
                str(self.log_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(fd)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _check_alert_rules(self, event_type: str, details: dict[str, Any]) -> None:
        """Evaluate built-in alert rules and fire callbacks when triggered."""
        # ── CRITICAL ────────────────────────────────────────────────
        if event_type == "decrypted":
            success = details.get("success", True)
            if not success:
                task_key = details.get("task_id", "__default__")
                count = self._decrypt_failures.get(task_key, 0) + 1
                self._decrypt_failures[task_key] = count
                if count >= 3:
                    self._fire_alert("CRITICAL", event_type, details)
            else:
                task_key = details.get("task_id", "__default__")
                self._decrypt_failures.pop(task_key, None)

        if event_type == "master_key_changed":
            self._fire_alert("CRITICAL", event_type, details)

        if event_type == "sandbox_escape_attempt":
            self._fire_alert("CRITICAL", event_type, details)

        # ── HIGH ───────────────────────────────────────────────────
        if event_type == "cloud_fallback_used":
            self._fire_alert("HIGH", event_type, details)

        if event_type in ("policy_violation", "offline_policy_violation"):
            self._fire_alert("HIGH", event_type, details)

        # ── MEDIUM ─────────────────────────────────────────────────
        if event_type == "sandbox_run_failed":
            self._fire_alert("MEDIUM", event_type, details)

        if event_type == "password_store_operation":
            self._fire_alert("MEDIUM", event_type, details)

        if event_type == "connection_tested":
            is_local = details.get("is_local", True)
            if not is_local and self._first_cloud_connection:
                self._first_cloud_connection = False
                self._fire_alert("MEDIUM", event_type, details)

        # ── Resource governance (Phase 6.6) ───────────────────────
        if event_type in ("resource_backpressure", "disk_watermark_exceeded"):
            self._fire_alert("HIGH", event_type, details)

        # ── Ecosystem integration (Phase 7) ───────────────────────
        # Plugin load failures and webhook dispatch failures are operationally
        # significant: a failed plugin may break a feature the user depends on,
        # and a failed webhook may drop a security alert on the floor.
        if event_type == "plugin_load_failed":
            self._fire_alert("HIGH", event_type, details)
        elif event_type == "plugin_loaded":
            self._fire_alert("MEDIUM", event_type, details)
        elif event_type == "webhook_failed":
            self._fire_alert("MEDIUM", event_type, details)
        elif event_type == "storage_backend_operation":
            # Storage ops are MEDIUM by default; callers can escalate via the
            # ``severity`` detail field when an operation fully fails.
            self._fire_alert(
                str(details.get("severity", "MEDIUM")).upper()
                if str(details.get("severity", "")).upper() in ("HIGH", "MEDIUM", "CRITICAL")
                else "MEDIUM",
                event_type,
                details,
            )

    def _iter_records(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(lineno, record)`` for every record across all log files.

        Rotated files are read oldest-first (highest index → lowest index)
        so that records are returned in chronological order.  Line numbers
        are global across all files so that :meth:`verify` line references
        remain unambiguous even when the log has been rotated.
        """
        # Collect rotated files that exist, sorted from oldest to newest
        # (highest index is the oldest rotation).
        rotated_files: list[Path] = []
        for i in range(_MAX_LOG_ROTATIONS, 0, -1):
            p = self._rotated_path(i)
            if p.exists():
                rotated_files.append(p)

        lineno = 0
        for log_file in [*rotated_files, self.log_path]:
            if not log_file.exists():
                continue
            with log_file.open("r", encoding="utf-8") as f:
                for line in f:
                    lineno += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logging.warning("Skipping corrupt audit log line %d", lineno)
                        continue
                    yield lineno, record

    def query(
        self,
        since: datetime | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit records with optional filtering.

        Records are returned in chronological order.
        """
        results: list[dict[str, Any]] = []
        for _lineno, record in self._iter_records():
            if event_type is not None and record.get("event_type") != event_type:
                continue
            if since is not None:
                ts = record.get("timestamp")
                if ts:
                    try:
                        record_ts = datetime.fromisoformat(str(ts))
                    except ValueError:
                        continue
                    if record_ts < since:
                        continue
            results.append(record)
            if len(results) >= limit:
                break
        return results

    def verify(self) -> tuple[bool, list[int]]:
        """Verify the integrity of all logged records.

        Checks BOTH per-record HMAC integrity AND chain continuity: each
        record's ``prev_hash`` must equal the preceding record's HMAC, so a
        deleted / reordered / replayed whole entry breaks the chain and is
        flagged. See issue #16.

        Returns ``(ok, invalid_line_numbers)``.
        """
        invalid: list[int] = []
        expected_prev: str | None = None
        for lineno, record in self._iter_records():
            record = record.copy()
            stored_hmac = record.pop("hmac", None)
            # The HMAC was computed over the record *including* prev_hash, so
            # keep prev_hash in place for the signature check and only read
            # it for the chain-continuity check below.
            prev_hash = record.get("prev_hash")
            if not hmac.compare_digest(self._sign(record), stored_hmac or ""):
                invalid.append(lineno)
                continue
            if (prev_hash or None) != expected_prev:
                # Predecessor mismatch / gap / reorder.
                invalid.append(lineno)
                continue
            expected_prev = stored_hmac
        return not invalid, invalid

    def _validate_time_range(
        self,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> None:
        if start_time is not None and end_time is not None and start_time >= end_time:
            raise ValueError("start_time must be before end_time")

    def _record_in_range(
        self,
        record: dict[str, Any],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> bool:
        ts = record.get("timestamp")
        if not ts:
            return False
        try:
            record_ts = datetime.fromisoformat(str(ts))
        except ValueError:
            return False
        if start_time is not None and record_ts < start_time:
            return False
        if end_time is not None and record_ts >= end_time:
            return False
        return True

    def export_logs(
        self,
        dest_path: Path,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        format: str = "ndjson",
    ) -> None:
        """Export audit log records to *dest_path*.

        Parameters
        ----------
        dest_path:
            Destination file path.
        start_time:
            Only include records at or after this time (inclusive).
        end_time:
            Only include records before this time (exclusive).
        format:
            Output format: ``"ndjson"`` (raw NDJSON with HMAC) or ``"csv"``
            (timestamp, event_type, details as JSON).
        """
        self._validate_time_range(start_time, end_time)

        # Verify integrity before exporting.
        ok, invalid = self.verify()
        if not ok:
            raise RuntimeError(f"Audit log integrity check failed. Tampered lines: {invalid}")

        if format not in ("ndjson", "csv"):
            raise ValueError(f"Unsupported export format: {format!r}")

        records: list[dict[str, Any]] = []
        for _lineno, record in self._iter_records():
            if self._record_in_range(record, start_time, end_time):
                records.append(record)

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if format == "ndjson":
            content = "\n".join(json.dumps(r, default=str) for r in records) + "\n"
            dest_path.write_text(content, encoding="utf-8")
        else:
            with dest_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "event_type", "details", "hmac"])
                for r in records:
                    writer.writerow(
                        [
                            r.get("timestamp", ""),
                            r.get("event_type", ""),
                            json.dumps(r.get("details", {}), default=str),
                            r.get("hmac", ""),
                        ]
                    )

    def statistics(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Return aggregate statistics for the audit log.

        Parameters
        ----------
        start_time:
            Only include records at or after this time (inclusive).
        end_time:
            Only include records before this time (exclusive).

        Returns
        -------
        dict with keys:
            ``total_events``, ``by_event_type``, ``by_severity``,
            ``active_periods``.
        """
        self._validate_time_range(start_time, end_time)

        total = 0
        by_event_type: dict[str, int] = {}
        # Derive the severity buckets from the canonical map so new severities
        # (e.g. clinical INFO events) propagate automatically instead of being
        # silently dropped by a stale local copy.
        by_severity: dict[str, int] = dict.fromkeys(sorted(set(AUDIT_EVENT_SEVERITY.values())), 0)

        timestamps: list[datetime] = []
        _max_timestamps = 10_000  # protect against unbounded memory with large logs

        for _lineno, record in self._iter_records():
            if not self._record_in_range(record, start_time, end_time):
                continue

            total += 1
            et = record.get("event_type", "unknown")
            by_event_type[et] = by_event_type.get(et, 0) + 1

            severity = AUDIT_EVENT_SEVERITY.get(et)
            if severity:
                by_severity[severity] = by_severity.get(severity, 0) + 1

            ts = record.get("timestamp")
            if ts and len(timestamps) < _max_timestamps:
                try:
                    record_ts = datetime.fromisoformat(str(ts))
                    timestamps.append(record_ts)
                except ValueError:
                    pass

        # Compute active periods: group consecutive timestamps within 30 minutes.
        active_periods: list[dict[str, str]] = []
        if timestamps:
            timestamps.sort()
            period_start = timestamps[0]
            period_end = timestamps[0]
            gap = 30 * 60  # 30 minutes in seconds

            for t in timestamps[1:]:
                if (t - period_end).total_seconds() <= gap:
                    period_end = t
                else:
                    active_periods.append(
                        {
                            "start": period_start.isoformat(),
                            "end": period_end.isoformat(),
                        }
                    )
                    period_start = t
                    period_end = t
            active_periods.append(
                {
                    "start": period_start.isoformat(),
                    "end": period_end.isoformat(),
                }
            )

        return {
            "total_events": total,
            "by_event_type": by_event_type,
            "by_severity": by_severity,
            "active_periods": active_periods,
        }

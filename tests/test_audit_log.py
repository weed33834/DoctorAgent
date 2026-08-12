"""Tests for the append-only audit logger."""

import csv
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from doctoragent.compat import UTC
from pathlib import Path
from typing import Any

import pytest

from doctoragent.config import AegisConfig
from doctoragent.security.audit_log import ALLOWED_EVENT_TYPES, AuditLogger


@pytest.fixture
def config(tmp_path: Path) -> AegisConfig:
    """Test configuration with isolated log path."""
    cfg = AegisConfig()
    cfg.paths.logs = tmp_path / "logs"
    return cfg


@pytest.fixture
def logger(config: AegisConfig) -> AuditLogger:
    """Audit logger with a deterministic HMAC key."""
    return AuditLogger(config, hmac_key=b"x" * 32)


def test_allowed_event_types_contains_required_events() -> None:
    """All required event types are allowed."""
    required = {
        "file_ingested",
        "classified",
        "encrypted",
        "decrypted",
        "connection_tested",
        "policy_violation",
        "login_attempt",
    }
    assert required <= ALLOWED_EVENT_TYPES

    # New alert-rule event types.
    new_events = {
        "master_key_changed",
        "sandbox_escape_attempt",
        "audit_write_failed",
        "password_store_operation",
        "sandbox_run_failed",
    }
    assert new_events <= ALLOWED_EVENT_TYPES


def test_log_appends_ndjson(config: AegisConfig, logger: AuditLogger) -> None:
    """Logging appends timestamped, HMAC-signed NDJSON records."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "1"})

    log_path = config.paths.logs / "audit.log.ndjson"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2

    record = json.loads(lines[0])
    assert record["event_type"] == "file_ingested"
    assert "timestamp" in record
    assert "hmac" in record
    assert record["details"] == {"task_id": "1"}


def test_log_rejects_unknown_event(logger: AuditLogger) -> None:
    """Unknown event types are rejected."""
    with pytest.raises(ValueError, match="Unsupported audit event type"):
        logger.log("unknown_event")


def test_query_by_event_type(config: AegisConfig, logger: AuditLogger) -> None:
    """query filters by event_type."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "1"})
    logger.log("file_ingested", {"task_id": "2"})

    results = logger.query(event_type="file_ingested")
    assert len(results) == 2
    assert all(r["event_type"] == "file_ingested" for r in results)


def test_query_since(config: AegisConfig, logger: AuditLogger) -> None:
    """query filters by minimum timestamp."""
    logger.log("file_ingested", {"task_id": "1"})

    future = datetime.now(UTC) + timedelta(hours=1)
    results = logger.query(since=future)
    assert results == []


def test_query_limit(config: AegisConfig, logger: AuditLogger) -> None:
    """query respects the limit parameter."""
    for i in range(5):
        logger.log("file_ingested", {"task_id": str(i)})

    results = logger.query(event_type="file_ingested", limit=2)
    assert len(results) == 2


def test_verify_valid_records(logger: AuditLogger) -> None:
    """verify returns True for untampered records."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "1"})

    ok, invalid = logger.verify()
    assert ok is True
    assert invalid == []


def test_verify_detects_deleted_middle_record(config: AegisConfig, logger: AuditLogger) -> None:
    """Regression for #16: a deleted whole entry breaks the hash chain."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "2"})
    logger.log("agent_initialized", {"task_id": "3"})

    log_path = config.paths.logs / "audit.log.ndjson"
    lines = log_path.read_text().strip().splitlines()
    del lines[1]  # remove the middle record
    log_path.write_text("\n".join(lines) + "\n")

    ok, invalid = logger.verify()
    assert ok is False
    assert 2 in invalid  # the record after the gap is flagged


def test_verify_detects_reordered_records(config: AegisConfig, logger: AuditLogger) -> None:
    """Regression for #16: reordering whole entries breaks the chain."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "2"})
    logger.log("agent_initialized", {"task_id": "3"})

    log_path = config.paths.logs / "audit.log.ndjson"
    lines = log_path.read_text().strip().splitlines()
    log_path.write_text("\n".join(reversed(lines)) + "\n")

    ok, invalid = logger.verify()
    assert ok is False
    assert invalid


def test_verify_detects_tampering(config: AegisConfig, logger: AuditLogger) -> None:
    """verify flags lines whose payload was modified."""
    logger.log("file_ingested", {"task_id": "1"})

    log_path = config.paths.logs / "audit.log.ndjson"
    text = log_path.read_text()
    tampered = text.replace('"task_id": "1"', '"task_id": "2"')
    log_path.write_text(tampered)

    ok, invalid = logger.verify()
    assert ok is False
    assert invalid == [1]


def test_verify_detects_missing_hmac(config: AegisConfig) -> None:
    """verify flags lines missing the HMAC field."""
    log_path = config.paths.logs / "audit.log.ndjson"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "file_ingested",
        "details": {},
    }
    log_path.write_text(json.dumps(record) + "\n")

    logger = AuditLogger(config, hmac_key=b"x" * 32)
    ok, invalid = logger.verify()
    assert ok is False
    assert invalid == [1]


def test_key_persistence(config: AegisConfig) -> None:
    """The HMAC key is persisted across AuditLogger instances."""
    logger1 = AuditLogger(config)
    key1 = logger1.hmac_key
    logger2 = AuditLogger(config)
    assert logger2.hmac_key == key1


# ── Alert tests ────────────────────────────────────────────────────────


def test_register_alert_callback_fires_on_critical(logger: AuditLogger) -> None:
    """Alert callback fires when a CRITICAL-level event is logged."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("master_key_changed", {"provider": "tpm"})

    assert len(alerts) == 1
    assert alerts[0][0] == "CRITICAL"
    assert alerts[0][1]["event_type"] == "master_key_changed"


def test_register_alert_callback_fires_on_high(logger: AuditLogger) -> None:
    """Alert callback fires when a HIGH-level event is logged."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("policy_violation", {"operation": "test"})

    assert len(alerts) == 1
    assert alerts[0][0] == "HIGH"


def test_register_alert_callback_fires_on_medium(logger: AuditLogger) -> None:
    """Alert callback fires when a MEDIUM-level event is logged."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("password_store_operation", {"entry": "test"})

    assert len(alerts) == 1
    assert alerts[0][0] == "MEDIUM"


def test_decrypt_failure_three_consecutive_triggers_critical(
    logger: AuditLogger,
) -> None:
    """Three consecutive decrypt failures trigger a CRITICAL alert."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)

    # Two failures — no alert yet.
    logger.log("decrypted", {"success": False, "task_id": "1"})
    logger.log("decrypted", {"success": False, "task_id": "1"})
    assert len(alerts) == 0

    # Third failure triggers the alert.
    logger.log("decrypted", {"success": False, "task_id": "1"})
    assert len(alerts) == 1
    assert alerts[0][0] == "CRITICAL"


def test_decrypt_failure_resets_on_success(logger: AuditLogger) -> None:
    """A successful decrypt resets the failure counter."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)

    logger.log("decrypted", {"success": False, "task_id": "1"})
    logger.log("decrypted", {"success": False, "task_id": "1"})
    # Success resets the counter.
    logger.log("decrypted", {"success": True, "task_id": "1"})

    # Start failing again — needs 3 more.
    logger.log("decrypted", {"success": False, "task_id": "1"})
    logger.log("decrypted", {"success": False, "task_id": "1"})
    assert len(alerts) == 0

    logger.log("decrypted", {"success": False, "task_id": "1"})
    assert len(alerts) == 1
    assert alerts[0][0] == "CRITICAL"


def test_sandbox_escape_triggers_critical(logger: AuditLogger) -> None:
    """Sandbox escape attempt triggers CRITICAL alert."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("sandbox_escape_attempt", {"method": "ptrace"})

    assert len(alerts) == 1
    assert alerts[0][0] == "CRITICAL"


def test_cloud_fallback_triggers_high(logger: AuditLogger) -> None:
    """Cloud fallback usage triggers HIGH alert."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("cloud_fallback_used", {"url": "https://api.example.com"})

    assert len(alerts) == 1
    assert alerts[0][0] == "HIGH"


def test_offline_policy_violation_triggers_high(logger: AuditLogger) -> None:
    """Offline policy violation triggers HIGH alert."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("offline_policy_violation", {"operation": "decrypt"})

    assert len(alerts) == 1
    assert alerts[0][0] == "HIGH"


def test_sandbox_run_failed_triggers_medium(logger: AuditLogger) -> None:
    """Sandbox run failure triggers MEDIUM alert."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)
    logger.log("sandbox_run_failed", {"reason": "bwrap not found"})

    assert len(alerts) == 1
    assert alerts[0][0] == "MEDIUM"


def test_first_cloud_connection_triggers_medium(logger: AuditLogger) -> None:
    """First non-local connection test triggers MEDIUM alert."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def capture(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(capture)

    # Local connection does not trigger.
    logger.log("connection_tested", {"is_local": True})
    assert len(alerts) == 0

    # First non-local triggers.
    logger.log("connection_tested", {"is_local": False, "host": "api.example.com"})
    assert len(alerts) == 1
    assert alerts[0][0] == "MEDIUM"

    # Second non-local does not trigger again.
    logger.log("connection_tested", {"is_local": False, "host": "other.example.com"})
    assert len(alerts) == 1


def test_multiple_callbacks_all_fire(logger: AuditLogger) -> None:
    """Multiple registered callbacks all receive the alert."""
    results: list[list[tuple[str, dict[str, Any]]]] = []

    for i in range(3):

        def make_cb(idx: int = i) -> Callable[[str, dict[str, Any]], None]:
            def cb(severity: str, payload: dict[str, Any]) -> None:
                results[idx].append((severity, payload))

            return cb

        results.append([])
        logger.register_alert(make_cb())

    logger.log("sandbox_escape_attempt", {"method": "shellcode"})

    assert all(len(r) == 1 for r in results)
    assert all(r[0][0] == "CRITICAL" for r in results)


def test_alert_callback_exception_does_not_block_others(
    logger: AuditLogger,
) -> None:
    """A callback raising an exception does not prevent others from firing."""
    alerts: list[tuple[str, dict[str, Any]]] = []

    def bad_callback(severity: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("callback failed")

    def good_callback(severity: str, payload: dict[str, Any]) -> None:
        alerts.append((severity, payload))

    logger.register_alert(bad_callback)
    logger.register_alert(good_callback)
    logger.log("master_key_changed", {})

    assert len(alerts) == 1


# ── Export tests ───────────────────────────────────────────────────────


def test_export_ndjson(logger: AuditLogger, tmp_path: Path) -> None:
    """Export produces valid NDJSON with all records."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "1"})

    dest = tmp_path / "export.ndjson"
    logger.export_logs(dest, format="ndjson")

    content = dest.read_text()
    lines = [line for line in content.strip().split("\n") if line]
    assert len(lines) == 2

    for line in lines:
        record = json.loads(line)
        assert "hmac" in record
        assert "timestamp" in record
        assert "event_type" in record


def test_export_csv(logger: AuditLogger, tmp_path: Path) -> None:
    """Export produces valid CSV with header and rows."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "2"})

    dest = tmp_path / "export.csv"
    logger.export_logs(dest, format="csv")

    with dest.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2
    assert rows[0]["event_type"] == "file_ingested"
    assert rows[1]["event_type"] == "encrypted"
    assert set(reader.fieldnames or []) == {"timestamp", "event_type", "details", "hmac"}


def test_export_time_filter(logger: AuditLogger, tmp_path: Path) -> None:
    """Export filters records by time range."""
    # Log one record.
    logger.log("file_ingested", {"task_id": "1"})

    # Wait a tiny bit to ensure timestamp difference.
    import time

    time.sleep(0.01)

    after = datetime.now(UTC)

    dest = tmp_path / "export_filtered.ndjson"
    logger.export_logs(dest, start_time=after, format="ndjson")

    content = dest.read_text().strip()
    assert content == ""


def test_export_rejects_invalid_format(logger: AuditLogger, tmp_path: Path) -> None:
    """Export raises ValueError for unknown format."""
    dest = tmp_path / "invalid.xyz"
    with pytest.raises(ValueError, match="Unsupported export format"):
        logger.export_logs(dest, format="json")


def test_export_rejects_invalid_time_range(
    logger: AuditLogger,
    tmp_path: Path,
) -> None:
    """Export raises ValueError when start_time >= end_time."""
    now = datetime.now(UTC)
    dest = tmp_path / "export.ndjson"
    with pytest.raises(ValueError, match="start_time must be before end_time"):
        logger.export_logs(dest, start_time=now, end_time=now)


def test_export_verifies_integrity(
    config: AegisConfig,
    logger: AuditLogger,
    tmp_path: Path,
) -> None:
    """Export fails when audit log has been tampered."""
    logger.log("file_ingested", {"task_id": "1"})

    log_path = config.paths.logs / "audit.log.ndjson"
    text = log_path.read_text()
    tampered = text.replace('"task_id": "1"', '"task_id": "2"')
    log_path.write_text(tampered)

    dest = tmp_path / "export.ndjson"
    with pytest.raises(RuntimeError, match="integrity check failed"):
        logger.export_logs(dest)


# ── Statistics tests ───────────────────────────────────────────────────


def test_statistics_empty_log(logger: AuditLogger) -> None:
    """Statistics on an empty log returns zeros.

    The severity buckets are derived from the canonical
    :data:`AUDIT_EVENT_SEVERITY` map (CRITICAL / HIGH / MEDIUM / INFO), so
    INFO-level events such as ``clinical_decision`` are counted rather than
    silently dropped by a stale local copy.
    """
    from doctoragent.security.audit_log import AUDIT_EVENT_SEVERITY

    stats = logger.statistics()
    assert stats["total_events"] == 0
    assert stats["by_event_type"] == {}
    assert stats["by_severity"] == {sev: 0 for sev in sorted(set(AUDIT_EVENT_SEVERITY.values()))}
    assert stats["active_periods"] == []


def test_statistics_counts(logger: AuditLogger) -> None:
    """Statistics correctly counts events by type and severity."""
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("file_ingested", {"task_id": "2"})
    logger.log("policy_violation", {"operation": "test"})
    logger.log("master_key_changed", {"provider": "tpm"})

    stats = logger.statistics()

    assert stats["total_events"] == 4
    assert stats["by_event_type"] == {
        "file_ingested": 2,
        "policy_violation": 1,
        "master_key_changed": 1,
    }
    assert stats["by_severity"]["HIGH"] == 1
    assert stats["by_severity"]["CRITICAL"] == 1


def test_statistics_counts_clinical_events(logger: AuditLogger) -> None:
    """Clinical audit events are counted in by_severity.

    Regression guard: a stale local severity_map previously omitted the
    clinical event types, so ``clinical_safety_alert`` (CRITICAL) and
    ``clinical_decision`` (INFO) were silently dropped from statistics.
    """
    logger.log("clinical_safety_alert", {"patient_id": "P1", "findings": []})
    logger.log("clinical_decision", {"patient_id": "P1", "query": "q", "finding_count": 0})
    logger.log("clinical_guardrail_action", {"patient_id": "P1", "action": "flag"})

    stats = logger.statistics()

    assert stats["total_events"] == 3
    assert stats["by_event_type"] == {
        "clinical_safety_alert": 1,
        "clinical_decision": 1,
        "clinical_guardrail_action": 1,
    }
    assert stats["by_severity"]["CRITICAL"] == 1  # clinical_safety_alert
    assert stats["by_severity"]["HIGH"] == 1  # clinical_guardrail_action
    assert stats["by_severity"]["INFO"] == 1  # clinical_decision


def test_statistics_active_periods(logger: AuditLogger) -> None:
    """Statistics reports active periods from event timestamps."""
    # Log a single event; it should produce one active period.
    logger.log("file_ingested", {"task_id": "1"})

    stats = logger.statistics()
    assert len(stats["active_periods"]) == 1
    period = stats["active_periods"][0]
    assert "start" in period
    assert "end" in period


def test_statistics_time_filter(logger: AuditLogger) -> None:
    """Statistics respects time range filters."""
    logger.log("file_ingested", {"task_id": "1"})

    future = datetime.now(UTC) + timedelta(hours=1)
    stats = logger.statistics(start_time=future)
    assert stats["total_events"] == 0


def test_statistics_rejects_invalid_time_range(logger: AuditLogger) -> None:
    """Statistics raises ValueError when start_time >= end_time."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="start_time must be before end_time"):
        logger.statistics(start_time=now, end_time=now)

"""Tests for resource governance: disk watermark and backpressure (Phase 6.6)."""

from pathlib import Path
from unittest.mock import patch

from doctoragent.config import AegisConfig
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.resources import (
    BackpressureGuard,
    DiskWatermarkChecker,
    disk_usage_percent,
)


def test_disk_usage_percent_returns_value_for_existing_path(tmp_path: Path) -> None:
    """An existing path reports a usage percentage in [0, 100]."""
    pct = disk_usage_percent(tmp_path)
    assert 0.0 <= pct <= 100.0


def test_disk_usage_percent_nonexistent_uses_ancestor(tmp_path: Path) -> None:
    """A non-existent path resolves to the nearest existing ancestor device."""
    pct = disk_usage_percent(tmp_path / "does" / "not" / "exist")
    assert 0.0 <= pct <= 100.0


def test_disk_watermark_rising_edge_emits_single_audit_event(tmp_path: Path) -> None:
    """Crossing the threshold emits one audit event; sustained usage does not spam."""
    config = AegisConfig()
    config.paths.logs = tmp_path / "logs"
    audit = AuditLogger(config, hmac_key=b"k" * 32)
    checker = DiskWatermarkChecker(tmp_path, threshold_percent=90.0, audit_logger=audit)

    with patch("doctoragent.security.resources.disk_usage_percent", return_value=95.0):
        exceeded, percent = checker.check()
    assert exceeded
    assert percent == 95.0
    assert checker.alerted
    records = audit.query(event_type="disk_watermark_exceeded")
    assert len(records) == 1

    # A second check while still over the watermark must not emit again.
    with patch("doctoragent.security.resources.disk_usage_percent", return_value=95.0):
        checker.check()
    assert len(audit.query(event_type="disk_watermark_exceeded")) == 1


def test_disk_watermark_clears_when_usage_drops(tmp_path: Path) -> None:
    """Dropping below the threshold resets the alerted state without a new event."""
    config = AegisConfig()
    config.paths.logs = tmp_path / "logs"
    audit = AuditLogger(config, hmac_key=b"k" * 32)
    checker = DiskWatermarkChecker(tmp_path, threshold_percent=90.0, audit_logger=audit)

    with patch("doctoragent.security.resources.disk_usage_percent", return_value=95.0):
        checker.check()
    assert checker.alerted

    with patch("doctoragent.security.resources.disk_usage_percent", return_value=80.0):
        exceeded, _ = checker.check()
    assert not exceeded
    assert not checker.alerted
    # No additional event was emitted during clearing.
    assert len(audit.query(event_type="disk_watermark_exceeded")) == 1


def test_disk_watermark_without_audit_logger_does_not_raise(tmp_path: Path) -> None:
    """A checker with no audit logger still reports status via logging only."""
    checker = DiskWatermarkChecker(tmp_path, threshold_percent=90.0, audit_logger=None)
    with patch("doctoragent.security.resources.disk_usage_percent", return_value=95.0):
        exceeded, _ = checker.check()
    assert exceeded
    assert checker.alerted


def test_backpressure_guard_rejects_low_above_high() -> None:
    """low_watermark greater than high_watermark is a configuration error."""
    try:
        BackpressureGuard(high_watermark=1, low_watermark=2)
    except ValueError:
        return
    raise AssertionError("Expected ValueError when low > high")


def test_backpressure_guard_pause_and_resume_cycle() -> None:
    """The guard pauses at the high watermark and resumes at the low watermark."""
    guard = BackpressureGuard(high_watermark=3, low_watermark=1)

    assert not guard.on_schedule()  # pending 1
    assert not guard.on_schedule()  # pending 2
    crossed = guard.on_schedule()  # pending 3 -> crossed
    assert crossed
    assert guard.paused
    assert guard.pending == 3

    # Start processing events: pending drains.
    guard.on_start()  # pending 2
    assert not guard.on_done()  # 2 > low(1), do not resume yet

    guard.on_start()  # pending 1
    resumed = guard.on_done()  # 1 <= low(1), resume
    assert resumed
    assert not guard.paused


def test_backpressure_guard_on_start_clamps_at_zero() -> None:
    """on_start without a matching schedule does not drive pending negative."""
    guard = BackpressureGuard(high_watermark=10, low_watermark=5)
    guard.on_start()
    assert guard.pending == 0

"""Resource governance: disk watermark monitoring and inbox backpressure.

These helpers implement the resource-limit and backpressure controls from
Phase 6.6. They are intentionally dependency-free and side-effect-light so
they can be unit-tested in isolation and wired into the agent/pipeline with
minimal coupling.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doctoragent.security.audit_log import AuditLogger

logger = logging.getLogger(__name__)


def disk_usage_percent(path: Path) -> float:
    """Return filesystem usage percentage for the device holding *path*.

    If *path* does not yet exist, the nearest existing ancestor is used so a
    freshly-configured Vault directory still reports the right device. Returns
    ``0.0`` when usage cannot be determined (e.g. the filesystem reports zero
    total bytes or the path cannot be resolved).
    """
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            # Reached the filesystem root without finding an existing ancestor.
            return 0.0
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(str(candidate))
    except OSError:
        return 0.0
    if usage.total <= 0:
        return 0.0
    return (usage.used / usage.total) * 100.0


class DiskWatermarkChecker:
    """Watch a directory's device and alert when usage crosses a threshold.

    Emits a single ``disk_watermark_exceeded`` audit event on the rising edge
    so a sustained full-disk condition does not spam the audit log, and clears
    the internal state when usage drops back below the threshold.
    """

    def __init__(
        self,
        path: Path,
        threshold_percent: float,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._path = path
        self._threshold = threshold_percent
        self._audit_logger = audit_logger
        self._alerted = False
        self._lock = threading.Lock()

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def alerted(self) -> bool:
        with self._lock:
            return self._alerted

    def check(self) -> tuple[bool, float]:
        """Check current usage. Returns ``(exceeded, percent)``."""
        percent = disk_usage_percent(self._path)
        exceeded = percent >= self._threshold
        with self._lock:
            if exceeded and not self._alerted:
                self._alerted = True
                self._emit_alert(percent)
            elif not exceeded and self._alerted:
                self._alerted = False
                logger.info(
                    "Disk usage for %s dropped below watermark (%.1f%% < %.1f%%)",
                    self._path,
                    percent,
                    self._threshold,
                )
        return exceeded, percent

    def _emit_alert(self, percent: float) -> None:
        logger.warning(
            "Disk usage for %s exceeded watermark (%.1f%% >= %.1f%%)",
            self._path,
            percent,
            self._threshold,
        )
        if self._audit_logger is not None:
            self._audit_logger.log(
                "disk_watermark_exceeded",
                {
                    "path": str(self._path),
                    "percent": round(percent, 2),
                    "threshold": self._threshold,
                },
            )


class BackpressureGuard:
    """Track queued Inbox events and signal when ingestion should pause.

    The guard counts *pending* events (scheduled but not yet started) and
    applies hysteresis: ingestion pauses when pending reaches the high
    watermark and resumes only once pending drops to the low watermark. This
    prevents the Inbox from being accepted faster than it can be processed
    without dropping files — a paused watcher keeps new files in the Inbox
    until the backlog drains.
    """

    def __init__(self, high_watermark: int, low_watermark: int) -> None:
        if low_watermark > high_watermark:
            raise ValueError("low_watermark must not exceed high_watermark")
        self._high = high_watermark
        self._low = low_watermark
        self._pending = 0
        self._paused = False
        self._lock = threading.Lock()

    @property
    def high_watermark(self) -> int:
        return self._high

    @property
    def low_watermark(self) -> int:
        return self._low

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def on_schedule(self) -> bool:
        """Record a newly scheduled event.

        Returns ``True`` when the high watermark was just crossed and the
        caller should pause ingestion.
        """
        with self._lock:
            self._pending += 1
            if not self._paused and self._pending >= self._high:
                self._paused = True
                return True
            return False

    def on_start(self) -> None:
        """Record that a scheduled event has started processing."""
        with self._lock:
            if self._pending > 0:
                self._pending -= 1

    def on_done(self) -> bool:
        """Record that an event finished processing.

        Returns ``True`` when pending has drained to the low watermark and the
        caller should resume ingestion.
        """
        with self._lock:
            if self._paused and self._pending <= self._low:
                self._paused = False
                return True
            return False

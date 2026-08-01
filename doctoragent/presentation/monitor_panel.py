"""System health monitoring panel (PyQt6).

Provides a live dashboard of DoctorAgent's operational health:

* **Disk water level** – Vault directory size, device usage percent, and
  available space, with a warning highlight when usage exceeds the
  configured watermark.
* **Task statistics** – pending / in-progress / completed / failed /
  quarantined counts queried from :class:`TaskStore`.
* **Backpressure status** – whether ingestion is paused and how many
  events are pending, from :class:`BackpressureGuard`.
* **Connection health** – configured :class:`ConnectionManager`
  connections and their enabled / local-trust status.

The panel auto-refreshes every 30 seconds via a :class:`QTimer`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via stubs in tests
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent.orchestration.state_machine import TaskState
from doctoragent.presentation.utils import human_size

# Refresh interval in milliseconds (30 seconds).
_REFRESH_INTERVAL_MS = 30_000


class MonitorPanel(QWidget):
    """Live system health monitoring widget."""

    def __init__(
        self,
        config: Any = None,
        task_store: Any = None,
        connection_manager: Any = None,
        backpressure_guard: Any = None,
        audit_logger: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.task_store = task_store
        self.connection_manager = connection_manager
        self.backpressure_guard = backpressure_guard
        self.audit_logger = audit_logger

        self.setWindowTitle("DoctorAgent Monitor")
        self.setMinimumSize(600, 450)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_disk_group())
        layout.addWidget(self._build_tasks_group())
        layout.addWidget(self._build_backpressure_group())
        layout.addWidget(self._build_connections_group())

        refresh_row = QHBoxLayout()
        self.refresh_button = QPushButton("🔄 Refresh Now")
        self.refresh_button.clicked.connect(self.refresh)
        refresh_row.addWidget(self.refresh_button)
        refresh_row.addStretch()
        self.last_refresh_label = QLabel("Last refresh: —")
        refresh_row.addWidget(self.last_refresh_label)
        layout.addLayout(refresh_row)

        # Auto-refresh timer.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(_REFRESH_INTERVAL_MS)

        # Populate initial values.
        self.refresh()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_disk_group(self) -> QGroupBox:
        """Build the disk water-level section."""
        group = QGroupBox("💾 Disk Water Level")
        grid = QGridLayout(group)

        self.vault_path_label = QLabel("—")
        self.vault_size_label = QLabel("—")
        self.disk_usage_label = QLabel("—")
        self.disk_available_label = QLabel("—")
        self.watermark_label = QLabel("—")

        grid.addWidget(QLabel("Vault Path:"), 0, 0)
        grid.addWidget(self.vault_path_label, 0, 1)
        grid.addWidget(QLabel("Vault Size:"), 1, 0)
        grid.addWidget(self.vault_size_label, 1, 1)
        grid.addWidget(QLabel("Device Usage:"), 2, 0)
        grid.addWidget(self.disk_usage_label, 2, 1)
        grid.addWidget(QLabel("Available:"), 3, 0)
        grid.addWidget(self.disk_available_label, 3, 1)
        grid.addWidget(QLabel("Watermark:"), 4, 0)
        grid.addWidget(self.watermark_label, 4, 1)

        return group

    def _build_tasks_group(self) -> QGroupBox:
        """Build the task statistics section."""
        group = QGroupBox("📋 Task Statistics")
        grid = QGridLayout(group)

        self.task_labels: dict[str, QLabel] = {}
        states = [
            (TaskState.IDLE.name, "⏳ Pending"),
            (TaskState.CLASSIFYING.name, "🔍 Classifying"),
            (TaskState.ENCRYPTING.name, "🔒 Encrypting"),
            (TaskState.INDEXING.name, "📊 Indexing"),
            (TaskState.COMPLETED.name, "✅ Completed"),
            (TaskState.FAILED.name, "❌ Failed"),
            (TaskState.QUARANTINED.name, "⚠️ Quarantined"),
        ]
        for row, (state_key, display) in enumerate(states):
            grid.addWidget(QLabel(display), row, 0)
            value_label = QLabel("0")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.task_labels[state_key] = value_label
            grid.addWidget(value_label, row, 1)

        self.task_total_label = QLabel("Total: 0")
        grid.addWidget(self.task_total_label, len(states), 0, 1, 2)

        return group

    def _build_backpressure_group(self) -> QGroupBox:
        """Build the backpressure status section."""
        group = QGroupBox("🚦 Backpressure Status")
        form = QVBoxLayout(group)

        self.bp_paused_label = QLabel("Paused: —")
        self.bp_pending_label = QLabel("Pending events: —")
        self.bp_watermark_label = QLabel("Watermarks: —")

        form.addWidget(self.bp_paused_label)
        form.addWidget(self.bp_pending_label)
        form.addWidget(self.bp_watermark_label)

        return group

    def _build_connections_group(self) -> QGroupBox:
        """Build the connection health section."""
        group = QGroupBox("🔌 Connection Health")
        layout = QVBoxLayout(group)
        self.connections_label = QLabel("—")
        self.connections_label.setWordWrap(True)
        layout.addWidget(self.connections_label)
        return group

    # ── Refresh logic ────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-query all backends and update the labels."""
        self._refresh_disk()
        self._refresh_tasks()
        self._refresh_backpressure()
        self._refresh_connections()
        from datetime import datetime

        self.last_refresh_label.setText(
            f"Last refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _vault_path(self) -> Path:
        """Return the configured vault path (or a sensible default)."""
        if self.config is not None:
            return Path(self.config.paths.vault)
        return Path.home() / "DoctorAgent" / "Vault"

    def _refresh_disk(self) -> None:
        """Update the disk water-level labels."""
        vault = self._vault_path()
        self.vault_path_label.setText(str(vault))

        # Vault directory size.
        if vault.exists():
            total_bytes = sum(f.stat().st_size for f in vault.rglob("*") if f.is_file())
            self.vault_size_label.setText(human_size(total_bytes))
        else:
            self.vault_size_label.setText("0 B (directory not created)")

        # Device-level usage.
        try:
            from doctoragent.security.resources import disk_usage_percent

            usage_percent = disk_usage_percent(vault)
        except Exception:
            usage_percent = 0.0

        try:
            candidate = vault
            while not candidate.exists():
                if candidate.parent == candidate:
                    break
                candidate = candidate.parent
            usage = shutil.disk_usage(str(candidate))
            available = usage.free
            total = usage.total
        except OSError:
            available = 0
            total = 0

        self.disk_usage_label.setText(f"{usage_percent:.1f}%")
        self.disk_available_label.setText(
            f"{human_size(available)} free / {human_size(total)} total"
        )

        # Watermark threshold and alert state.
        threshold = None
        if self.config is not None:
            threshold = getattr(self.config.resources, "disk_watermark_percent", None)
        if threshold is not None:
            self.watermark_label.setText(f"{threshold:.1f}% threshold")
            if usage_percent >= threshold:
                self.disk_usage_label.setStyleSheet("color: red; font-weight: bold;")
            else:
                self.disk_usage_label.setStyleSheet("")
        else:
            self.watermark_label.setText("not configured")

    def _refresh_tasks(self) -> None:
        """Update the task statistics labels."""
        if self.task_store is None:
            for label in self.task_labels.values():
                label.setText("N/A")
            self.task_total_label.setText("Total: N/A (task store not configured)")
            return

        try:
            counts = self.task_store.counts_by_state()
        except Exception:
            counts = {}

        total = 0
        for state_key, label in self.task_labels.items():
            value = counts.get(state_key, 0)
            label.setText(str(value))
            total += value
        self.task_total_label.setText(f"Total: {total}")

    def _refresh_backpressure(self) -> None:
        """Update the backpressure status labels."""
        if self.backpressure_guard is None:
            self.bp_paused_label.setText("Paused: N/A (not configured)")
            self.bp_pending_label.setText("Pending events: N/A")
            self.bp_watermark_label.setText("Watermarks: N/A")
            return

        try:
            paused = self.backpressure_guard.paused
            pending = self.backpressure_guard.pending
            high = self.backpressure_guard.high_watermark
            low = self.backpressure_guard.low_watermark
        except Exception:
            paused = False
            pending = 0
            high = 0
            low = 0

        paused_text = "YES ⛔" if paused else "No ✅"
        self.bp_paused_label.setText(f"Paused: {paused_text}")
        self.bp_pending_label.setText(f"Pending events: {pending}")
        self.bp_watermark_label.setText(f"Watermarks: high={high}, low={low}")
        if paused:
            self.bp_paused_label.setStyleSheet("color: orange; font-weight: bold;")
        else:
            self.bp_paused_label.setStyleSheet("")

    def _refresh_connections(self) -> None:
        """Update the connection health summary."""
        if self.connection_manager is None:
            self.connections_label.setText("N/A (connection manager not configured)")
            return

        try:
            connections = self.connection_manager.list_all()
        except Exception:
            connections = []

        if not connections:
            self.connections_label.setText("No connections configured.")
            return

        lines: list[str] = []
        for conn in connections:
            status_parts: list[str] = []
            if not conn.is_enabled:
                status_parts.append("disabled")
            else:
                status_parts.append("enabled")
            if conn.is_trusted_local():
                status_parts.append("local-trusted")
            platform = getattr(conn.platform_type, "value", str(conn.platform_type))
            lines.append(f"• {conn.name} ({platform}) — {', '.join(status_parts)}")
        self.connections_label.setText("\n".join(lines))

    # ── Lifecycle ────────────────────────────────────────────────────────

    def stop_timer(self) -> None:
        """Stop the auto-refresh timer (call before destroying the panel)."""
        self._timer.stop()

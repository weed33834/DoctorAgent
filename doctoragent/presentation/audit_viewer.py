"""Audit log viewer dialog (PyQt6).

Provides a filterable, exportable view of the tamper-evident audit log.
All backend access goes through the injected ``AuditLogger`` instance so the
dialog is testable without touching the real log files.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtCore import QDateTime
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QDateTimeEdit,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via stubs in tests
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent.compat import UTC

# Severity levels exposed in the filter dropdown. The first entry is the
# "no filter" sentinel; the rest map to the severities returned by
# ``AuditLogger.statistics()["by_severity"]``.
_SEVERITY_FILTERS: list[str] = ["All", "CRITICAL", "HIGH", "MEDIUM"]


class AuditLogViewerDialog(QDialog):
    """Dialog to view, filter and export audit log records."""

    def __init__(self, audit_logger: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.audit_logger = audit_logger
        self.setWindowTitle("Audit Log Viewer")
        self.setMinimumSize(900, 600)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_filter_group())
        layout.addWidget(self._build_stats_group())
        layout.addWidget(self._build_log_table(), stretch=1)
        layout.addLayout(self._build_button_row())

        self._refresh()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_filter_group(self) -> QGroupBox:
        """Build the filter bar with time range, event type, severity, keyword."""
        group = QGroupBox("Filters")
        form = QFormLayout(group)

        now = QDateTime.currentDateTime()
        week_ago = QDateTime(now.toPython() - timedelta(days=7))

        self.start_time_edit = QDateTimeEdit(week_ago)
        self.start_time_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_time_edit.setCalendarPopup(True)
        form.addRow("Start:", self.start_time_edit)

        self.end_time_edit = QDateTimeEdit(now)
        self.end_time_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.end_time_edit.setCalendarPopup(True)
        form.addRow("End:", self.end_time_edit)

        self.event_type_combo = QComboBox()
        self.event_type_combo.addItem("All")
        event_types = self._available_event_types()
        self.event_type_combo.addItems(event_types)
        form.addRow("Event Type:", self.event_type_combo)

        self.severity_combo = QComboBox()
        self.severity_combo.addItems(_SEVERITY_FILTERS)
        form.addRow("Severity:", self.severity_combo)

        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("Keyword in details (optional)")
        form.addRow("Keyword:", self.keyword_input)

        apply_button = QPushButton("Apply Filters")
        apply_button.clicked.connect(self._refresh)
        form.addRow("", apply_button)

        return group

    def _build_stats_group(self) -> QGroupBox:
        """Build the statistics summary panel."""
        group = QGroupBox("Statistics")
        layout = QVBoxLayout(group)
        self.stats_label = QLabel("Loading statistics...")
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)
        return group

    def _build_log_table(self) -> QTableWidget:
        """Build the audit log table."""
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Time", "Event Type", "Severity", "Details"])
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return self.table

    def _build_button_row(self) -> QHBoxLayout:
        """Build the action button row."""
        row = QHBoxLayout()

        self.refresh_button = QPushButton("🔄 Refresh")
        self.refresh_button.clicked.connect(self._refresh)
        row.addWidget(self.refresh_button)

        self.export_button = QPushButton("📤 Export Logs...")
        self.export_button.clicked.connect(self._export_logs)
        row.addWidget(self.export_button)

        self.verify_button = QPushButton("🔒 Verify Integrity")
        self.verify_button.clicked.connect(self._verify_integrity)
        row.addWidget(self.verify_button)

        row.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)

        return row

    # ── Backend interaction ──────────────────────────────────────────────

    def _available_event_types(self) -> list[str]:
        """Return the sorted list of known audit event types."""
        try:
            from doctoragent.security.audit_log import ALLOWED_EVENT_TYPES

            return sorted(ALLOWED_EVENT_TYPES)
        except Exception:
            return []

    def _build_query_params(self) -> dict[str, Any]:
        """Collect filter values into a parameter dict for querying."""
        start_dt = self._datetime_from_edit(self.start_time_edit)
        end_dt = self._datetime_from_edit(self.end_time_edit)
        event_type = self.event_type_combo.currentText()
        severity = self.severity_combo.currentText()
        keyword = self.keyword_input.text().strip()
        return {
            "start_time": start_dt,
            "end_time": end_dt,
            "event_type": event_type if event_type != "All" else None,
            "severity": severity if severity != "All" else None,
            "keyword": keyword or None,
        }

    def _datetime_from_edit(self, edit: QDateTimeEdit) -> datetime | None:
        """Extract a timezone-aware datetime from a QDateTimeEdit, or None."""
        try:
            qdt = edit.dateTime()
            py_dt = qdt.toPython()
            if py_dt.tzinfo is None:
                py_dt = py_dt.replace(tzinfo=UTC)
            return py_dt
        except Exception:
            return None

    def _refresh(self) -> None:
        """Reload audit records into the table and refresh statistics."""
        if self.audit_logger is None:
            self.stats_label.setText("Audit logger not configured.")
            self.table.setRowCount(0)
            return

        params = self._build_query_params()
        self._load_records(params)
        self._load_statistics(params)

    def _load_records(self, params: dict[str, Any]) -> None:
        """Query records and populate the table."""
        try:
            records = self.audit_logger.query(
                since=params["start_time"],
                event_type=params["event_type"],
                limit=500,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Query Error", f"Failed to query audit log: {exc}")
            self.table.setRowCount(0)
            return

        # Apply client-side filters (end_time, severity, keyword) that the
        # backend query() does not support directly.
        filtered: list[dict[str, Any]] = []
        for record in records:
            if not self._record_matches_filters(record, params):
                continue
            filtered.append(record)

        self.table.setRowCount(len(filtered))
        for row, record in enumerate(filtered):
            ts = str(record.get("timestamp", ""))
            et = str(record.get("event_type", ""))
            severity = self._severity_for_event(et)
            details = json.dumps(record.get("details", {}), ensure_ascii=False)
            self.table.setItem(row, 0, QTableWidgetItem(ts))
            self.table.setItem(row, 1, QTableWidgetItem(et))
            self.table.setItem(row, 2, QTableWidgetItem(severity))
            self.table.setItem(row, 3, QTableWidgetItem(details))

    def _record_matches_filters(self, record: dict[str, Any], params: dict[str, Any]) -> bool:
        """Return True when *record* passes the end_time/severity/keyword filters."""
        end_time = params["end_time"]
        if end_time is not None:
            ts = record.get("timestamp")
            if ts:
                try:
                    record_ts = datetime.fromisoformat(str(ts))
                    if record_ts.tzinfo is None:
                        record_ts = record_ts.replace(tzinfo=UTC)
                    if record_ts >= end_time:
                        return False
                except ValueError:
                    pass

        severity = params["severity"]
        if severity is not None:
            et = record.get("event_type", "")
            if self._severity_for_event(str(et)) != severity:
                return False

        keyword = params["keyword"]
        if keyword:
            details_str = json.dumps(record.get("details", {}), ensure_ascii=False).lower()
            if keyword.lower() not in details_str:
                return False

        return True

    def _severity_for_event(self, event_type: str) -> str:
        """Map an event type to its severity label (or 'INFO')."""
        mapping = {
            "decrypted": "CRITICAL",
            "master_key_changed": "CRITICAL",
            "sandbox_escape_attempt": "CRITICAL",
            "cloud_fallback_used": "HIGH",
            "policy_violation": "HIGH",
            "offline_policy_violation": "HIGH",
            "resource_backpressure": "HIGH",
            "disk_watermark_exceeded": "HIGH",
            "plugin_load_failed": "HIGH",
            "sandbox_run_failed": "MEDIUM",
            "password_store_operation": "MEDIUM",
            "connection_tested": "MEDIUM",
            "plugin_loaded": "MEDIUM",
            "webhook_dispatched": "MEDIUM",
            "webhook_failed": "MEDIUM",
            "storage_backend_operation": "MEDIUM",
        }
        return mapping.get(event_type, "INFO")

    def _load_statistics(self, params: dict[str, Any]) -> None:
        """Query statistics and render them into the stats label."""
        try:
            stats = self.audit_logger.statistics(
                start_time=params["start_time"],
                end_time=params["end_time"],
            )
        except Exception as exc:
            self.stats_label.setText(f"Failed to load statistics: {exc}")
            return

        total = stats.get("total_events", 0)
        by_type = stats.get("by_event_type", {})
        by_severity = stats.get("by_severity", {})
        critical = by_severity.get("CRITICAL", 0)
        high = by_severity.get("HIGH", 0)
        medium = by_severity.get("MEDIUM", 0)

        type_lines = " · ".join(f"{k}: {v}" for k, v in sorted(by_type.items())) or "—"
        text = (
            f"<b>Total events:</b> {total}<br/>"
            f"<b>By severity:</b> CRITICAL {critical} · HIGH {high} · MEDIUM {medium}<br/>"
            f"<b>By event type:</b> {type_lines}"
        )
        self.stats_label.setText(text)

    def _export_logs(self) -> None:
        """Export the filtered audit log to a file chosen by the user."""
        if self.audit_logger is None:
            QMessageBox.warning(self, "Export", "Audit logger not configured.")
            return

        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Export Audit Log",
            "audit_export.ndjson",
            "NDJSON (*.ndjson);;CSV (*.csv)",
        )
        if not dest:
            return

        params = self._build_query_params()
        fmt = "csv" if dest.lower().endswith(".csv") else "ndjson"
        try:
            self.audit_logger.export_logs(
                Path(dest),
                start_time=params["start_time"],
                end_time=params["end_time"],
                format=fmt,
            )
            QMessageBox.information(self, "Export Complete", f"Audit log exported to:\n{dest}")
        except Exception as exc:
            QMessageBox.warning(self, "Export Failed", f"Failed to export audit log:\n{exc}")

    def _verify_integrity(self) -> None:
        """Run HMAC integrity verification on the audit log."""
        if self.audit_logger is None:
            QMessageBox.warning(self, "Verify", "Audit logger not configured.")
            return
        try:
            ok, invalid_lines = self.audit_logger.verify()
        except Exception as exc:
            QMessageBox.warning(self, "Verify Error", f"Integrity check raised:\n{exc}")
            return
        if ok:
            QMessageBox.information(
                self, "Integrity OK", "All audit log records passed HMAC verification."
            )
        else:
            QMessageBox.warning(
                self,
                "Integrity Failure",
                f"Tampered records detected on lines: {invalid_lines}",
            )

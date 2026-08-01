"""Backup management dialog (PyQt6).

Manages remote and local encrypted vault backups. The dialog delegates the
actual backup/restore work to :func:`doctoragent.integrations.storage.backup_vault_to_backend`
and the :class:`StorageBackend` implementations, keeping the UI layer thin
and testable.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
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

# Friendly labels for the supported storage backends.
_BACKEND_LABELS: dict[str, str] = {
    "local": "Local Directory",
    "s3": "S3 / MinIO",
    "webdav": "WebDAV",
}


class BackupDialog(QDialog):
    """Dialog to manage encrypted vault backups."""

    def __init__(
        self,
        config: Any,
        connection_manager: Any = None,
        audit_logger: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.connection_manager = connection_manager
        self.audit_logger = audit_logger
        self.setWindowTitle("Backup Manager")
        self.setMinimumSize(800, 500)

        # In-memory backup history for the current session. Each entry is a
        # dict with keys: timestamp, backend, action, files, status, error.
        self._history: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_backend_group())
        layout.addWidget(self._build_status_group())
        layout.addWidget(self._build_history_table(), stretch=1)
        layout.addLayout(self._build_button_row())

        self._refresh_status()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_backend_group(self) -> QGroupBox:
        """Build the backend selection group."""
        group = QGroupBox("Backup Backend")
        form = QFormLayout(group)

        self.backend_combo = QComboBox()
        configured = self._configured_backend()
        for key, label in _BACKEND_LABELS.items():
            self.backend_combo.addItem(f"{label} ({key})", userData=key)
        # Select the configured backend if present.
        idx = self.backend_combo.findData(configured)
        if idx >= 0:
            self.backend_combo.setCurrentIndex(idx)
        form.addRow("Backend:", self.backend_combo)

        self.local_dir_input = QLineEdit()
        self.local_dir_input.setPlaceholderText("Local backup directory")
        self.local_dir_input.setText(self._default_local_backup_dir())
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_local_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.local_dir_input, stretch=1)
        dir_row.addWidget(browse_button)
        dir_widget = QWidget()
        dir_widget.setLayout(dir_row)
        form.addRow("Local Path:", dir_widget)

        return group

    def _build_status_group(self) -> QGroupBox:
        """Build the status summary group."""
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)
        self.status_label = QLabel("Loading status...")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        return group

    def _build_history_table(self) -> QTableWidget:
        """Build the backup history table."""
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels(
            ["Time", "Backend", "Action", "Files", "Status"]
        )
        header = self.history_table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return self.history_table

    def _build_button_row(self) -> QHBoxLayout:
        """Build the action button row."""
        row = QHBoxLayout()

        self.backup_button = QPushButton("📦 Backup Now")
        self.backup_button.clicked.connect(self._backup_now)
        row.addWidget(self.backup_button)

        self.restore_button = QPushButton("♻️ Restore...")
        self.restore_button.clicked.connect(self._restore)
        row.addWidget(self.restore_button)

        self.test_button = QPushButton("🧪 Test Connection")
        self.test_button.clicked.connect(self._test_backend)
        row.addWidget(self.test_button)

        row.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)

        return row

    # ── Helpers ──────────────────────────────────────────────────────────

    def _configured_backend(self) -> str:
        """Return the configured storage backend name, or 'local'."""
        try:
            return self.config.integrations.storage_backup_backend.lower()
        except Exception:
            return "local"

    def _default_local_backup_dir(self) -> str:
        """Return a sensible default local backup directory."""
        try:
            vault = self.config.paths.vault
            return str(vault.parent / "Backup")
        except Exception:
            return str(Path.home() / "DoctorAgent" / "Backup")

    def _selected_backend_key(self) -> str:
        """Return the backend key selected in the combo box."""
        data = self.backend_combo.currentData()
        if isinstance(data, str) and data:
            return data
        text = self.backend_combo.currentText()
        for key, label in _BACKEND_LABELS.items():
            if key in text or label in text:
                return key
        return "local"

    def _vault_path(self) -> Path:
        """Return the configured vault directory."""
        try:
            return self.config.paths.vault
        except Exception:
            return Path.home() / "DoctorAgent" / "Vault"

    def _create_backend(self) -> Any:
        """Instantiate the selected storage backend.

        For the local backend we use the directory from the input field; for
        remote backends we rely on the configured IntegrationsConfig.
        """
        from doctoragent.integrations.storage import (
            LocalBackend,
            create_storage_backend,
        )

        key = self._selected_backend_key()
        if key == "local":
            local_root = Path(
                self.local_dir_input.text().strip() or self._default_local_backup_dir()
            )
            return LocalBackend(local_root)
        return create_storage_backend(self.config.integrations)

    def _refresh_status(self) -> None:
        """Update the status label with last backup info and connection state."""
        parts: list[str] = []

        # Last backup time from history.
        if self._history:
            last = self._history[-1]
            ts = last.get("timestamp", "—")
            status = last.get("status", "—")
            files = last.get("files", 0)
            backend = last.get("backend", "—")
            parts.append(f"Last backup: {ts} via {backend} — {files} files ({status})")
        else:
            parts.append("No backups performed in this session.")

        # Connection count.
        if self.connection_manager is not None:
            try:
                conns = self.connection_manager.list_enabled()
                parts.append(f"Enabled connections: {len(conns)}")
            except Exception:
                parts.append("Connections: unavailable")
        else:
            parts.append("Connection manager: not configured")

        # Vault path.
        parts.append(f"Vault: {self._vault_path()}")

        self.status_label.setText("<br/>".join(parts))
        self._refresh_history_table()

    def _refresh_history_table(self) -> None:
        """Reload backup history into the table."""
        self.history_table.setRowCount(len(self._history))
        for row, entry in enumerate(self._history):
            self.history_table.setItem(row, 0, QTableWidgetItem(str(entry.get("timestamp", ""))))
            self.history_table.setItem(row, 1, QTableWidgetItem(str(entry.get("backend", ""))))
            self.history_table.setItem(row, 2, QTableWidgetItem(str(entry.get("action", ""))))
            self.history_table.setItem(row, 3, QTableWidgetItem(str(entry.get("files", 0))))
            self.history_table.setItem(row, 4, QTableWidgetItem(str(entry.get("status", ""))))

    def _record_history(self, entry: dict[str, Any]) -> None:
        """Append a history entry and refresh the table."""
        self._history.append(entry)
        self._refresh_status()

    # ── Actions ──────────────────────────────────────────────────────────

    def _browse_local_dir(self) -> None:
        """Open a directory chooser for the local backup path."""
        current = self.local_dir_input.text().strip()
        start = current if current else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select backup directory", start)
        if chosen:
            self.local_dir_input.setText(chosen)

    def _backup_now(self) -> None:
        """Trigger an incremental backup to the selected backend."""
        vault_path = self._vault_path()
        if not vault_path.exists():
            QMessageBox.warning(self, "Backup", f"Vault directory does not exist:\n{vault_path}")
            return

        try:
            backend = self._create_backend()
        except Exception as exc:
            QMessageBox.warning(self, "Backup", f"Failed to create backend:\n{exc}")
            return

        self.backup_button.setEnabled(False)
        try:
            from doctoragent.integrations.storage import backup_vault_to_backend

            result = backup_vault_to_backend(
                vault_path,
                backend,
                audit_logger=self.audit_logger,
            )
        except Exception as exc:
            self._record_history(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "backend": self._selected_backend_key(),
                    "action": "backup",
                    "files": 0,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            QMessageBox.warning(self, "Backup Failed", f"Backup failed:\n{exc}")
            return
        finally:
            self.backup_button.setEnabled(True)

        files = len(result.uploaded) + len(result.skipped)
        status = "ok" if result.ok else f"error: {result.error}"
        self._record_history(
            {
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "backend": self._selected_backend_key(),
                "action": "backup",
                "files": files,
                "status": status,
                "uploaded": len(result.uploaded),
                "skipped": len(result.skipped),
                "removed": len(result.removed),
            }
        )
        QMessageBox.information(
            self,
            "Backup Complete",
            f"Backup complete.\nUploaded: {len(result.uploaded)}\n"
            f"Skipped: {len(result.skipped)}\nRemoved: {len(result.removed)}",
        )

    def _restore(self) -> None:
        """Restore vault files from the selected backend.

        Downloads all objects from the backend into a user-chosen directory
        so the original vault is never overwritten destructively.
        """
        dest_dir = QFileDialog.getExistingDirectory(
            self, "Select restore destination directory", str(Path.home())
        )
        if not dest_dir:
            return

        try:
            backend = self._create_backend()
        except Exception as exc:
            QMessageBox.warning(self, "Restore", f"Failed to create backend:\n{exc}")
            return

        reply = QMessageBox.question(
            self,
            "Confirm Restore",
            f"Download all backup objects to:\n{dest_dir}?\n"
            "Existing files with the same name will be overwritten.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.restore_button.setEnabled(False)
        try:
            objects = backend.list()
            restored = 0
            errors: list[str] = []
            for obj in objects:
                key = obj.key if hasattr(obj, "key") else str(obj)
                if key.endswith(".doctoragent-backup-manifest.json"):
                    continue
                local_path = Path(dest_dir) / key
                try:
                    backend.download(key, local_path)
                    restored += 1
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
        except Exception as exc:
            self._record_history(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "backend": self._selected_backend_key(),
                    "action": "restore",
                    "files": 0,
                    "status": f"failed: {exc}",
                }
            )
            QMessageBox.warning(self, "Restore Failed", f"Restore failed:\n{exc}")
            return
        finally:
            self.restore_button.setEnabled(True)

        status = "ok" if not errors else f"partial ({len(errors)} errors)"
        self._record_history(
            {
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "backend": self._selected_backend_key(),
                "action": "restore",
                "files": restored,
                "status": status,
            }
        )
        if errors:
            QMessageBox.warning(
                self,
                "Restore Partial",
                f"Restored {restored} files with {len(errors)} errors:\n" + "\n".join(errors[:10]),
            )
        else:
            QMessageBox.information(
                self, "Restore Complete", f"Restored {restored} files to:\n{dest_dir}"
            )

    def _test_backend(self) -> None:
        """Test connectivity to the selected backend."""
        try:
            backend = self._create_backend()
        except Exception as exc:
            QMessageBox.warning(self, "Test", f"Failed to create backend:\n{exc}")
            return

        self.test_button.setEnabled(False)
        try:
            ok = backend.test_connection()
        except Exception as exc:
            QMessageBox.warning(self, "Test Failed", f"Backend test raised:\n{exc}")
            return
        finally:
            self.test_button.setEnabled(True)

        if ok:
            QMessageBox.information(self, "Test", "Backend connection successful.")
        else:
            QMessageBox.warning(self, "Test", "Backend connection failed.")

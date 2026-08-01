"""Master key management dialog (PyQt6).

Provides a safe UI for rotating the master key and performing emergency
rotations. All cryptographic operations are delegated to
:mod:`doctoragent.security.master_key`; the dialog never displays the key
material itself — only metadata (provider type, creation/rotation timestamps).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
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

# The confirmation text the user must type to authorise an emergency rotation.
_EMERGENCY_CONFIRM_TEXT = "EMERGENCY ROTATE"


class KeyManagementDialog(QDialog):
    """Dialog to manage the master encryption key."""

    def __init__(
        self,
        config: Any,
        vault_key: bytes | None = None,
        master_key_provider: Any = None,
        audit_logger: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.vault_key = vault_key
        self.master_key_provider = master_key_provider
        self.audit_logger = audit_logger
        self.setWindowTitle("Key Management")
        self.setMinimumSize(700, 500)

        # In-memory rotation history for the current session.
        self._rotation_history: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_info_group())
        layout.addWidget(self._build_warning_group())
        layout.addWidget(self._build_history_table(), stretch=1)
        layout.addLayout(self._build_button_row())

        self._refresh_info()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_info_group(self) -> QGroupBox:
        """Build the current key information group."""
        group = QGroupBox("Current Master Key")
        form = QFormLayout(group)

        self.provider_label = QLabel("—")
        form.addRow("Provider:", self.provider_label)

        self.storage_path_label = QLabel("—")
        form.addRow("Storage Path:", self.storage_path_label)

        self.created_label = QLabel("—")
        form.addRow("Created / Last Rotated:", self.created_label)

        self.vault_key_label = QLabel("—")
        form.addRow("Vault Key Status:", self.vault_key_label)

        return group

    def _build_warning_group(self) -> QGroupBox:
        """Build the security warning group."""
        group = QGroupBox("⚠️ Security Notice")
        layout = QVBoxLayout(group)
        warning = QLabel(
            "<b>Before rotating the master key:</b><br/>"
            "1. Create a full backup of your Vault.<br/>"
            "2. Ensure you have access to the new key material.<br/>"
            "3. Rotation will re-encrypt all vault files — this may take time.<br/>"
            "4. Emergency rotation does NOT re-encrypt files; it wraps the vault key."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        return group

    def _build_history_table(self) -> QTableWidget:
        """Build the rotation history table."""
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(4)
        self.history_table.setHorizontalHeaderLabels(["Time", "Action", "Provider", "Status"])
        header = self.history_table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return self.history_table

    def _build_button_row(self) -> QHBoxLayout:
        """Build the action button row."""
        row = QHBoxLayout()

        self.rotate_button = QPushButton("🔄 Rotate Master Key")
        self.rotate_button.clicked.connect(self._rotate_key)
        row.addWidget(self.rotate_button)

        self.emergency_button = QPushButton("🚨 Emergency Rotate")
        self.emergency_button.clicked.connect(self._emergency_rotate)
        row.addWidget(self.emergency_button)

        row.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)

        return row

    # ── Helpers ──────────────────────────────────────────────────────────

    def _master_key_storage_path(self) -> Path:
        """Return the path to the master key storage file."""
        try:
            return self.config.paths.connections.parent / "master_key.bin"
        except Exception:
            return Path.home() / "DoctorAgent" / "Config" / "master_key.bin"

    def _rotation_marker_path(self) -> Path:
        """Return the path to the rotation marker file."""
        return self._master_key_storage_path().with_name(".rotation_marker")

    def _provider_type_name(self) -> str:
        """Return a human-readable provider type name."""
        if self.master_key_provider is not None:
            return type(self.master_key_provider).__name__
        try:
            return self.config.security.master_key_provider
        except Exception:
            return "unknown"

    def _last_rotation_time(self) -> str:
        """Read the rotation marker file and return its timestamp, or '—'."""
        marker = self._rotation_marker_path()
        if not marker.exists():
            # Fall back to the master key file mtime as an approximation.
            mk = self._master_key_storage_path()
            if mk.exists():
                try:
                    ts = datetime.fromtimestamp(mk.stat().st_mtime, tz=UTC)
                    return ts.isoformat(timespec="seconds")
                except OSError:
                    pass
            return "— (never rotated)"
        try:
            raw = marker.read_text(encoding="utf-8").strip()
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts.isoformat(timespec="seconds")
        except (OSError, ValueError):
            return "— (corrupt marker)"

    def _refresh_info(self) -> None:
        """Populate the key information labels."""
        self.provider_label.setText(self._provider_type_name())
        self.storage_path_label.setText(str(self._master_key_storage_path()))
        self.created_label.setText(self._last_rotation_time())
        if self.vault_key:
            self.vault_key_label.setText("✅ Vault key available in memory")
        else:
            self.vault_key_label.setText("⚠️ Vault key not loaded (unlock required)")

        has_key = self.vault_key is not None and self.master_key_provider is not None
        self.rotate_button.setEnabled(has_key)
        self.emergency_button.setEnabled(has_key)

    def _refresh_history_table(self) -> None:
        """Reload rotation history into the table."""
        self.history_table.setRowCount(len(self._rotation_history))
        for row, entry in enumerate(self._rotation_history):
            self.history_table.setItem(row, 0, QTableWidgetItem(str(entry.get("timestamp", ""))))
            self.history_table.setItem(row, 1, QTableWidgetItem(str(entry.get("action", ""))))
            self.history_table.setItem(row, 2, QTableWidgetItem(str(entry.get("provider", ""))))
            self.history_table.setItem(row, 3, QTableWidgetItem(str(entry.get("status", ""))))

    def _record_history(self, entry: dict[str, Any]) -> None:
        """Append a history entry and refresh the table."""
        self._rotation_history.append(entry)
        self._refresh_history_table()

    def _create_new_provider(self) -> Any:
        """Create a fresh master key provider of the same type as the current one."""
        from doctoragent.security.master_key import create_master_key_provider

        provider_name = self._provider_type_name()
        # Map class names back to config names for the factory.
        name_map = {
            "FilePasswordProvider": "FilePassword",
            "DpapiMasterKeyProvider": "DPAPI",
            "TpmMasterKeyProvider": "TPM",
            "KeychainMasterKeyProvider": "mac-keychain",
        }
        config_name = name_map.get(provider_name, provider_name)
        password = None
        if config_name.lower() == "filepassword":
            try:
                password = self.config.security.master_key_password
            except Exception:
                password = None
        return create_master_key_provider(
            config_name,
            self._master_key_storage_path(),
            password=password,
        )

    # ── Actions ──────────────────────────────────────────────────────────

    def _rotate_key(self) -> None:
        """Perform a standard master key rotation with confirmation."""
        if self.vault_key is None or self.master_key_provider is None:
            QMessageBox.warning(self, "Rotate", "Vault key or provider not available.")
            return

        reply = QMessageBox.question(
            self,
            "Confirm Key Rotation",
            "This will re-encrypt ALL vault files with a new master key.\n\n"
            "Have you created a backup? This operation cannot be undone.\n\n"
            "Proceed with key rotation?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.rotate_button.setEnabled(False)
        try:
            from doctoragent.security.master_key import rotate_master_key

            new_provider = self._create_new_provider()
            rotate_master_key(
                current_provider=self.master_key_provider,
                new_provider=new_provider,
                vault_key=self.vault_key,
                storage_path=self._master_key_storage_path(),
                audit_logger=self.audit_logger,
                vault_dir=self.config.paths.vault if self.config else None,
            )
            self.master_key_provider = new_provider
            self._record_history(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "rotation",
                    "provider": type(new_provider).__name__,
                    "status": "ok",
                }
            )
            self._refresh_info()
            QMessageBox.information(self, "Rotation Complete", "Master key rotated successfully.")
        except Exception as exc:
            self._record_history(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "rotation",
                    "provider": self._provider_type_name(),
                    "status": f"failed: {exc}",
                }
            )
            QMessageBox.warning(self, "Rotation Failed", f"Key rotation failed:\n{exc}")
        finally:
            self.rotate_button.setEnabled(True)

    def _emergency_rotate(self) -> None:
        """Perform an emergency rotation requiring typed confirmation."""
        if self.vault_key is None or self.master_key_provider is None:
            QMessageBox.warning(self, "Emergency", "Vault key or provider not available.")
            return

        text, ok = QInputDialog.getText(
            self,
            "🚨 Emergency Rotation",
            f"This is a SECURITY-CRITICAL operation.\n\n"
            f"It will NOT re-encrypt vault files. Instead it wraps the\n"
            f"existing vault key with a new master key.\n\n"
            f"Type {_EMERGENCY_CONFIRM_TEXT!r} to confirm:",
            QLineEdit.EchoMode.Normal,
        )
        if not ok:
            return
        if text.strip() != _EMERGENCY_CONFIRM_TEXT:
            QMessageBox.warning(
                self,
                "Emergency",
                "Confirmation text does not match. Emergency rotation cancelled.",
            )
            return

        # Choose where to store the wrapped vault key.
        default_path = str(self._master_key_storage_path().parent / "vault_key.wrapped")
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Save Wrapped Vault Key",
            default_path,
            "Wrapped Key (*.wrapped);;All Files (*)",
        )
        if not dest:
            return

        self.emergency_button.setEnabled(False)
        try:
            from doctoragent.security.master_key import emergency_rotate

            new_provider = self._create_new_provider()
            emergency_rotate(
                current_provider=self.master_key_provider,
                new_provider=new_provider,
                vault_key=self.vault_key,
                vault_key_backup_path=Path(dest),
                audit_logger=self.audit_logger,
            )
            self.master_key_provider = new_provider
            self._record_history(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "emergency_rotation",
                    "provider": type(new_provider).__name__,
                    "status": "ok",
                }
            )
            self._refresh_info()
            QMessageBox.information(
                self,
                "Emergency Rotation Complete",
                f"Emergency rotation complete.\n"
                f"Wrapped vault key saved to:\n{dest}\n\n"
                f"Keep this file safe — it is needed to decrypt existing vault files.",
            )
        except Exception as exc:
            self._record_history(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "emergency_rotation",
                    "provider": self._provider_type_name(),
                    "status": f"failed: {exc}",
                }
            )
            QMessageBox.warning(self, "Emergency Failed", f"Emergency rotation failed:\n{exc}")
        finally:
            self.emergency_button.setEnabled(True)

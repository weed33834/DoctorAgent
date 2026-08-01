"""Application settings UI for DoctorAgent."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

try:
    from PyQt6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent.config import AegisConfig, ModelConfig, PathConfig, SecurityConfig

_MASTER_KEY_PROVIDERS = ["FilePassword", "DPAPI", "TPM"]


class SettingsDialog(QDialog):
    """Edit DoctorAgent configuration and persist it to disk."""

    def __init__(self, config: AegisConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._original = config.model_dump(mode="json")

        self.setWindowTitle("DoctorAgent Settings")
        self.setMinimumSize(600, 500)

        layout = QVBoxLayout(self)

        layout.addWidget(self._build_security_group())
        layout.addWidget(self._build_paths_group())
        layout.addWidget(self._build_model_group())

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_security_group(self) -> QGroupBox:
        """Build the security settings form."""
        group = QGroupBox("Security")
        form = QFormLayout(group)

        self.master_key_combo = QComboBox()
        self.master_key_combo.addItems(_MASTER_KEY_PROVIDERS)
        self.master_key_combo.setCurrentText(self.config.security.master_key_provider)
        form.addRow("Master Key Provider:", self.master_key_combo)

        self.semantic_search_check = QCheckBox("Enable semantic search")
        self.semantic_search_check.setChecked(self.config.security.enable_semantic_search)
        form.addRow(self.semantic_search_check)

        self.windows_hello_check = QCheckBox("Enable Windows Hello")
        self.windows_hello_check.setChecked(self.config.security.windows_hello_enabled)
        self.windows_hello_check.setVisible(sys.platform == "win32")
        form.addRow(self.windows_hello_check)

        return group

    def _build_paths_group(self) -> QGroupBox:
        """Build the path settings form."""
        group = QGroupBox("Paths")
        form = QFormLayout(group)

        self.inbox_label = QLabel(str(self.config.paths.inbox))
        self.vault_label = QLabel(str(self.config.paths.vault))
        self.index_label = QLabel(str(self.config.paths.index))

        form.addRow("Inbox:", self._path_row(self.inbox_label, self._browse_inbox))
        form.addRow("Vault:", self._path_row(self.vault_label, self._browse_vault))
        form.addRow("Index:", self._path_row(self.index_label, self._browse_index))

        return group

    def _path_row(self, label: QLabel, callback: Callable[[], None]) -> QWidget:
        """Return a widget pairing a path label with a browse button."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.addWidget(label, stretch=1)
        button = QPushButton("Browse...")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def _browse_inbox(self) -> None:
        self._browse_path(self.inbox_label)

    def _browse_vault(self) -> None:
        self._browse_path(self.vault_label)

    def _browse_index(self) -> None:
        self._browse_path(self.index_label)

    def _browse_path(self, label: QLabel) -> None:
        """Open a directory chooser and update *label*."""
        current = Path(label.text())
        if not current.exists():
            current = Path.home()
        chosen = QFileDialog.getExistingDirectory(self, "Select directory", str(current))
        if chosen:
            label.setText(chosen)

    def _build_model_group(self) -> QGroupBox:
        """Build the default local model settings form."""
        group = QGroupBox("Default Local Model")
        form = QFormLayout(group)

        self.model_url = QLineEdit(self.config.model.base_url)
        self.model_name = QLineEdit(self.config.model.model_name)
        self.model_ctx = QLineEdit(str(self.config.model.ctx_size))
        self.model_temp = QLineEdit(str(self.config.model.temperature))
        self.model_timeout = QLineEdit(str(self.config.model.timeout))
        self.model_fallback = QLineEdit(self.config.model.fallback_model_name or "")

        form.addRow("Base URL:", self.model_url)
        form.addRow("Model Name:", self.model_name)
        form.addRow("Context Size:", self.model_ctx)
        form.addRow("Temperature:", self.model_temp)
        form.addRow("Timeout:", self.model_timeout)
        form.addRow("Fallback Model:", self.model_fallback)

        return group

    @staticmethod
    def _parse_int(value: str) -> int | None:
        try:
            return int(value.strip())
        except ValueError:
            return None

    @staticmethod
    def _parse_float(value: str) -> float | None:
        try:
            return float(value.strip())
        except ValueError:
            return None

    def _build_config(self, ctx_size: int, temperature: float, timeout: float) -> AegisConfig:
        """Return a new AegisConfig reflecting the current form values."""
        # On non-Windows platforms the Windows Hello checkbox is hidden and
        # not editable; preserve the original value instead of letting the
        # unchecked (and invisible) checkbox clobber the configured state.
        windows_hello_enabled = (
            self.windows_hello_check.isChecked()
            if sys.platform == "win32"
            else self.config.security.windows_hello_enabled
        )
        security = SecurityConfig(
            kdf=self.config.security.kdf,
            encryption=self.config.security.encryption,
            master_key_provider=self.master_key_combo.currentText(),
            master_key_password=self.config.security.master_key_password,
            enable_semantic_search=self.semantic_search_check.isChecked(),
            windows_hello_enabled=windows_hello_enabled,
        )
        paths = PathConfig(
            inbox=Path(self.inbox_label.text()),
            vault=Path(self.vault_label.text()),
            index=Path(self.index_label.text()),
            logs=self.config.paths.logs,
            connections=self.config.paths.connections,
            settings=self.config.paths.settings,
        )
        model = ModelConfig(
            base_url=self.model_url.text().strip(),
            model_name=self.model_name.text().strip(),
            ctx_size=ctx_size,
            temperature=temperature,
            timeout=timeout,
            fallback_model_name=self.model_fallback.text().strip() or None,
        )
        return AegisConfig(
            app_name=self.config.app_name,
            debug=self.config.debug,
            model=model,
            security=security,
            paths=paths,
        )

    def _has_changes(self, new_config: AegisConfig) -> bool:
        """Return True when *new_config* differs from the original config."""
        return new_config.model_dump(mode="json") != self._original

    def accept(self) -> None:
        """Validate, persist, and notify the user when settings changed."""
        ctx_size = self._parse_int(self.model_ctx.text())
        temperature = self._parse_float(self.model_temp.text())
        timeout = self._parse_float(self.model_timeout.text())

        if ctx_size is None or temperature is None or timeout is None:
            QMessageBox.warning(
                self, "Validation", "Context size, temperature and timeout must be numeric."
            )
            return

        # Validate that the configured inbox / vault / index paths can be
        # created (or already exist and are writable). Failing here prevents
        # the user from saving a configuration that DoctorAgent cannot use.
        for label, name in (
            (self.inbox_label, "Inbox"),
            (self.vault_label, "Vault"),
            (self.index_label, "Index"),
        ):
            path_text = label.text().strip()
            if not path_text:
                QMessageBox.warning(self, "Validation", f"{name} path must not be empty.")
                return
            try:
                Path(path_text).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "Validation",
                    f"{name} path '{path_text}' is not writable: {exc}",
                )
                return

        new_config = self._build_config(ctx_size, temperature, timeout)
        if self._has_changes(new_config):
            self.config.security = new_config.security
            self.config.paths = new_config.paths
            self.config.model = new_config.model
            self.config.save_to_file()
            QMessageBox.information(
                self,
                "Restart Required",
                "Settings saved. Please restart DoctorAgent for changes to take effect.",
            )
        super().accept()

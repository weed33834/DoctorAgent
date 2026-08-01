"""Platform connection management UI (PyQt6)."""

from typing import Any

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
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
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from pydantic import SecretStr

from doctoragent.connections.manager import ConnectionManager
from doctoragent.connections.models import AuthMethod, Connection, PlatformType
from doctoragent.presentation.utils import validate_http_url


class ConnectionEditDialog(QDialog):
    """Dialog to add or edit a connection."""

    def __init__(self, manager: ConnectionManager, connection: Connection | None = None) -> None:
        super().__init__()
        self.manager = manager
        self.connection = connection
        self.setWindowTitle("Edit Connection" if connection else "Add Connection")
        self.setMinimumWidth(450)

        layout = QFormLayout(self)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Local Ollama")
        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("http://127.0.0.1:11434/v1")
        self.model_name_input = QLineEdit()
        self.model_name_input.setPlaceholderText("e.g. qwen2.5:7b")
        self.platform_combo = QComboBox()
        self.platform_combo.addItems([pt.value for pt in PlatformType])
        self.auth_combo = QComboBox()
        self.auth_combo.addItems([am.value for am in AuthMethod])
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Only for bearer / API key auth")
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Only for basic auth")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Only for basic auth")
        self.local_check = QCheckBox("Local connection (127.0.0.1 / localhost)")
        self.local_check.setToolTip("Sensitive tasks require a trusted local connection.")
        self.cloud_auth_check = QCheckBox("Authorize for cloud fallback")
        self.cloud_auth_check.setToolTip(
            "Allow this connection for non-sensitive tasks when no local model is available."
        )

        layout.addRow("Name:", self.name_input)
        layout.addRow("Platform Type:", self.platform_combo)
        layout.addRow("Base URL:", self.base_url_input)
        layout.addRow("Model Name:", self.model_name_input)
        layout.addRow("Auth Method:", self.auth_combo)
        layout.addRow("API Key:", self.api_key_input)
        layout.addRow("Username:", self.username_input)
        layout.addRow("Password:", self.password_input)
        layout.addRow(self.local_check)
        layout.addRow(self.cloud_auth_check)

        help_label = QLabel(
            "<small>Tip: For sensitive classification and encryption, use a local model "
            "running on 127.0.0.1 or localhost.</small>"
        )
        help_label.setWordWrap(True)
        layout.addRow(help_label)

        if connection:
            self.name_input.setText(connection.name)
            self.base_url_input.setText(connection.base_url)
            self.model_name_input.setText(connection.model_name)
            self.platform_combo.setCurrentText(connection.platform_type.value)
            self.auth_combo.setCurrentText(connection.auth_method.value)
            self.api_key_input.setText(connection.api_key.get_secret_value())
            self.username_input.setText(connection.username)
            self.password_input.setText(connection.password.get_secret_value())
            self.local_check.setChecked(connection.is_local)
            self.cloud_auth_check.setChecked(connection.is_cloud_authorized)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _set_error_style(self, widget: QWidget, error: bool) -> None:
        """Highlight or clear a widget's error state."""
        if error:
            widget.setStyleSheet("border: 1px solid red;")
        else:
            widget.setStyleSheet("")

    def _validate_required(self) -> bool:
        """Validate required fields and highlight errors."""
        name = self.name_input.text().strip()
        base_url = self.base_url_input.text().strip()
        self._set_error_style(self.name_input, not name)
        self._set_error_style(self.base_url_input, not base_url)
        return bool(name and base_url)

    def _connection_from_form(self) -> dict[str, Any]:
        """Return a data dictionary representing the current form values."""
        auth_method = AuthMethod(self.auth_combo.currentText())
        data: dict[str, Any] = {
            "name": self.name_input.text().strip(),
            "platform_type": PlatformType(self.platform_combo.currentText()),
            "base_url": self.base_url_input.text().strip(),
            "model_name": self.model_name_input.text().strip(),
            "auth_method": auth_method,
            "is_local": self.local_check.isChecked(),
            "is_cloud_authorized": self.cloud_auth_check.isChecked(),
        }
        # Only collect the credentials that match the chosen auth method so we
        # do not silently persist unrelated secrets from the form.
        if auth_method in (AuthMethod.BEARER, AuthMethod.API_KEY):
            data["api_key"] = SecretStr(self.api_key_input.text())
        if auth_method == AuthMethod.BASIC:
            data["username"] = self.username_input.text()
            data["password"] = SecretStr(self.password_input.text())
        return data

    def accept(self) -> None:
        """Save connection and close."""
        if not self._validate_required():
            QMessageBox.warning(self, "Validation", "Name and Base URL are required.")
            return

        base_url = self.base_url_input.text().strip()
        if not validate_http_url(base_url):
            QMessageBox.warning(
                self,
                "Validation",
                "Base URL must be a valid http:// or https:// URL with a host.",
            )
            self._set_error_style(self.base_url_input, True)
            return

        data = self._connection_from_form()

        if self.connection:
            updated = self.connection.model_copy(update=data)
            self.manager.update(updated)
        else:
            self.manager.add(Connection(**data))
        super().accept()


class ConnectionManagerDialog(QDialog):
    """Main connection management dialog."""

    def __init__(self, manager: ConnectionManager) -> None:
        super().__init__()
        self.manager = manager
        self.setWindowTitle("Platform Connection Manager")
        self.setMinimumSize(800, 450)

        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Platform", "Base URL", "Model", "Local", "Cloud OK"]
        )
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        button_layout = QHBoxLayout()
        self.add_button = QPushButton("➕ Add")
        self.edit_button = QPushButton("✏️ Edit")
        self.delete_button = QPushButton("🗑️ Delete")
        self.test_button = QPushButton("🧪 Test")

        self.add_button.clicked.connect(self._add_connection)
        self.edit_button.clicked.connect(self._edit_connection)
        self.delete_button.clicked.connect(self._delete_connection)
        self.test_button.clicked.connect(self._test_connection)

        button_layout.addWidget(self.add_button)
        button_layout.addWidget(self.edit_button)
        button_layout.addWidget(self.delete_button)
        button_layout.addWidget(self.test_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.status_label)

        self.refresh_table()

    def refresh_table(self) -> None:
        """Reload connection list into table."""
        connections = self.manager.list_all()
        self.table.setRowCount(len(connections))
        for row, conn in enumerate(connections):
            name_item = QTableWidgetItem(conn.name)
            # Store the connection id in the UserRole so selection lookup is
            # unambiguous even when several connections share a name.
            name_item.setData(Qt.ItemDataRole.UserRole, str(conn.id))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(conn.platform_type.value))
            self.table.setItem(row, 2, QTableWidgetItem(conn.base_url))
            self.table.setItem(row, 3, QTableWidgetItem(conn.model_name))
            self.table.setItem(row, 4, QTableWidgetItem("Yes" if conn.is_local else "No"))
            self.table.setItem(
                row, 5, QTableWidgetItem("Yes" if conn.is_cloud_authorized else "No")
            )

    def _selected_connection(self) -> Connection | None:
        """Return selected connection or None."""
        selected = self.table.selectedItems()
        if not selected:
            return None
        row = selected[0].row()
        item = self.table.item(row, 0)
        if item is None:
            return None
        conn_id = item.data(Qt.ItemDataRole.UserRole)
        if conn_id is None:
            # Backward-compat fallback for rows populated without an id (e.g.
            # by tests installing a bare QTableWidgetItem): look up by name.
            name = item.text()
            for conn in self.manager.list_all():
                if conn.name == name:
                    return conn
            return None
        for conn in self.manager.list_all():
            if str(conn.id) == str(conn_id):
                return conn
        return None

    def _add_connection(self) -> None:
        """Open add dialog."""
        dialog = ConnectionEditDialog(self.manager)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_table()
            self.status_label.setText("Connection added")

    def _edit_connection(self) -> None:
        """Open edit dialog."""
        conn = self._selected_connection()
        if conn is None:
            QMessageBox.information(self, "Select", "Please select a connection.")
            return
        dialog = ConnectionEditDialog(self.manager, conn)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_table()
            self.status_label.setText(f"Updated '{conn.name}'")

    def _delete_connection(self) -> None:
        """Delete selected connection."""
        conn = self._selected_connection()
        if conn is None:
            return
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete connection '{conn.name}'?",
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.manager.delete(conn.id)
            self.refresh_table()
            self.status_label.setText(f"Deleted '{conn.name}'")

    def _test_connection(self) -> None:
        """Test selected connection.

        The network call is synchronous and blocks the UI thread; at minimum
        disable the Test button while the request is in flight to prevent
        repeated clicks, and reflect the in-progress state in the status line.
        """
        conn = self._selected_connection()
        if conn is None:
            return
        self.test_button.setEnabled(False)
        self.status_label.setText(f"Testing '{conn.name}'...")
        try:
            success, message = self.manager.test_connection(conn.id)
        finally:
            self.test_button.setEnabled(True)
        if success:
            self.status_label.setText(f"'{conn.name}' OK: {message}")
            QMessageBox.information(self, "Connection Test", message)
        else:
            self.status_label.setText(f"'{conn.name}' FAILED: {message}")
            QMessageBox.warning(self, "Connection Test", message)

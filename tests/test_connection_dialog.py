"""Tests for the platform connection management dialog."""

# mypy: ignore-errors

# ruff: noqa: N802

from pathlib import Path

import pytest

from doctoragent.connections.manager import ConnectionManager
from doctoragent.connections.models import AuthMethod, Connection, PlatformType

from .presentation_stubs import (
    FakeApplication,
    FakeDialog,
    FakeMessageBox,
    FakeTableWidgetItem,
    install_presentation_stubs,
    restore_modules,
)

pytestmark = pytest.mark.gui


@pytest.fixture
def dialog_qt_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace PyQt6 widgets with stubs for dialog tests."""
    saved = install_presentation_stubs()
    FakeApplication._instance = None
    FakeMessageBox._last_warning = None
    FakeMessageBox._last_information = None
    FakeMessageBox._last_question = None
    yield
    FakeApplication._instance = None
    restore_modules(saved)


@pytest.fixture
def manager(tmp_path: Path) -> ConnectionManager:
    """Connection manager backed by an isolated, initially empty file."""
    path = tmp_path / "connections.json"
    path.write_text('{"version": 1, "connections": []}')
    return ConnectionManager(path)


def test_edit_dialog_loads_connection_values(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """ConnectionEditDialog pre-populates fields from an existing connection."""
    from doctoragent.presentation.connection_dialog import ConnectionEditDialog

    conn = Connection(
        name="Test Local",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        model_name="qwen2.5:7b",
        auth_method=AuthMethod.BEARER,
        api_key="secret",
        username="user",
        password="pass",
        is_local=True,
        is_cloud_authorized=True,
    )
    dialog = ConnectionEditDialog(manager, conn)

    assert dialog.name_input.text() == "Test Local"
    assert dialog.base_url_input.text() == "http://127.0.0.1:11434/v1"
    assert dialog.model_name_input.text() == "qwen2.5:7b"
    assert dialog.platform_combo.currentText() == PlatformType.OLLAMA.value
    assert dialog.auth_combo.currentText() == AuthMethod.BEARER.value
    assert dialog.api_key_input.text() == "secret"
    assert dialog.username_input.text() == "user"
    assert dialog.password_input.text() == "pass"
    assert dialog.local_check.isChecked() is True
    assert dialog.cloud_auth_check.isChecked() is True


def test_edit_dialog_adds_connection(dialog_qt_stubs: None, manager: ConnectionManager) -> None:
    """Accepting the add dialog creates a new connection in the manager."""
    from doctoragent.presentation.connection_dialog import ConnectionEditDialog

    dialog = ConnectionEditDialog(manager)
    dialog.name_input.setText("New Connection")
    dialog.base_url_input.setText("http://localhost:1234/v1")
    dialog.model_name_input.setText("model-x")
    dialog.platform_combo.setCurrentText(PlatformType.LM_STUDIO.value)
    dialog.auth_combo.setCurrentText(AuthMethod.API_KEY.value)
    dialog.api_key_input.setText("key")
    dialog.local_check.setChecked(True)
    dialog.cloud_auth_check.setChecked(False)

    assert len(manager.list_all()) == 0
    dialog.accept()

    connections = manager.list_all()
    assert len(connections) == 1
    assert connections[0].name == "New Connection"
    assert connections[0].platform_type == PlatformType.LM_STUDIO
    assert connections[0].auth_method == AuthMethod.API_KEY
    assert connections[0].api_key.get_secret_value() == "key"
    assert connections[0].is_local is True
    assert connections[0].is_cloud_authorized is False


def test_edit_dialog_updates_connection(dialog_qt_stubs: None, manager: ConnectionManager) -> None:
    """Accepting the edit dialog updates the existing connection."""
    from doctoragent.presentation.connection_dialog import ConnectionEditDialog

    conn = Connection(
        name="Old Name",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
    )
    manager.add(conn)

    dialog = ConnectionEditDialog(manager, conn)
    dialog.name_input.setText("Renamed")
    dialog.accept()

    updated = manager.list_all()[0]
    assert updated.name == "Renamed"
    assert updated.id == conn.id


def test_edit_dialog_validation_warns_on_empty_required(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """Accept with empty required fields shows a warning and does not save."""
    from doctoragent.presentation.connection_dialog import ConnectionEditDialog

    dialog = ConnectionEditDialog(manager)
    dialog.name_input.setText("")
    dialog.base_url_input.setText("")

    dialog.accept()

    assert FakeMessageBox._last_warning is not None
    assert FakeMessageBox._last_warning[1] == "Validation"
    assert len(manager.list_all()) == 0


def test_connection_from_form(dialog_qt_stubs: None, manager: ConnectionManager) -> None:
    """_connection_from_form returns current widget values."""
    from doctoragent.presentation.connection_dialog import ConnectionEditDialog

    dialog = ConnectionEditDialog(manager)
    dialog.name_input.setText("Form")
    dialog.base_url_input.setText("http://127.0.0.1:8080")
    dialog.platform_combo.setCurrentText(PlatformType.ANTHROPIC.value)
    dialog.auth_combo.setCurrentText(AuthMethod.BASIC.value)
    dialog.username_input.setText("u")
    dialog.password_input.setText("p")
    dialog.local_check.setChecked(False)
    dialog.cloud_auth_check.setChecked(True)

    data = dialog._connection_from_form()
    assert data["name"] == "Form"
    assert data["base_url"] == "http://127.0.0.1:8080"
    assert data["platform_type"] == PlatformType.ANTHROPIC
    assert data["auth_method"] == AuthMethod.BASIC
    assert data["username"] == "u"
    assert data["password"].get_secret_value() == "p"
    assert data["is_local"] is False
    assert data["is_cloud_authorized"] is True


def test_manager_dialog_refresh_table(dialog_qt_stubs: None, manager: ConnectionManager) -> None:
    """ConnectionManagerDialog loads connections into the table on init."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    manager.add(
        Connection(
            name="Conn A",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
            is_local=True,
            is_cloud_authorized=False,
        )
    )

    dialog = ConnectionManagerDialog(manager)

    assert dialog.table._row_count == 1
    assert dialog.table._horizontal_labels == [
        "Name",
        "Platform",
        "Base URL",
        "Model",
        "Local",
        "Cloud OK",
    ]
    assert dialog.table.item(0, 0).text() == "Conn A"
    assert dialog.table.item(0, 4).text() == "Yes"


def test_manager_dialog_selected_connection(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """_selected_connection returns the connection matching the selected row."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    conn = Connection(
        name="Selected",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
    )
    manager.add(conn)

    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)

    assert dialog._selected_connection() == conn


def test_manager_dialog_add_connection(
    dialog_qt_stubs: None, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking Add opens the edit dialog and refreshes the table on accept."""
    from doctoragent.presentation import connection_dialog
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    class FakeEditDialog:
        def __init__(self, _manager: ConnectionManager) -> None:
            pass

        def exec(self) -> int:
            manager.add(
                Connection(
                    name="Added",
                    platform_type=PlatformType.OLLAMA,
                    base_url="http://127.0.0.1:11434/v1",
                )
            )
            return FakeDialog.DialogCode.Accepted

    monkeypatch.setattr(connection_dialog, "ConnectionEditDialog", FakeEditDialog)

    dialog = ConnectionManagerDialog(manager)
    assert dialog.table._row_count == 0
    dialog._add_connection()
    assert dialog.table._row_count == 1
    assert "Connection added" in dialog.status_label.text


def test_manager_dialog_edit_without_selection_warns(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """Clicking Edit with no selection shows an information message."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    dialog = ConnectionManagerDialog(manager)
    dialog._edit_connection()

    assert FakeMessageBox._last_information is not None
    assert FakeMessageBox._last_information[1] == "Select"


def test_manager_dialog_edit_connection(dialog_qt_stubs: None, manager: ConnectionManager) -> None:
    """Clicking Edit updates the selected connection."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    _conn = manager.add(
        Connection(
            name="Editable",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)
    dialog._edit_connection()

    assert "Updated 'Editable'" in dialog.status_label.text


def test_manager_dialog_delete_connection(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """Clicking Delete removes the selected connection."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    _conn = manager.add(
        Connection(
            name="Deletable",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)
    dialog._delete_connection()

    assert manager.get(_conn.id) is None
    assert "Deleted 'Deletable'" in dialog.status_label.text


def test_manager_dialog_test_connection_updates_status(
    dialog_qt_stubs: None, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking Test updates the status label and shows a message box."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    _conn = manager.add(
        Connection(
            name="Testable",
            platform_type=PlatformType.CUSTOM,
            base_url="http://127.0.0.1:1/v1",
        )
    )

    def fake_test(_connection_id: object) -> tuple[bool, str]:
        return True, "all good"

    monkeypatch.setattr(manager, "test_connection", fake_test)

    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)
    dialog._test_connection()

    assert "'Testable' OK: all good" in dialog.status_label.text
    assert FakeMessageBox._last_information is not None
    assert FakeMessageBox._last_information[2] == "all good"


def test_manager_dialog_test_connection_failure(
    dialog_qt_stubs: None, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed connection test updates the status and shows a warning."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    manager.add(
        Connection(
            name="Bad",
            platform_type=PlatformType.CUSTOM,
            base_url="http://127.0.0.1:1/v1",
        )
    )

    def fake_test(_connection_id: object) -> tuple[bool, str]:
        return False, "unreachable"

    monkeypatch.setattr(manager, "test_connection", fake_test)

    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)
    dialog._test_connection()

    assert "'Bad' FAILED: unreachable" in dialog.status_label.text
    assert FakeMessageBox._last_warning is not None
    assert FakeMessageBox._last_warning[2] == "unreachable"


def test_manager_dialog_selected_connection_not_found(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """_selected_connection returns None when the row name has no match."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    manager.add(
        Connection(
            name="Real",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )

    dialog = ConnectionManagerDialog(manager)
    dialog.table.setItem(0, 0, FakeTableWidgetItem("Missing"))
    dialog.table.select_row(0)

    assert dialog._selected_connection() is None


def test_manager_dialog_add_connection_rejected(
    dialog_qt_stubs: None, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking Add and cancelling does not change the table or status."""
    from doctoragent.presentation import connection_dialog
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    class FakeEditDialog:
        def __init__(self, _manager: ConnectionManager) -> None:
            pass

        def exec(self) -> int:
            return FakeDialog.DialogCode.Rejected

    monkeypatch.setattr(connection_dialog, "ConnectionEditDialog", FakeEditDialog)

    dialog = ConnectionManagerDialog(manager)
    assert dialog.table._row_count == 0
    dialog._add_connection()
    assert dialog.table._row_count == 0
    assert "Connection added" not in dialog.status_label.text


def test_manager_dialog_edit_connection_rejected(
    dialog_qt_stubs: None, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking Edit and cancelling leaves the connection unchanged."""
    from doctoragent.presentation import connection_dialog
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    manager.add(
        Connection(
            name="Editable",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )

    class FakeEditDialog:
        def __init__(self, _manager: ConnectionManager, _connection: object) -> None:
            pass

        def exec(self) -> int:
            return FakeDialog.DialogCode.Rejected

    monkeypatch.setattr(connection_dialog, "ConnectionEditDialog", FakeEditDialog)

    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)
    dialog._edit_connection()

    assert manager.list_all()[0].name == "Editable"
    assert "Updated" not in dialog.status_label.text


def test_manager_dialog_delete_without_selection(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """Clicking Delete with no selection does nothing."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    conn = manager.add(
        Connection(
            name="Safe",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )

    dialog = ConnectionManagerDialog(manager)
    dialog._delete_connection()

    assert manager.get(conn.id) is not None
    assert dialog.status_label.text == "Ready"


def test_manager_dialog_delete_cancelled(
    dialog_qt_stubs: None, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the delete confirmation keeps the connection."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    from tests.presentation_stubs import FakeMessageBox

    conn = manager.add(
        Connection(
            name="Kept",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )

    monkeypatch.setattr(
        FakeMessageBox,
        "question",
        lambda _parent, _title, _text: FakeMessageBox.StandardButton.No,
    )

    dialog = ConnectionManagerDialog(manager)
    dialog.table.select_row(0)
    dialog._delete_connection()

    assert manager.get(conn.id) is not None
    assert "Deleted" not in dialog.status_label.text


def test_manager_dialog_test_without_selection(
    dialog_qt_stubs: None, manager: ConnectionManager
) -> None:
    """Clicking Test with no selection does nothing."""
    from doctoragent.presentation.connection_dialog import ConnectionManagerDialog

    manager.add(
        Connection(
            name="Idle",
            platform_type=PlatformType.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
        )
    )

    dialog = ConnectionManagerDialog(manager)
    dialog._test_connection()

    assert dialog.status_label.text == "Ready"

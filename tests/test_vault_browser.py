"""Tests for the VaultBrowser presentation dialog."""

# mypy: ignore-errors

# ruff: noqa: N802

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.config import AegisConfig
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore

from .presentation_stubs import (
    FakeApplication,
    FakeMessageBox,
    FakeSignal,
    FakeStyle,
    install_presentation_stubs,
    restore_modules,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDialogWithClose:
    """FakeDialog replacement that also supports closeEvent."""

    def __init__(self, parent: object | None = None) -> None:
        self._parent = parent
        self._title = ""
        self._minimum_size: tuple[int, int] = (0, 0)

    def setWindowTitle(self, title: str) -> None:
        self._title = title

    def setMinimumSize(self, w: int, h: int) -> None:
        self._minimum_size = (w, h)

    def closeEvent(self, event: object) -> None:
        self._close_event_received = True

    def style(self) -> FakeStyle:
        return FakeStyle()


class _FakeVaultManager:
    """VaultManager stand-in that records decrypt calls."""

    def __init__(self, vault_path: Path, vault_key: bytes) -> None:
        self.vault_path = vault_path
        self.vault_key = vault_key
        self.decrypt_calls: list[tuple[Path, bytes, Path]] = []

    def decrypt(self, vault_path: Path, salt: bytes, destination: Path) -> None:
        self.decrypt_calls.append((vault_path, salt, destination))


def _patch_extra_stubs() -> None:
    """Add QTimer / QCloseEvent stubs that presentation_stubs omits."""
    import sys

    class _FakeQTimer:
        def __init__(self, parent: object | None = None) -> None:
            self._parent = parent
            self._single_shot = False
            self.timeout = FakeSignal()

        def setSingleShot(self, value: bool) -> None:
            self._single_shot = value

        def start(self, msec: int) -> None:
            pass

        def stop(self) -> None:
            pass

        @staticmethod
        def singleShot(_msec: int, _callback: object) -> None:
            pass

    class _FakeQCloseEvent:
        pass

    sys.modules["PyQt6.QtCore"].QTimer = _FakeQTimer
    sys.modules["PyQt6.QtGui"].QCloseEvent = _FakeQCloseEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def qt_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace PyQt6 widgets with stubs so tests run without a display."""
    saved = install_presentation_stubs()
    _patch_extra_stubs()
    FakeApplication._instance = None
    FakeMessageBox._last_warning = None
    FakeMessageBox._last_question = None
    yield
    FakeApplication._instance = None
    restore_modules(saved)


@pytest.fixture()
def config(tmp_path: Path) -> AegisConfig:
    """Test configuration with isolated paths."""
    cfg = AegisConfig()
    cfg.paths.index = tmp_path / "Index"
    cfg.paths.connections = tmp_path / "connections.json"
    cfg.paths.inbox = tmp_path / "Inbox"
    cfg.paths.vault = tmp_path / "Vault"
    return cfg


def _make_completed(
    store: TaskStore,
    config: AegisConfig,
    classification: ClassificationResult,
    *,
    create_file: bool = False,
) -> None:
    """Insert a COMPLETED task with classification metadata.

    When *create_file* is True the vault file is also written to disk so
    that deletion tests can verify filesystem cleanup.
    """
    vault_file = (
        config.paths.vault / classification.category / f"{classification.disguise_name}.bin"
    )
    task_id = uuid4()
    store.create(task_id, Path(f"/tmp/{classification.disguise_name}.txt"))
    store.update_classification(task_id, classification)
    store.update_vault_result(task_id, vault_file, b"salt", b"nonce")
    store.update_state(task_id, TaskState.COMPLETED)
    if create_file:
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.write_bytes(b"encrypted")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_browser_construction_empty_store(qt_stubs: None, config: AegisConfig) -> None:
    """VaultBrowser with an empty task store shows no items and only 'All'."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    # Use a richer base so closeEvent works during teardown.
    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    browser = VaultBrowser(store, config.paths.vault, None)

    assert browser._items == []
    assert browser.file_table._row_count == 0
    # Category tree should contain only the "All" root item.
    assert len(browser.category_tree._items) == 1
    assert browser.category_tree._items[0].text(0) == "All"


def test_browser_category_tree_populated(qt_stubs: None, config: AegisConfig) -> None:
    """Category tree lists unique categories from completed items."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    for cat in ("finance", "health", "finance"):
        cls_ = ClassificationResult(
            sensitivity=SensitivityLevel.HIGH,
            category=cat,
            tags=[],
            summary="",
            disguise_name=f"doc_{cat}_{id(cat)}",
            disguise_extension="bin",
        )
        _make_completed(store, config, cls_)

    browser = VaultBrowser(store, config.paths.vault, None)

    labels = [item.text(0) for item in browser.category_tree._items]
    assert "All" in labels
    assert "finance" in labels
    assert "health" in labels
    # Two "finance" tasks must collapse into a single category entry.
    assert labels.count("finance") == 1


def test_browser_table_renders_items(qt_stubs: None, config: AegisConfig) -> None:
    """Table rows match the number of completed items and show metadata."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    cls_ = ClassificationResult(
        sensitivity=SensitivityLevel.CRITICAL,
        category="legal",
        tags=["nda"],
        summary="non-disclosure agreement",
        disguise_name="spreadsheet_42",
        disguise_extension="bin",
    )
    _make_completed(store, config, cls_)

    browser = VaultBrowser(store, config.paths.vault, None)

    assert browser.file_table._row_count == 1
    assert browser.file_table.item(0, 0).text() == "spreadsheet_42"
    assert browser.file_table.item(0, 1).text() == "legal"
    assert browser.file_table.item(0, 2).text() == "critical"


def test_browser_category_filter_changes_table(qt_stubs: None, config: AegisConfig) -> None:
    """Selecting a category narrows the visible table rows."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.HIGH,
            category="finance",
            tags=[],
            summary="",
            disguise_name="tax_return",
            disguise_extension="bin",
        ),
    )
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.LOW,
            category="personal",
            tags=[],
            summary="",
            disguise_name="grocery_list",
            disguise_extension="bin",
        ),
    )

    browser = VaultBrowser(store, config.paths.vault, None)
    assert browser.file_table._row_count == 2

    # Simulate selecting the "finance" category in the tree.
    finance_item = None
    for tree_item in browser.category_tree._items:
        if tree_item.text(0) == "finance":
            finance_item = tree_item
            break
    assert finance_item is not None

    browser.category_tree.setCurrentItem(finance_item)
    browser.category_tree.emit_current_item_changed(finance_item)

    assert browser.file_table._row_count == 1
    assert browser.file_table.item(0, 0).text() == "tax_return"


def test_decrypt_item_no_vault_manager_shows_warning(qt_stubs: None, config: AegisConfig) -> None:
    """_decrypt_item shows a warning when no vault key was provided."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)
    FakeMessageBox._last_warning = None

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.HIGH,
            category="finance",
            tags=[],
            summary="",
            disguise_name="secret",
            disguise_extension="bin",
        ),
    )

    # vault_key=None -> vault_manager stays None.
    browser = VaultBrowser(store, config.paths.vault, None)
    assert browser.vault_manager is None

    browser._decrypt_item(browser._items[0])

    assert FakeMessageBox._last_warning is not None
    _, title, text = FakeMessageBox._last_warning
    assert title == "Decrypt"
    assert "No vault key" in text


def test_decrypt_item_creates_temp_file(qt_stubs: None, config: AegisConfig) -> None:
    """_decrypt_item invokes VaultManager.decrypt and tracks the temp file."""
    from doctoragent.presentation import vault_browser as vb_module
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.HIGH,
            category="finance",
            tags=[],
            summary="",
            disguise_name="secret",
            disguise_extension="bin",
        ),
    )

    original_vm = vb_module.VaultManager
    vb_module.VaultManager = _FakeVaultManager  # type: ignore[misc]
    try:
        browser = VaultBrowser(store, config.paths.vault, b"k" * 32)
        assert isinstance(browser.vault_manager, _FakeVaultManager)

        browser._decrypt_item(browser._items[0])
    finally:
        vb_module.VaultManager = original_vm

    assert len(browser._temp_files) == 1
    temp_path = browser._temp_files[0]
    assert temp_path.exists()
    assert "doctoragent_" in temp_path.name

    vm: _FakeVaultManager = browser.vault_manager  # type: ignore[assignment]
    assert len(vm.decrypt_calls) == 1
    assert vm.decrypt_calls[0][1] == b"salt"

    # Verify preview was updated with the decrypted path.
    assert "Decrypted to:" in browser.preview_text.toPlainText()

    # Cleanup.
    temp_path.unlink(missing_ok=True)


def test_open_item_creates_temp_file(qt_stubs: None, config: AegisConfig) -> None:
    """_open_item decrypts, tracks the temp file, and opens the path."""
    from doctoragent.presentation import vault_browser as vb_module
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.MEDIUM,
            category="work",
            tags=[],
            summary="",
            disguise_name="report",
            disguise_extension="bin",
        ),
    )

    original_vm = vb_module.VaultManager
    original_open = vb_module.open_path
    vb_module.VaultManager = _FakeVaultManager  # type: ignore[misc]
    opened_paths: list[Path] = []
    vb_module.open_path = lambda p: opened_paths.append(p)  # type: ignore[assignment]
    try:
        browser = VaultBrowser(store, config.paths.vault, b"k" * 32)

        browser._open_item(browser._items[0])
    finally:
        vb_module.VaultManager = original_vm
        vb_module.open_path = original_open

    assert len(browser._temp_files) == 1
    assert browser._temp_files[0].exists()
    assert len(opened_paths) == 1
    assert opened_paths[0] == browser._temp_files[0]

    # Cleanup.
    browser._temp_files[0].unlink(missing_ok=True)


def test_close_event_cleans_up_temp_files(qt_stubs: None, config: AegisConfig) -> None:
    """closeEvent removes all tracked temp files and clears the list."""
    from doctoragent.presentation import vault_browser as vb_module
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.LOW,
            category="misc",
            tags=[],
            summary="",
            disguise_name="blob",
            disguise_extension="bin",
        ),
    )

    original_vm = vb_module.VaultManager
    vb_module.VaultManager = _FakeVaultManager  # type: ignore[misc]
    try:
        browser = VaultBrowser(store, config.paths.vault, b"k" * 32)
        browser._decrypt_item(browser._items[0])
        browser._decrypt_item(browser._items[0])
    finally:
        vb_module.VaultManager = original_vm

    assert len(browser._temp_files) == 2
    temp_paths = list(browser._temp_files)
    assert all(p.exists() for p in temp_paths)

    browser.closeEvent(None)

    assert browser._temp_files == []
    assert all(not p.exists() for p in temp_paths)


def test_delete_item_removes_from_store_and_filesystem(qt_stubs: None, config: AegisConfig) -> None:
    """_delete_item removes the task from the store and deletes the vault file."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)
    FakeMessageBox._last_question = None

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.HIGH,
            category="finance",
            tags=[],
            summary="",
            disguise_name="tax_return",
            disguise_extension="bin",
        ),
        create_file=True,
    )

    browser = VaultBrowser(store, config.paths.vault, None)
    assert len(browser._items) == 1

    vault_file = browser._items[0]["vault_path"]
    assert vault_file.exists()

    # FakeMessageBox.question returns Yes by default.
    browser._delete_item(browser._items[0])

    # After deletion the item list should be empty.
    assert browser._items == []
    assert browser.file_table._row_count == 0
    # Vault file should have been removed from disk.
    assert not vault_file.exists()
    # Confirmation dialog should have been shown.
    assert FakeMessageBox._last_question is not None
    _, title, text = FakeMessageBox._last_question
    assert title == "Confirm Delete"
    assert "tax_return" in text


def test_delete_item_cancel_does_nothing(qt_stubs: None, config: AegisConfig) -> None:
    """Cancelling the delete confirmation leaves everything unchanged."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.MEDIUM,
            category="personal",
            tags=[],
            summary="",
            disguise_name="diary",
            disguise_extension="bin",
        ),
        create_file=True,
    )

    browser = VaultBrowser(store, config.paths.vault, None)
    assert len(browser._items) == 1

    vault_file = browser._items[0]["vault_path"]

    # Simulate the user clicking "No" in the confirmation dialog.
    FakeMessageBox.question = classmethod(  # type: ignore[method-assign]
        lambda cls, parent, title, text: FakeMessageBox.StandardButton.No
    )
    try:
        browser._delete_item(browser._items[0])
    finally:
        # Restore the default behaviour for subsequent tests.
        FakeMessageBox.question = classmethod(  # type: ignore[method-assign]
            lambda cls, parent, title, text: FakeMessageBox.StandardButton.Yes
        )

    # Nothing should have changed.
    assert len(browser._items) == 1
    assert browser.file_table._row_count == 1
    assert vault_file.exists()


def test_decrypt_item_none_is_noop(qt_stubs: None, config: AegisConfig) -> None:
    """Passing None to _decrypt_item is a silent no-op."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)
    FakeMessageBox._last_warning = None

    store = TaskStore(config.paths.index / "tasks.db")
    browser = VaultBrowser(store, config.paths.vault, b"k" * 32)

    browser._decrypt_item(None)

    assert browser._temp_files == []
    assert FakeMessageBox._last_warning is None


def test_open_item_no_vault_manager_shows_warning(qt_stubs: None, config: AegisConfig) -> None:
    """_open_item shows a warning when no vault key was provided."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)
    FakeMessageBox._last_warning = None

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.LOW,
            category="misc",
            tags=[],
            summary="",
            disguise_name="public_doc",
            disguise_extension="bin",
        ),
    )

    browser = VaultBrowser(store, config.paths.vault, None)
    assert browser.vault_manager is None

    browser._open_item(browser._items[0])

    assert FakeMessageBox._last_warning is not None
    _, title, text = FakeMessageBox._last_warning
    assert title == "Open"
    assert "No vault key" in text


def test_browser_toolbar_has_view_toggle_actions(qt_stubs: None, config: AegisConfig) -> None:
    """Toolbar contains list and grid view toggle actions that are checkable."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    browser = VaultBrowser(store, config.paths.vault, None)

    # Both view actions should exist and be checkable.
    assert browser._view_list_action is not None
    assert browser._view_grid_action is not None
    assert browser._view_list_action._checkable is True
    assert browser._view_grid_action._checkable is True
    # List view should be checked by default.
    assert browser._view_list_action._checked is True
    assert browser._view_grid_action._checked is False


def test_browser_set_view_switches_stack(qt_stubs: None, config: AegisConfig) -> None:
    """_set_view toggles between list and grid pages in the view stack."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    browser = VaultBrowser(store, config.paths.vault, None)

    # Default is list view (index 0).
    assert browser._view_stack.currentIndex() == 0

    # Switch to grid view.
    browser._set_view("grid")
    assert browser._view_stack.currentIndex() == 1
    assert browser._view_list_action._checked is False
    assert browser._view_grid_action._checked is True

    # Switch back to list view.
    browser._set_view("list")
    assert browser._view_stack.currentIndex() == 0
    assert browser._view_list_action._checked is True
    assert browser._view_grid_action._checked is False


def test_browser_sort_combo_changes_sort(qt_stubs: None, config: AegisConfig) -> None:
    """Changing the sort combo updates the sort column and refreshes the table."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    browser = VaultBrowser(store, config.paths.vault, None)

    # Default sort column is COL_NAME.
    assert browser._sort_column == browser.COL_NAME

    # Simulate selecting "Date" in the sort combo (index 1).
    browser._sort_combo.setCurrentIndex(1)
    assert browser._sort_column == browser.COL_TIMESTAMP

    # Simulate selecting "Size" (index 2).
    browser._sort_combo.setCurrentIndex(2)
    assert browser._sort_column == browser.COL_SIZE

    # Back to "Name" (index 0).
    browser._sort_combo.setCurrentIndex(0)
    assert browser._sort_column == browser.COL_NAME


def test_browser_header_click_toggles_sort_order(qt_stubs: None, config: AegisConfig) -> None:
    """Clicking a column header toggles sort order; clicking a new column resets to ascending."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    browser = VaultBrowser(store, config.paths.vault, None)

    from PyQt6.QtCore import Qt

    # Initially ascending by name.
    assert browser._sort_column == browser.COL_NAME
    assert browser._sort_order == Qt.SortOrder.AscendingOrder

    # Click the same column header -> toggle to descending.
    browser._header_clicked(browser.COL_NAME)
    assert browser._sort_column == browser.COL_NAME
    assert browser._sort_order == Qt.SortOrder.DescendingOrder

    # Click again -> back to ascending.
    browser._header_clicked(browser.COL_NAME)
    assert browser._sort_column == browser.COL_NAME
    assert browser._sort_order == Qt.SortOrder.AscendingOrder

    # Click a different column -> ascending on new column.
    browser._header_clicked(browser.COL_TIMESTAMP)
    assert browser._sort_column == browser.COL_TIMESTAMP
    assert browser._sort_order == Qt.SortOrder.AscendingOrder


def test_browser_grid_view_populated(qt_stubs: None, config: AegisConfig) -> None:
    """Grid (icon) view contains one item per completed task."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    for name in ("alpha", "beta", "gamma"):
        cls_ = ClassificationResult(
            sensitivity=SensitivityLevel.MEDIUM,
            category="docs",
            tags=[],
            summary="",
            disguise_name=name,
            disguise_extension=".txt",
        )
        _make_completed(store, config, cls_)

    browser = VaultBrowser(store, config.paths.vault, None)

    # Grid should have one item per task.
    assert len(browser._grid_list._items) == 3
    grid_texts = [item._text for item in browser._grid_list._items]
    assert any("alpha" in t for t in grid_texts)
    assert any("beta" in t for t in grid_texts)
    assert any("gamma" in t for t in grid_texts)


def test_browser_sort_items_by_name(qt_stubs: None, config: AegisConfig) -> None:
    """Table rows are sorted alphabetically by disguise name by default."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    for name in ("charlie", "alpha", "bravo"):
        cls_ = ClassificationResult(
            sensitivity=SensitivityLevel.LOW,
            category="misc",
            tags=[],
            summary="",
            disguise_name=name,
            disguise_extension=".bin",
        )
        _make_completed(store, config, cls_)

    browser = VaultBrowser(store, config.paths.vault, None)

    names = [browser.file_table.item(r, 0).text() for r in range(browser.file_table._row_count)]
    assert names == ["alpha", "bravo", "charlie"]


def test_browser_category_filter_updates_grid(qt_stubs: None, config: AegisConfig) -> None:
    """Selecting a category also filters the grid view."""
    from doctoragent.presentation.vault_browser import VaultBrowser

    VaultBrowser.__bases__ = (_FakeDialogWithClose,)

    store = TaskStore(config.paths.index / "tasks.db")
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.HIGH,
            category="finance",
            tags=[],
            summary="",
            disguise_name="tax_return",
            disguise_extension=".bin",
        ),
    )
    _make_completed(
        store,
        config,
        ClassificationResult(
            sensitivity=SensitivityLevel.LOW,
            category="personal",
            tags=[],
            summary="",
            disguise_name="grocery_list",
            disguise_extension=".bin",
        ),
    )

    browser = VaultBrowser(store, config.paths.vault, None)
    assert len(browser._grid_list._items) == 2

    # Filter to "finance" category.
    finance_item = None
    for tree_item in browser.category_tree._items:
        if tree_item.text(0) == "finance":
            finance_item = tree_item
            break
    assert finance_item is not None

    browser.category_tree.setCurrentItem(finance_item)
    browser.category_tree.emit_current_item_changed(finance_item)

    assert len(browser._grid_list._items) == 1
    assert "tax_return" in browser._grid_list._items[0]._text

"""Vault browser UI for DoctorAgent."""

from __future__ import annotations

import logging
import mimetypes
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.execution.vault import VaultManager
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.presentation.utils import human_size, open_path

try:
    from PyQt6.QtCore import QSize, Qt, QTimer
    from PyQt6.QtGui import QCloseEvent, QIcon, QPixmap
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMenu,
        QMessageBox,
        QPushButton,
        QSplitter,
        QStackedWidget,
        QStyle,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QToolBar,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc


# ---------------------------------------------------------------------------
# File-type helpers
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS: set[str] = {
    ".txt",
    ".md",
    ".rst",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".less",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".log",
    ".sh",
    ".bash",
    ".zsh",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".swift",
    ".sql",
    ".r",
    ".m",
    ".tex",
    ".bib",
}

_IMAGE_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".ico",
    ".svg",
    ".tiff",
    ".tif",
    ".ppm",
    ".pgm",
    ".pbm",
}


def _file_category(path: Path) -> str:
    """Return 'text', 'image', or 'binary' based on extension."""
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    return "binary"


# ---------------------------------------------------------------------------
# VaultBrowser
# ---------------------------------------------------------------------------


class VaultBrowser(QDialog):
    """Browse, preview and manage completed Vault tasks."""

    # Column indices (must match horizontal header labels)
    COL_NAME = 0
    COL_CATEGORY = 1
    COL_SENSITIVITY = 2
    COL_TIMESTAMP = 3
    COL_SIZE = 4  # hidden / used for sorting

    def __init__(
        self,
        task_store: TaskStore | None,
        vault_path: Path,
        vault_key: bytes | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task_store = task_store
        self.vault_path = vault_path
        self.vault_key = vault_key
        self.vault_manager = VaultManager(vault_path, vault_key) if vault_key else None
        self._items: list[dict[str, Any]] = []
        self._temp_files: list[Path] = []
        self._cleanup_timers: list[QTimer] = []
        self._file_icon_cache: dict[str, QIcon] = {}

        # ---- sorting state --------------------------------------------------
        self._sort_column = self.COL_NAME
        self._sort_order = Qt.SortOrder.AscendingOrder

        # ---- search/filter state -------------------------------------------
        self._search_keyword: str = ""
        self._filter_sensitivity: str = ""  # "" means "any"
        self._filter_tags: list[str] = []

        # ---- window setup ---------------------------------------------------
        self.setWindowTitle("Vault Browser")
        self.setMinimumSize(1000, 650)

        outer = QVBoxLayout(self)

        # ---- toolbar --------------------------------------------------------
        self._toolbar = QToolBar("Actions")
        self._toolbar.setIconSize(QSize(20, 20))
        self._toolbar.setMovable(False)

        # view toggle
        _style = self.style()
        assert _style is not None
        _list_icon = QIcon.fromTheme(
            "view-list-details",
            _style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView),
        )
        self._view_list_action = self._toolbar.addAction(_list_icon, "List View")
        assert self._view_list_action is not None
        self._view_list_action.setCheckable(True)
        self._view_list_action.setChecked(True)
        self._view_list_action.triggered.connect(lambda: self._set_view("list"))

        _grid_icon = QIcon.fromTheme(
            "view-list-icons",
            _style.standardIcon(QStyle.StandardPixmap.SP_FileDialogInfoView),
        )
        self._view_grid_action = self._toolbar.addAction(_grid_icon, "Grid View")
        assert self._view_grid_action is not None
        self._view_grid_action.setCheckable(True)
        self._view_grid_action.triggered.connect(lambda: self._set_view("grid"))

        self._toolbar.addSeparator()

        # sort combo
        self._toolbar.addWidget(QLabel(" Sort: "))
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Name", "Date", "Size"])
        self._sort_combo.setCurrentIndex(0)
        self._sort_combo.currentIndexChanged.connect(self._sort_changed)
        self._toolbar.addWidget(self._sort_combo)

        # batch action buttons
        self._toolbar.addSeparator()
        self._batch_decrypt_btn = QPushButton("Batch Decrypt")
        self._batch_decrypt_btn.clicked.connect(self._batch_decrypt)
        self._batch_decrypt_btn.setToolTip("Decrypt selected items to a directory")
        self._toolbar.addWidget(self._batch_decrypt_btn)

        self._batch_delete_btn = QPushButton("Batch Delete")
        self._batch_delete_btn.clicked.connect(self._batch_delete)
        self._batch_delete_btn.setToolTip("Delete selected items")
        self._toolbar.addWidget(self._batch_delete_btn)

        outer.addWidget(self._toolbar)

        # ---- search / filter bar -------------------------------------------
        search_bar = QWidget()
        search_layout = QFormLayout(search_bar)
        search_layout.setContentsMargins(4, 2, 4, 2)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search name, category, summary, tags...")
        self._search_input.textChanged.connect(self._on_search_changed)

        self._sensitivity_combo = QComboBox()
        self._sensitivity_combo.addItem("(any)")
        for level in SensitivityLevel:
            self._sensitivity_combo.addItem(level.value)
        self._sensitivity_combo.currentTextChanged.connect(self._on_sensitivity_changed)

        self._tags_input = QLineEdit()
        self._tags_input.setPlaceholderText("tag1, tag2, ...")
        self._tags_input.textChanged.connect(self._on_tags_changed)

        search_layout.addRow("Keyword:", self._search_input)
        search_layout.addRow("Sensitivity:", self._sensitivity_combo)
        search_layout.addRow("Tags:", self._tags_input)
        outer.addWidget(search_bar)

        # ---- main splitter --------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # left: category tree
        self.category_tree = QTreeWidget()
        self.category_tree.setHeaderLabel("Categories")
        self._all_item = QTreeWidgetItem(self.category_tree, ["All"])
        self.category_tree.addTopLevelItem(self._all_item)
        self.category_tree.currentItemChanged.connect(self._filter_changed)
        splitter.addWidget(self.category_tree)

        # centre: stacked list / grid
        self._view_stack = QStackedWidget()

        #  page 0: list (table)
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels(
            ["Disguise Name", "Category", "Sensitivity", "Timestamp"]
        )
        header = self.file_table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.sectionClicked.connect(self._header_clicked)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.itemSelectionChanged.connect(self._selection_changed)
        self.file_table.cellDoubleClicked.connect(self._decrypt_selected)
        self.file_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_table.customContextMenuRequested.connect(self._show_context_menu)
        self._view_stack.addWidget(self.file_table)

        #  page 1: grid
        self._grid_list = QListWidget()
        self._grid_list.setViewMode(QListWidget.ViewMode.IconMode)
        self._grid_list.setIconSize(QSize(64, 64))
        self._grid_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._grid_list.setMovement(QListWidget.Movement.Static)
        self._grid_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._grid_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._grid_list.customContextMenuRequested.connect(self._show_context_menu)
        self._grid_list.itemSelectionChanged.connect(self._grid_selection_changed)
        self._grid_list.itemDoubleClicked.connect(self._grid_double_clicked)
        self._view_stack.addWidget(self._grid_list)

        splitter.addWidget(self._view_stack)

        # right: preview panel
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        preview_layout.addWidget(QLabel("Metadata"))
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(160)
        preview_layout.addWidget(self.preview_text)

        self._preview_btn = QPushButton("Preview Content")
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self._preview_content)
        preview_layout.addWidget(self._preview_btn)

        self._preview_stack = QStackedWidget()
        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_stack.addWidget(self._preview_text)
        self._preview_image = QLabel()
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setMinimumSize(200, 200)
        self._preview_stack.addWidget(self._preview_image)
        preview_layout.addWidget(self._preview_stack, stretch=1)

        splitter.addWidget(preview)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        outer.addWidget(splitter)

        self._load_items()
        self._refresh_categories()
        self._refresh_table()
        self._refresh_grid()

    # -------------------------------------------------------------------
    # Data loading helpers
    # -------------------------------------------------------------------

    def _load_items(self) -> None:
        """Load COMPLETED tasks with classification metadata."""
        self._items = []
        if self.task_store is None:
            return

        with self.task_store._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                """
                SELECT task_id, vault_path, salt, classification, created_at, updated_at
                FROM tasks
                WHERE state = ? AND vault_path IS NOT NULL
                """,
                (TaskState.COMPLETED.name,),
            ).fetchall()

        for row in rows:
            classification_json = row["classification"]
            if not classification_json:
                continue
            try:
                classification = ClassificationResult.model_validate_json(classification_json)
            except (ValueError, TypeError):
                logging.getLogger(__name__).warning(
                    "Failed to validate classification JSON for item %s", row["task_id"]
                )
                continue
            vault_path = Path(row["vault_path"])
            file_size = vault_path.stat().st_size if vault_path.exists() else 0
            self._items.append(
                {
                    "task_id": row["task_id"],
                    "vault_path": vault_path,
                    "salt": row["salt"],
                    "classification": classification,
                    "timestamp": row["updated_at"] or row["created_at"] or "",
                    "size": file_size,
                }
            )

    # -------------------------------------------------------------------
    # Category tree
    # -------------------------------------------------------------------

    def _refresh_categories(self) -> None:
        """Populate the category tree from loaded items."""
        self.category_tree.clear()
        self._all_item = QTreeWidgetItem(self.category_tree, ["All"])
        self.category_tree.addTopLevelItem(self._all_item)
        for category in sorted({item["classification"].category for item in self._items}):
            self.category_tree.addTopLevelItem(QTreeWidgetItem(self.category_tree, [category]))
        self.category_tree.setCurrentItem(self._all_item)

    def _selected_category(self) -> str | None:
        """Return the selected category filter, or None for "All"."""
        item = self.category_tree.currentItem()
        if item is None or item == self._all_item:
            return None
        return item.text(0)

    def _visible_items(self) -> list[dict[str, Any]]:
        """Return items matching the current category and search filters, sorted."""
        category = self._selected_category()
        if category is None:
            items = list(self._items)
        else:
            items = [item for item in self._items if item["classification"].category == category]

        # Apply search filters (keyword, sensitivity, tags).
        if self._search_keyword:
            keyword = self._search_keyword.lower()
            filtered: list[dict[str, Any]] = []
            for item in items:
                classification: ClassificationResult = item["classification"]
                haystack = " ".join(
                    [
                        classification.disguise_name,
                        classification.category,
                        classification.summary,
                        " ".join(classification.tags),
                    ]
                ).lower()
                if keyword in haystack:
                    filtered.append(item)
            items = filtered

        if self._filter_sensitivity:
            items = [
                item
                for item in items
                if item["classification"].sensitivity.value == self._filter_sensitivity
            ]

        if self._filter_tags:
            requested = {tag.lower() for tag in self._filter_tags}
            items = [
                item
                for item in items
                if requested.intersection({tag.lower() for tag in item["classification"].tags})
            ]

        return self._sort_items(items)

    def _filter_changed(
        self,
        _current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        """Refresh the file list when the category selection changes."""
        self._refresh_table()
        self._refresh_grid()

    # -------------------------------------------------------------------
    # Search filter handlers
    # -------------------------------------------------------------------

    def _on_search_changed(self, text: str) -> None:
        """Update the keyword filter and refresh views."""
        self._search_keyword = text.strip()
        self._refresh_table()
        self._refresh_grid()

    def _on_sensitivity_changed(self, text: str) -> None:
        """Update the sensitivity filter and refresh views."""
        self._filter_sensitivity = "" if text == "(any)" else text.strip()
        self._refresh_table()
        self._refresh_grid()

    def _on_tags_changed(self, text: str) -> None:
        """Update the tag filter and refresh views."""
        self._filter_tags = [tag.strip() for tag in text.strip().split(",") if tag.strip()]
        self._refresh_table()
        self._refresh_grid()

    def set_search_filter(self, query: str) -> None:
        """Pre-set the keyword search filter from an external caller.

        This is used by the tray application's "Search Vault" action to
        open the browser with a pre-filled search query instead of using
        the now-removed ``SearchVaultDialog``.
        """
        self._search_input.setText(query)
        # _on_search_changed is triggered by setText, so no manual refresh
        # is needed here.

    # -------------------------------------------------------------------
    # Sorting
    # -------------------------------------------------------------------

    _SORT_KEYS = {
        "Name": lambda i: i["classification"].disguise_name.lower(),
        "Date": lambda i: i["timestamp"],
        "Size": lambda i: i["size"],
    }

    def _sort_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return *items* sorted by the current sort column and order."""
        key_map = {self.COL_NAME: "Name", self.COL_TIMESTAMP: "Date", self.COL_SIZE: "Size"}
        sort_key_name = key_map.get(self._sort_column, "Name")
        key_fn = self._SORT_KEYS.get(sort_key_name, self._SORT_KEYS["Name"])
        return sorted(items, key=key_fn, reverse=(self._sort_order == Qt.SortOrder.DescendingOrder))

    def _sort_changed(self, index: int) -> None:
        """Handle toolbar sort combo change."""
        name = self._sort_combo.itemText(index)
        if name == "Size":
            self._sort_column = self.COL_SIZE
        elif name == "Date":
            self._sort_column = self.COL_TIMESTAMP
        else:
            self._sort_column = self.COL_NAME
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._refresh_table()
        self._refresh_grid()

    def _header_clicked(self, logical_index: int) -> None:
        """Toggle sort on table header click."""
        if logical_index == self._sort_column:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column = logical_index
            self._sort_order = Qt.SortOrder.AscendingOrder
        # sync toolbar combo
        col_to_name = {self.COL_NAME: 0, self.COL_TIMESTAMP: 1}
        combo_index = col_to_name.get(self._sort_column, 0)
        self._sort_combo.blockSignals(True)
        self._sort_combo.setCurrentIndex(combo_index)
        self._sort_combo.blockSignals(False)
        self._refresh_table()
        self._refresh_grid()

    # -------------------------------------------------------------------
    # Table (list) view
    # -------------------------------------------------------------------

    def _refresh_table(self) -> None:
        """Render visible items into the file table."""
        visible = self._visible_items()
        self.file_table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            classification = item["classification"]
            self.file_table.setItem(row, 0, QTableWidgetItem(classification.disguise_name))
            self.file_table.setItem(row, 1, QTableWidgetItem(classification.category))
            self.file_table.setItem(row, 2, QTableWidgetItem(classification.sensitivity.value))
            self.file_table.setItem(row, 3, QTableWidgetItem(item["timestamp"]))
            # store reference data for retrieval
            table_item = self.file_table.item(row, 0)
            if table_item is not None:
                table_item.setData(Qt.ItemDataRole.UserRole, item)
        self._selection_changed()

    # -------------------------------------------------------------------
    # Grid view
    # -------------------------------------------------------------------

    def _grid_icon_for(self, item: dict[str, Any]) -> QIcon:
        """Return an icon for *item* based on its disguise extension."""
        classification: ClassificationResult = item["classification"]
        ext = classification.disguise_extension.lower()
        if ext not in self._file_icon_cache:
            mime_type, _ = mimetypes.guess_type(f"file{ext}")
            _style = self.style()
            fallback = (
                _style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
                if _style is not None
                else QIcon()
            )
            icon = QIcon.fromTheme(
                mime_type or "text-x-generic",
                fallback,
            )
            self._file_icon_cache[ext] = icon
        return self._file_icon_cache[ext]

    def _refresh_grid(self) -> None:
        """Re-populate the grid view with visible items."""
        self._grid_list.clear()
        for item in self._visible_items():
            classification = item["classification"]
            label_text = f"{classification.disguise_name}\n[{classification.category}]"
            list_item = QListWidgetItem(self._grid_icon_for(item), label_text)
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            list_item.setSizeHint(QSize(140, 100))
            self._grid_list.addItem(list_item)

    def _set_view(self, mode: str) -> None:
        """Switch the centre view between list and grid."""
        assert self._view_list_action is not None
        assert self._view_grid_action is not None
        if mode == "list":
            self._view_stack.setCurrentIndex(0)
            self._view_list_action.setChecked(True)
            self._view_grid_action.setChecked(False)
        else:
            self._view_stack.setCurrentIndex(1)
            self._view_list_action.setChecked(False)
            self._view_grid_action.setChecked(True)

    def _grid_selection_changed(self) -> None:
        """Handle grid selection → update preview."""
        items = self._grid_list.selectedItems()
        if not items:
            self.preview_text.setPlainText("Select a Vault item to view metadata.")
            self._preview_btn.setEnabled(False)
            return
        self._show_item_metadata(items[0].data(Qt.ItemDataRole.UserRole))

    def _grid_double_clicked(self, list_item: QListWidgetItem) -> None:
        """Decrypt the double-clicked grid item."""
        item = list_item.data(Qt.ItemDataRole.UserRole)
        if item:
            self._decrypt_item(item)

    # -------------------------------------------------------------------
    # Item selection helpers
    # -------------------------------------------------------------------

    def _current_item(self) -> dict[str, Any] | None:
        """Return the item for the first selected table row."""
        selected = self.file_table.selectedItems()
        if not selected:
            return None
        row = self.file_table.row(selected[0])
        table_item = self.file_table.item(row, 0)
        if table_item is None:
            return None
        return table_item.data(Qt.ItemDataRole.UserRole)

    def _selected_items(self) -> list[dict[str, Any]]:
        """Return all currently selected items (table or grid)."""
        if self._view_stack.currentIndex() == 0:
            items: list[dict[str, Any]] = []
            seen: set[int] = set()
            for sel in self.file_table.selectedItems():
                row = self.file_table.row(sel)
                if row in seen:
                    continue
                seen.add(row)
                table_item = self.file_table.item(row, 0)
                if table_item is not None:
                    data = table_item.data(Qt.ItemDataRole.UserRole)
                    if data is not None:
                        items.append(data)
            return items
        else:
            items = []
            for list_item in self._grid_list.selectedItems():
                data = list_item.data(Qt.ItemDataRole.UserRole)
                if data is not None:
                    items.append(data)
            return items

    # -------------------------------------------------------------------
    # Preview panel
    # -------------------------------------------------------------------

    def _selection_changed(self) -> None:
        """Update the preview panel for the selected item."""
        item = self._current_item()
        if item is None:
            self.preview_text.setPlainText("Select a Vault item to view metadata.")
            self._preview_btn.setEnabled(False)
            self._clear_content_preview()
            return
        self._show_item_metadata(item)

    def _show_item_metadata(self, item: dict[str, Any]) -> None:
        """Display metadata for *item* in the preview panel."""
        classification: ClassificationResult = item["classification"]
        lines = [
            f"Task ID: {item['task_id']}",
            f"Vault Path: {item['vault_path']}",
            f"Disguise Name: {classification.disguise_name}",
            f"Extension: {classification.disguise_extension}",
            f"Category: {classification.category}",
            f"Sensitivity: {classification.sensitivity.value}",
            f"Tags: {', '.join(classification.tags)}",
            f"Summary: {classification.summary}",
            f"Encrypted Size: {human_size(item['size'])}",
            f"Timestamp: {item['timestamp']}",
        ]
        self.preview_text.setPlainText("\n".join(lines))
        self._preview_btn.setEnabled(True)
        self._clear_content_preview()

    def _clear_content_preview(self) -> None:
        """Reset the content preview area."""
        self._preview_stack.setCurrentIndex(0)
        self._preview_text.clear()
        self._preview_image.clear()

    def _preview_content(self) -> None:
        """Decrypt and preview the content of the selected item."""
        item = self._current_item()
        if item is None or self.vault_manager is None:
            return
        dest_path: Path | None = None
        try:
            fd, dest = tempfile.mkstemp(prefix="doctoragent_", suffix="_preview")
            os.close(fd)
            dest_path = Path(dest)
            self.vault_manager.decrypt(item["vault_path"], item["salt"], dest_path)

            file_cat = _file_category(dest_path)
            if file_cat == "text":
                self._preview_stack.setCurrentIndex(0)
                # Avoid loading very large decrypted files into memory; cap at
                # 1 MB and read at most a few thousand characters for display.
                if dest_path.stat().st_size > 1_000_000:
                    self._preview_text.setPlainText("(file too large to preview)")
                else:
                    with dest_path.open("r", encoding="utf-8", errors="replace") as f:
                        raw = f.read(5000)
                    self._preview_text.setPlainText(raw)
            elif file_cat == "image":
                self._preview_stack.setCurrentIndex(1)
                pixmap = QPixmap(str(dest_path))
                if pixmap.isNull():
                    self._preview_stack.setCurrentIndex(0)
                    self._preview_text.setPlainText("(image failed to load)")
                else:
                    scaled = pixmap.scaled(
                        400,
                        400,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self._preview_image.setPixmap(scaled)
            else:
                self._preview_stack.setCurrentIndex(0)
                self._preview_text.setPlainText(
                    "(binary content — preview not available)\n\n"
                    f"File size: {human_size(dest_path.stat().st_size)}"
                )
        except Exception as exc:
            self._preview_stack.setCurrentIndex(0)
            self._preview_text.setPlainText(f"Preview failed: {exc}")
        finally:
            if dest_path is not None:
                try:
                    dest_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # -------------------------------------------------------------------
    # Context menu
    # -------------------------------------------------------------------

    def _show_context_menu(self, position: Any) -> None:
        """Show context menu with single-item and batch operations."""
        selected = self._selected_items()
        if not selected:
            return

        menu = QMenu(self)

        if len(selected) == 1:
            item = selected[0]
            decrypt_action = menu.addAction("Decrypt")
            open_action = menu.addAction("Open")
            delete_action = menu.addAction("Delete")
            if decrypt_action is None or open_action is None or delete_action is None:
                return
            decrypt_action.triggered.connect(lambda _c=False, i=item: self._decrypt_item(i))
            open_action.triggered.connect(lambda _c=False, i=item: self._open_item(i))
            delete_action.triggered.connect(lambda _c=False, i=item: self._delete_item(i))
        else:
            batch_label = f"{len(selected)} selected"
            header_action = menu.addAction(batch_label)
            if header_action is None:
                return
            header_action.setEnabled(False)
            menu.addSeparator()
            decrypt_action = menu.addAction(f"Decrypt {len(selected)} items...")
            delete_action = menu.addAction(f"Delete {len(selected)} items")
            if decrypt_action is None or delete_action is None:
                return
            decrypt_action.triggered.connect(lambda _c=False: self._batch_decrypt())
            delete_action.triggered.connect(lambda _c=False: self._batch_delete())

        viewport: QWidget | None = None
        if self._view_stack.currentIndex() == 0:
            viewport = self.file_table.viewport()
        else:
            viewport = self._grid_list.viewport()
        if viewport is None:
            return
        menu.exec(viewport.mapToGlobal(position))

    # -------------------------------------------------------------------
    # Single-item operations (kept for backward compat)
    # -------------------------------------------------------------------

    def _decrypt_selected(self, _row: int, _column: int) -> None:
        """Decrypt the currently selected item on double-click."""
        self._decrypt_item(self._current_item())

    def _decrypt_item(self, item: dict[str, Any] | None) -> None:
        """Decrypt *item* to a temporary path and show the result."""
        if item is None:
            return
        if self.vault_manager is None:
            QMessageBox.warning(self, "Decrypt", "No vault key configured.")
            return
        dest_path: Path | None = None
        try:
            fd, dest = tempfile.mkstemp(prefix="doctoragent_", suffix="_decrypted")
            os.close(fd)
            dest_path = Path(dest)
            # Track the temp file before decrypting so a decryption failure
            # can still clean it up via closeEvent.
            self._temp_files.append(dest_path)
            self.vault_manager.decrypt(item["vault_path"], item["salt"], dest_path)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda p=dest_path: p.unlink(missing_ok=True))
            timer.start(300_000)
            self._cleanup_timers.append(timer)
            self.preview_text.setPlainText(f"Decrypted to:\n{dest}")
        except Exception as exc:
            # Decrypt failed — clean up the temp file immediately so it does
            # not leak until the dialog closes.
            if dest_path is not None:
                dest_path.unlink(missing_ok=True)
                try:
                    self._temp_files.remove(dest_path)
                except ValueError:
                    pass
            logging.getLogger(__name__).warning(
                "Decrypt failed for %s: %s", item["vault_path"], exc
            )
            QMessageBox.warning(self, "Decrypt", "Failed to decrypt the selected file.")

    def _open_item(self, item: dict[str, Any] | None) -> None:
        """Decrypt *item* and open it with the platform default application."""
        if item is None:
            return
        if self.vault_manager is None:
            QMessageBox.warning(self, "Open", "No vault key configured.")
            return
        dest_path: Path | None = None
        try:
            fd, dest = tempfile.mkstemp(prefix="doctoragent_", suffix="_decrypted")
            os.close(fd)
            dest_path = Path(dest)
            # Track the temp file before decrypting so a decryption failure
            # can still clean it up via closeEvent.
            self._temp_files.append(dest_path)
            self.vault_manager.decrypt(item["vault_path"], item["salt"], dest_path)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda p=dest_path: p.unlink(missing_ok=True))
            timer.start(300_000)
            self._cleanup_timers.append(timer)
            open_path(dest_path)
        except Exception as exc:
            # Decrypt failed — clean up the temp file immediately so it does
            # not leak until the dialog closes.
            if dest_path is not None:
                dest_path.unlink(missing_ok=True)
                try:
                    self._temp_files.remove(dest_path)
                except ValueError:
                    pass
            logging.getLogger(__name__).warning("Open failed for %s: %s", item["vault_path"], exc)
            QMessageBox.warning(self, "Open", "Failed to open the selected file.")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Clean up any remaining decrypted temp files and scheduled cleanup timers."""
        for timer in self._cleanup_timers:
            timer.stop()
        self._cleanup_timers.clear()
        for path in self._temp_files:
            path.unlink(missing_ok=True)
        self._temp_files.clear()
        super().closeEvent(event)

    def _delete_item(self, item: dict[str, Any] | None) -> None:
        """Delete *item* from the task store and the Vault filesystem."""
        if item is None:
            return
        classification: ClassificationResult = item["classification"]
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete '{classification.disguise_name}'?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._do_delete_items([item])

    # -------------------------------------------------------------------
    # Batch operations
    # -------------------------------------------------------------------

    def _batch_decrypt(self) -> None:
        """Decrypt selected items to a user-chosen directory."""
        selected = self._selected_items()
        if not selected:
            return
        if self.vault_manager is None:
            QMessageBox.warning(self, "Batch Decrypt", "No vault key configured.")
            return

        directory = QFileDialog.getExistingDirectory(self, "Choose Destination Directory")
        if not directory:
            return

        dest_dir = Path(directory)
        # The decrypt loop is synchronous and may take a moment; disable the
        # batch buttons and show progress text so the user gets feedback and
        # cannot trigger concurrent operations.
        self._batch_decrypt_btn.setEnabled(False)
        self._batch_delete_btn.setEnabled(False)
        self.preview_text.setPlainText(f"Decrypting {len(selected)} item(s) to {dest_dir}...")
        success = 0
        failures: list[str] = []

        try:
            for index, item in enumerate(selected, start=1):
                try:
                    classification: ClassificationResult = item["classification"]
                    name = classification.disguise_name
                    ext = classification.disguise_extension
                    dest = dest_dir / f"{name}{ext}"
                    # avoid overwrites
                    counter = 1
                    while dest.exists():
                        dest = dest_dir / f"{name}_{counter}{ext}"
                        counter += 1
                    self.vault_manager.decrypt(item["vault_path"], item["salt"], dest)
                    success += 1
                except Exception as exc:
                    name = item["classification"].disguise_name
                    failures.append(f"{name}: {exc}")
                self.preview_text.setPlainText(f"Decrypting {index}/{len(selected)}...")
        finally:
            self._batch_decrypt_btn.setEnabled(True)
            self._batch_delete_btn.setEnabled(True)

        msg = f"Decrypted {success} of {len(selected)} file(s)."
        if failures:
            msg += "\n\nFailures:\n" + "\n".join(failures[:5])
            if len(failures) > 5:
                msg += f"\n... and {len(failures) - 5} more"
        QMessageBox.information(self, "Batch Decrypt", msg)

    def _batch_delete(self) -> None:
        """Delete all selected items with confirmation."""
        selected = self._selected_items()
        if not selected:
            return
        reply = QMessageBox.question(
            self,
            "Confirm Batch Delete",
            f"Delete {len(selected)} selected item(s)?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._do_delete_items(selected)

    def _do_delete_items(self, items: list[dict[str, Any]]) -> None:
        """Delete a list of items from store and filesystem.

        Files are removed first; only if the file removal succeeds (or the
        file is already absent) is the store record deleted, so a filesystem
        failure leaves the index entry intact for retry. Errors are accumulated
        and surfaced in a single summary dialog instead of one alert per item.
        """
        errors: list[str] = []
        for item in items:
            try:
                # Remove the encrypted file first so a failure here leaves the
                # store record in place for the user to retry.
                item["vault_path"].unlink(missing_ok=True)
                if self.task_store is not None:
                    from uuid import UUID

                    self.task_store.delete(UUID(item["task_id"]))
            except Exception as exc:
                classification = item.get("classification")
                name = classification.disguise_name if classification else str(item["vault_path"])
                errors.append(f"{name}: {exc}")
                logging.getLogger(__name__).warning(
                    "Failed to delete %s: %s", item["vault_path"], exc
                )

        self._load_items()
        self._refresh_categories()
        self._refresh_table()
        self._refresh_grid()
        if errors:
            summary = "Some items could not be deleted:\n\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                summary += f"\n... and {len(errors) - 5} more"
            QMessageBox.warning(self, "Delete", summary)

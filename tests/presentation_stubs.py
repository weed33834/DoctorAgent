"""Headless PyQt6 stubs for presentation layer tests."""

# mypy: ignore-errors

# ruff: noqa: N802

from __future__ import annotations

import types
from typing import Any


class FakeSignal:
    """Stub Qt signal for headless tests."""

    def __init__(self) -> None:
        self.connected: list[object] = []

    def connect(self, callback: object) -> None:
        self.connected.append(callback)


class FakeAction:
    """Stub QAction for headless tests."""

    def __init__(self, text: str = "", parent: object | None = None) -> None:
        self.text = text
        self.parent = parent
        self.enabled = True
        self._checkable = False
        self._checked = False
        self.triggered = FakeSignal()

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setDefaultWidget(self, widget: object) -> None:
        self.widget = widget

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip

    def setCheckable(self, checkable: bool) -> None:
        self._checkable = checkable

    def setChecked(self, checked: bool) -> None:
        self._checked = checked

    def isChecked(self) -> bool:
        return self._checked


class FakeWidgetAction(FakeAction):
    """Stub QWidgetAction for headless tests."""

    def __init__(self, parent: object | None = None) -> None:
        super().__init__("", parent)


class FakeMenu:
    """Stub QMenu for headless tests."""

    def __init__(self, title: str = "") -> None:
        self.title = title
        self.actions: list[FakeAction | None | object] = []
        self.aboutToShow = FakeSignal()

    def addSection(self, text: str) -> FakeAction:
        action = FakeAction(text, self)
        self.actions.append(action)
        return action

    def addAction(self, action: FakeAction | str | None) -> FakeAction | None:
        if isinstance(action, str):
            fa = FakeAction(action, self)
            self.actions.append(fa)
            return fa
        self.actions.append(action)
        return None

    def addSeparator(self) -> None:
        self.actions.append(None)

    def addMenu(self, menu: object) -> None:
        self.actions.append(menu)

    def clear(self) -> None:
        self.actions.clear()

    def exec(self, _position: object = None) -> object:
        return None

    def emit_about_to_show(self) -> None:
        for callback in self.aboutToShow.connected:
            callback()  # type: ignore[operator]


class FakeProgressBar:
    """Stub progress bar for headless tests."""

    def __init__(self) -> None:
        self.value = 0
        self.format = ""
        self.range = (0, 0)
        self.text_visible = False

    def setRange(self, min_value: int, max_value: int) -> None:
        self.range = (min_value, max_value)

    def setValue(self, value: int) -> None:
        self.value = value

    def setFormat(self, fmt: str) -> None:
        self.format = fmt

    def setTextVisible(self, visible: bool) -> None:
        self.text_visible = visible


class _CallableStr(str):
    """String that can also be called like QLabel.text()."""

    def __call__(self) -> _CallableStr:
        return self


class FakeLabel:
    """Stub QLabel for headless tests."""

    def __init__(self, text: str = "") -> None:
        self._text = _CallableStr(text)
        self._pixmap: object | None = None

    @property
    def text(self) -> _CallableStr:
        return self._text

    def setText(self, text: str) -> None:
        self._text = _CallableStr(text)

    def setAlignment(self, _alignment: object) -> None:
        pass

    def setWordWrap(self, _enabled: bool) -> None:
        pass

    def setMinimumSize(self, _w: int, _h: int) -> None:
        pass

    def setPixmap(self, pixmap: object) -> None:
        self._pixmap = pixmap

    def setStyleSheet(self, _sheet: str) -> None:
        pass

    def clear(self) -> None:
        self._text = _CallableStr("")
        self._pixmap = None


class FakeSystemTrayIcon:
    """Stub QSystemTrayIcon for headless tests."""

    def __init__(self) -> None:
        self.menu: object | None = None
        self.visible = False
        self.tooltip = ""

    def setContextMenu(self, menu: object) -> None:
        self.menu = menu

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip


class FakeApplication:
    """Stub QApplication for headless tests."""

    _instance: FakeApplication | None = None

    def __init__(self, _args: object) -> None:
        if FakeApplication._instance is None:
            FakeApplication._instance = self
        self.quit_called = False

    def quit(self) -> None:
        self.quit_called = True

    def exec(self) -> int:
        return 0

    @classmethod
    def instance(cls) -> FakeApplication | None:
        return cls._instance


# --- Dialog stubs ---


class FakeDialog:
    """Stub QDialog for headless tests."""

    class DialogCode:
        Accepted = 1
        Rejected = 0

    def __init__(self, parent: object | None = None) -> None:
        self._parent = parent
        self._title = ""
        self._minimum_width = 0
        self._minimum_size: tuple[int, int] = (0, 0)
        self._exec_result = self.DialogCode.Accepted

    def setWindowTitle(self, title: str) -> None:
        self._title = title

    def setMinimumWidth(self, width: int) -> None:
        self._minimum_width = width

    def setMinimumSize(self, width: int, height: int) -> None:
        self._minimum_size = (width, height)

    def exec(self) -> int:
        if self._exec_result == self.DialogCode.Accepted:
            self.accept()
        return self._exec_result

    def accept(self) -> None:
        pass

    def reject(self) -> None:
        pass

    def style(self) -> FakeStyle:
        return FakeStyle()


class FakeDialogButtonBox:
    """Stub QDialogButtonBox for headless tests."""

    class StandardButton:
        Save = 1
        Cancel = 2

    def __init__(self, _buttons: object = None) -> None:
        self.accepted = FakeSignal()
        self.rejected = FakeSignal()


class FakeWizardPage:
    """Stub QWizardPage for headless tests."""

    def __init__(self) -> None:
        self._title = ""
        self._subtitle = ""
        self._layout: object | None = None
        self._wizard: FakeWizard | None = None
        self._pending_fields: list[tuple[str, object, str]] = []

    def setTitle(self, title: str) -> None:
        self._title = title

    def setSubTitle(self, subtitle: str) -> None:
        self._subtitle = subtitle

    def setLayout(self, layout: object) -> None:
        self._layout = layout

    def registerField(
        self,
        name: str,
        widget: object,
        property_name: str = "text",
        changed_signal: object | None = None,
    ) -> None:
        name_key = name.rstrip("*")
        wiz = self.wizard()
        if wiz is not None:
            wiz._register_field(name_key, widget, property_name)
        else:
            self._pending_fields.append((name_key, widget, property_name))

    def field(self, name: str) -> object:
        wiz = self.wizard()
        if wiz is not None:
            return wiz.field(name)
        return ""

    def setField(self, name: str, value: object) -> None:
        wiz = self.wizard()
        if wiz is not None:
            wiz.setField(name, value)

    def wizard(self) -> FakeWizard | None:
        return self._wizard

    def setWizard(self, wizard: FakeWizard) -> None:
        self._wizard = wizard
        # Flush any pending field registrations.
        for name_key, widget, prop in self._pending_fields:
            wizard._register_field(name_key, widget, prop)
        self._pending_fields.clear()

    def initializePage(self) -> None:
        pass

    def validatePage(self) -> bool:
        return True

    def isComplete(self) -> bool:
        return True


class FakeWizard(FakeDialog):
    """Stub QWizard for headless tests."""

    class WizardStyle:
        ModernStyle = 1
        ClassicStyle = 0

    class WizardButton:
        BackButton = 0
        NextButton = 1
        CommitButton = 2
        FinishButton = 3
        CancelButton = 4
        CustomButton1 = 5

    def __init__(self, parent: object | None = None) -> None:
        super().__init__(parent)
        self._pages: list[FakeWizardPage] = []
        self._current_id = 0
        self._fields: dict[str, object] = {}
        self._buttons: dict[int, object] = {}
        self._button_texts: dict[int, str] = {}

    def addPage(self, page: FakeWizardPage) -> int:
        page.setWizard(self)
        page_id = len(self._pages)
        self._pages.append(page)
        return page_id

    def setWizardStyle(self, style: int) -> None:
        pass

    def setButtonText(self, button: int, text: str) -> None:
        self._button_texts[button] = text

    def button(self, button: int) -> object | None:
        return self._buttons.get(button)

    def setButton(self, button: int, widget: object) -> None:
        self._buttons[button] = widget

    def nextId(self) -> int:
        return self._current_id + 1

    def currentId(self) -> int:
        return self._current_id

    def setCurrentId(self, page_id: int) -> None:
        self._current_id = page_id

    def _register_field(self, name: str, widget: object, property_name: str) -> None:
        self._fields[name] = widget

    def field(self, name: str) -> object:
        value = self._fields.get(name)
        if value is None:
            return ""
        if isinstance(value, FakeLineEdit):
            return value.text()
        return value

    def setField(self, name: str, value: object) -> None:
        widget = self._fields.get(name)
        if isinstance(widget, FakeLineEdit):
            widget.setText(str(value))
        self._fields[name] = value


class FakeMessageBox:
    """Stub QMessageBox for headless tests."""

    class StandardButton:
        Yes = 1
        No = 2

    _last_warning: tuple[object, str, str] | None = None
    _last_information: tuple[object, str, str] | None = None
    _last_question: tuple[object, str, str] | None = None
    _last_about: tuple[object, str, str] | None = None

    @classmethod
    def warning(cls, parent: object, title: str, text: str) -> None:
        cls._last_warning = (parent, title, text)

    @classmethod
    def information(cls, parent: object, title: str, text: str) -> None:
        cls._last_information = (parent, title, text)

    @classmethod
    def question(cls, parent: object, title: str, text: str) -> int:
        cls._last_question = (parent, title, text)
        return cls.StandardButton.Yes

    @classmethod
    def about(cls, parent: object, title: str, text: str) -> None:
        cls._last_about = (parent, title, text)


class FakeLineEdit:
    """Stub QLineEdit for headless tests."""

    class EchoMode:
        Password = 2
        Normal = 0

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._placeholder = ""
        self._echo_mode = self.EchoMode.Normal
        self._style_sheet = ""
        self.textChanged = FakeSignal()

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        for callback in self.textChanged.connected:
            callback(text)

    def setPlaceholderText(self, text: str) -> None:
        self._placeholder = text

    def setEchoMode(self, mode: int) -> None:
        self._echo_mode = mode

    def setStyleSheet(self, sheet: str) -> None:
        self._style_sheet = sheet


class FakeComboBox:
    """Stub QComboBox for headless tests."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._current = ""
        self._current_index = 0
        self._editable = False
        self._signals_blocked = False
        self.currentIndexChanged = FakeSignal()
        self.currentTextChanged = FakeSignal()

    def addItems(self, items: list[str]) -> None:
        self._items = list(items)
        if self._items and not self._current:
            self._current = self._items[0]
            self._current_index = 0

    def addItem(self, text: str) -> None:
        self._items.append(text)
        if not self._current:
            self._current = text
            self._current_index = 0

    def currentText(self) -> str:
        return self._current

    def setCurrentText(self, text: str) -> None:
        self._current = text
        if text in self._items:
            self._current_index = self._items.index(text)

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < len(self._items):
            self._current_index = index
            self._current = self._items[index]
            if not self._signals_blocked:
                for callback in self.currentIndexChanged.connected:
                    callback(index)

    def currentIndex(self) -> int:
        return self._current_index

    def itemText(self, index: int) -> str:
        if 0 <= index < len(self._items):
            return self._items[index]
        return ""

    def blockSignals(self, blocked: bool) -> None:
        self._signals_blocked = blocked

    def setEditable(self, editable: bool) -> None:
        self._editable = editable


class FakeCheckBox:
    """Stub QCheckBox for headless tests."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._checked = False
        self._tooltip = ""
        self._visible = True

    def setText(self, text: str) -> None:
        self._text = text

    def setToolTip(self, tooltip: str) -> None:
        self._tooltip = tooltip

    def setChecked(self, checked: bool) -> None:
        self._checked = checked

    def isChecked(self) -> bool:
        return self._checked

    def setVisible(self, visible: bool) -> None:
        self._visible = visible

    def isVisible(self) -> bool:
        return self._visible


class FakePushButton:
    """Stub QPushButton for headless tests."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.clicked = FakeSignal()
        self._enabled = True
        self._tooltip = ""

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def isEnabled(self) -> bool:
        return self._enabled

    def setToolTip(self, tooltip: str) -> None:
        self._tooltip = tooltip


class FakeTableWidgetItem:
    """Stub QTableWidgetItem for headless tests."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._row = -1
        self._column = -1
        self._data: dict[int, object] = {}

    def text(self) -> str:
        return self._text

    def row(self) -> int:
        return self._row

    def column(self) -> int:
        return self._column

    def setData(self, role: int, value: object) -> None:
        self._data[role] = value

    def data(self, role: int) -> object:
        return self._data.get(role)


class FakeTableWidget:
    """Stub QTableWidget for headless tests."""

    def __init__(self) -> None:
        self._column_count = 0
        self._horizontal_labels: list[str] = []
        self._row_count = 0
        self._items: dict[tuple[int, int], FakeTableWidgetItem] = {}
        self._selection_behavior = 0
        self._selection_mode = 0
        self._edit_triggers = 0
        self._selected: list[FakeTableWidgetItem] = []
        self._header = FakeHeaderView()
        self._context_menu_policy = 0
        self.cellDoubleClicked = FakeSignal()
        self.itemSelectionChanged = FakeSignal()
        self.customContextMenuRequested = FakeSignal()

    def viewport(self) -> FakeTableWidget:
        return self

    def mapToGlobal(self, _position: object) -> object:
        return None

    def setContextMenuPolicy(self, policy: int) -> None:
        self._context_menu_policy = policy

    def setColumnCount(self, count: int) -> None:
        self._column_count = count

    def setHorizontalHeaderLabels(self, labels: list[str]) -> None:
        self._horizontal_labels = labels

    def horizontalHeader(self) -> FakeHeaderView:
        return self._header

    def setSelectionBehavior(self, behavior: int) -> None:
        self._selection_behavior = behavior

    def setSelectionMode(self, mode: int) -> None:
        self._selection_mode = mode

    def setEditTriggers(self, triggers: int) -> None:
        self._edit_triggers = triggers

    def setRowCount(self, count: int) -> None:
        self._row_count = count

    def setItem(self, row: int, column: int, item: FakeTableWidgetItem) -> None:
        item._row = row
        item._column = column
        self._items[(row, column)] = item

    def item(self, row: int, column: int) -> FakeTableWidgetItem | None:
        return self._items.get((row, column))

    def row(self, item: FakeTableWidgetItem) -> int:
        return item._row

    def selectedItems(self) -> list[FakeTableWidgetItem]:
        return list(self._selected)

    def select_row(self, row: int) -> None:
        """Test helper to select a whole row."""
        self._selected = [item for (r, c), item in self._items.items() if r == row]

    def emit_cell_double_clicked(self, row: int, column: int) -> None:
        """Emit the cellDoubleClicked signal to connected handlers."""
        for callback in self.cellDoubleClicked.connected:
            callback(row, column)

    def emit_item_selection_changed(self) -> None:
        """Emit the itemSelectionChanged signal to connected handlers."""
        for callback in self.itemSelectionChanged.connected:
            callback()


class FakeHeaderView:
    """Stub QHeaderView for headless tests."""

    class ResizeMode:
        Stretch = 1

    def __init__(self) -> None:
        self.sectionClicked = FakeSignal()

    def setSectionResizeMode(self, _mode: int) -> None:
        pass


class FakeStyle:
    """Stub QStyle for headless tests."""

    class StandardPixmap:
        SP_FileIcon = 0
        SP_DirIcon = 1
        SP_ComputerIcon = 2
        SP_DriveHDIcon = 3
        SP_FileDialogDetailedView = 4
        SP_FileDialogInfoView = 5

    def standardIcon(self, _pixmap: int = 0) -> FakeIcon:
        return FakeIcon()


class FakeAbstractItemView:
    """Stub QAbstractItemView for headless tests."""

    class SelectionBehavior:
        SelectRows = 1

    class SelectionMode:
        SingleSelection = 0
        ExtendedSelection = 1

    class EditTrigger:
        NoEditTriggers = 0


class FakeFormLayout:
    """Stub QFormLayout for headless tests."""

    def __init__(self, _parent: object | None = None) -> None:
        self._rows: list[tuple[str | None, object]] = []

    def addRow(self, label: object, widget: object | None = None) -> None:
        if widget is None:
            self._rows.append((None, label))
        else:
            self._rows.append((str(label), widget))

    def setContentsMargins(self, *_args: object) -> None:
        pass


class FakeVBoxLayout:
    """Stub QVBoxLayout for headless tests."""

    def __init__(self, _parent: object | None = None) -> None:
        self._widgets: list[object] = []
        self._layouts: list[object] = []

    def addWidget(self, widget: object, *_args: object, **_kwargs: object) -> None:
        self._widgets.append(widget)

    def addLayout(self, layout: object) -> None:
        self._layouts.append(layout)

    def addStretch(self) -> None:
        pass

    def setContentsMargins(self, *_margins: int) -> None:
        pass


class FakeHBoxLayout(FakeVBoxLayout):
    """Stub QHBoxLayout for headless tests."""


class FakeGridLayout:
    """Stub QGridLayout for headless tests."""

    def __init__(self, _parent: object | None = None) -> None:
        self._widgets: list[tuple[object, int, int, int, int]] = []

    def addWidget(
        self,
        widget: object,
        row: int,
        col: int,
        row_span: int = 1,
        col_span: int = 1,
    ) -> None:
        self._widgets.append((widget, row, col, row_span, col_span))

    def setContentsMargins(self, *_args: object) -> None:
        pass


class FakeGroupBox:
    """Stub QGroupBox for headless tests."""

    def __init__(self, title: str = "") -> None:
        self.title = title
        self._layout: object | None = None

    def setLayout(self, layout: object) -> None:
        self._layout = layout


class FakeFileDialog:
    """Stub QFileDialog for headless tests."""

    next_directory: str = ""
    next_open_file: str = ""
    next_save_file: str = ""

    class AcceptMode:
        AcceptSave = 1
        AcceptOpen = 0

    class FileMode:
        AnyFile = 0
        ExistingFile = 1
        Directory = 2

    @classmethod
    def getExistingDirectory(
        cls, _parent: object | None = None, _caption: str = "", _directory: str = ""
    ) -> str:
        return cls.next_directory

    @classmethod
    def getOpenFileName(
        cls,
        _parent: object | None = None,
        _caption: str = "",
        _directory: str = "",
        _filter: str = "",
    ) -> tuple[str, str]:
        return (cls.next_open_file, "")

    @classmethod
    def getSaveFileName(
        cls,
        _parent: object | None = None,
        _caption: str = "",
        _directory: str = "",
        _filter: str = "",
    ) -> tuple[str, str]:
        return (cls.next_save_file, "")


class FakeTreeWidgetItem:
    """Stub QTreeWidgetItem for headless tests."""

    def __init__(self, _parent: object | None = None, texts: list[str] | None = None) -> None:
        self._texts = list(texts or [""])

    def text(self, column: int) -> str:
        if column < 0 or column >= len(self._texts):
            return ""
        return self._texts[column]


class FakeTreeWidget:
    """Stub QTreeWidget for headless tests."""

    def __init__(self) -> None:
        self._header = ""
        self._items: list[FakeTreeWidgetItem] = []
        self._current_item: FakeTreeWidgetItem | None = None
        self.currentItemChanged = FakeSignal()

    def clear(self) -> None:
        self._items.clear()

    def setHeaderLabel(self, label: str) -> None:
        self._header = label

    def addTopLevelItem(self, item: FakeTreeWidgetItem) -> None:
        self._items.append(item)

    def setCurrentItem(self, item: FakeTreeWidgetItem | None) -> None:
        self._current_item = item

    def currentItem(self) -> FakeTreeWidgetItem | None:
        return self._current_item

    def emit_current_item_changed(self, current: FakeTreeWidgetItem | None) -> None:
        """Emit the currentItemChanged signal to connected handlers."""
        for callback in self.currentItemChanged.connected:
            callback(current, self._current_item)


class FakeStackedWidget:
    """Stub QStackedWidget for headless tests."""

    def __init__(self, _parent: object | None = None) -> None:
        self._widgets: list[object] = []
        self._current_index = 0

    def addWidget(self, widget: object) -> int:
        self._widgets.append(widget)
        return len(self._widgets) - 1

    def setCurrentIndex(self, index: int) -> None:
        self._current_index = index

    def currentIndex(self) -> int:
        return self._current_index


class FakeListWidgetItem:
    """Stub QListWidgetItem for headless tests."""

    def __init__(self, icon_or_text: object = "", parent_or_text: object | None = None) -> None:
        # Support both QListWidgetItem(str) and QListWidgetItem(QIcon, str)
        if isinstance(icon_or_text, str):
            self._text = icon_or_text
            self._icon: object | None = None
        else:
            self._icon = icon_or_text
            self._text = parent_or_text if isinstance(parent_or_text, str) else ""
        self._size_hint: tuple[int, int] | None = None
        self._data: dict[int, object] = {}

    def setIcon(self, icon: object) -> None:
        self._icon = icon

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text

    def setSizeHint(self, size: object) -> None:
        self._size_hint = (size.width(), size.height())

    def setData(self, role: int, value: object) -> None:
        self._data[role] = value

    def data(self, role: int) -> object:
        return self._data.get(role)


class FakeListWidget:
    """Stub QListWidget for headless tests."""

    class ViewMode:
        IconMode = 0
        ListMode = 1

    class ResizeMode:
        Adjust = 0
        Fixed = 1

    class Movement:
        Static = 0
        Free = 1

    def __init__(self, _parent: object | None = None) -> None:
        self._items: list[FakeListWidgetItem] = []
        self._selected: list[FakeListWidgetItem] = []
        self.itemDoubleClicked = FakeSignal()
        self.itemSelectionChanged = FakeSignal()
        self.customContextMenuRequested = FakeSignal()

    def viewport(self) -> FakeListWidget:
        return self

    def mapToGlobal(self, _position: object) -> object:
        return None

    def clear(self) -> None:
        self._items.clear()

    def addItem(self, item: FakeListWidgetItem) -> None:
        self._items.append(item)

    def setIconSize(self, _size: object) -> None:
        pass

    def setViewMode(self, _mode: object) -> None:
        pass

    def setResizeMode(self, _mode: object) -> None:
        pass

    def setMovement(self, _mode: object) -> None:
        pass

    def setSelectionMode(self, _mode: object) -> None:
        pass

    def setContextMenuPolicy(self, _policy: object) -> None:
        pass

    def selectedItems(self) -> list[FakeListWidgetItem]:
        return list(self._selected)

    def select_item(self, item: FakeListWidgetItem) -> None:
        """Test helper to select an item."""
        self._selected = [item]

    def select_row(self, row: int) -> None:
        """Test helper to select an item by row index."""
        if 0 <= row < len(self._items):
            self._selected = [self._items[row]]

    def count(self) -> int:
        return len(self._items)

    def item(self, row: int) -> FakeListWidgetItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def currentRow(self) -> int:
        if self._selected and self._selected[0] in self._items:
            return self._items.index(self._selected[0])
        return -1

    def setCurrentRow(self, row: int) -> None:
        if 0 <= row < len(self._items):
            self._selected = [self._items[row]]

    def setItemSelected(self, item: FakeListWidgetItem, selected: bool) -> None:
        if selected:
            if item not in self._selected:
                self._selected.append(item)
        else:
            if item in self._selected:
                self._selected.remove(item)


class FakeToolBar:
    """Stub QToolBar for headless tests."""

    def __init__(self, _title: str = "") -> None:
        self._actions: list[object] = []
        self._movable = False

    def addAction(self, icon_or_text: object, text: str | None = None) -> FakeAction:
        if text is not None:
            action = FakeAction(text)
        else:
            action = FakeAction(str(icon_or_text) if isinstance(icon_or_text, str) else "")
        self._actions.append(action)
        return action

    def addWidget(self, widget: object) -> None:
        self._actions.append(widget)

    def addSeparator(self) -> None:
        self._actions.append(None)

    def setMovable(self, movable: bool) -> None:
        self._movable = movable

    def setIconSize(self, _size: object) -> None:
        pass


class FakeScrollArea:
    """Stub QScrollArea for headless tests."""

    def __init__(self, _parent: object | None = None) -> None:
        self._widget: object | None = None

    def setWidget(self, widget: object) -> None:
        self._widget = widget

    def setWidgetResizable(self, _resizable: bool) -> None:
        pass


class FakeSplitter:
    """Stub QSplitter for headless tests."""

    def __init__(self, _orientation: object) -> None:
        self._widgets: list[object] = []

    def addWidget(self, widget: object) -> None:
        self._widgets.append(widget)

    def setStretchFactor(self, _index: int, _stretch: int) -> None:
        pass


class FakeTextEdit:
    """Stub QTextEdit for headless tests."""

    def __init__(self) -> None:
        self._text = ""
        self._read_only = False
        self._placeholder = ""

    def setPlainText(self, text: str) -> None:
        self._text = text

    def setReadOnly(self, read_only: bool) -> None:
        self._read_only = read_only

    def setMaximumHeight(self, _height: int) -> None:
        pass

    def setPlaceholderText(self, text: str) -> None:
        self._placeholder = text

    def toPlainText(self) -> str:
        return self._text

    def clear(self) -> None:
        self._text = ""


class FakeUrl:
    """Stub QUrl for headless tests."""

    opened_urls: list[str] = []

    def __init__(self, url: str = "") -> None:
        self._url = url

    def toString(self) -> str:
        return self._url


class FakeIcon:
    """Stub QIcon for headless tests."""

    @classmethod
    def fromTheme(cls, _name: str, _fallback: object | None = None) -> FakeIcon:
        return cls()

    def isNull(self) -> bool:
        return True


class FakePixmap:
    """Stub QPixmap for headless tests."""

    def __init__(self, _path: str = "") -> None:
        pass

    def isNull(self) -> bool:
        return True

    def scaled(self, _w: int, _h: int, *_args: object, **_kwargs: object) -> FakePixmap:
        return self


class FakeDesktopServices:
    """Stub QDesktopServices for headless tests."""

    opened_urls: list[FakeUrl] = []

    @classmethod
    def openUrl(cls, url: FakeUrl) -> bool:
        cls.opened_urls.append(url)
        return True


class FakeQt:
    """Stub Qt namespace for headless tests."""

    class AlignmentFlag:
        AlignLeft = 1
        AlignCenter = 4

    class Orientation:
        Horizontal = 1
        Vertical = 2

    class ContextMenuPolicy:
        CustomContextMenu = 3

    class SortOrder:
        AscendingOrder = 0
        DescendingOrder = 1

    class ItemDataRole:
        UserRole = 256

    class AspectRatioMode:
        KeepAspectRatio = 1

    class TransformationMode:
        SmoothTransformation = 1

    class ItemFlag:
        ItemIsSelectable = 1
        ItemIsEnabled = 32


class FakeQTimer:
    """Stub QTimer for headless tests."""

    fired_callbacks: list[object] = []

    def __init__(self, parent: object | None = None) -> None:
        self._parent = parent
        self._single_shot = False
        self.timeout = FakeSignal()

    def setSingleShot(self, value: bool) -> None:
        self._single_shot = value

    def start(self, _msec: int) -> None:
        pass

    def stop(self) -> None:
        pass

    @staticmethod
    def singleShot(msec: int, callback: object) -> None:
        FakeQTimer.fired_callbacks.append(callback)


class FakeQCloseEvent:
    """Stub QCloseEvent for headless tests."""

    def __init__(self) -> None:
        self._accepted = False

    def accept(self) -> None:
        self._accepted = True

    def ignore(self) -> None:
        self._accepted = False

    def isAccepted(self) -> bool:
        return self._accepted


class FakeQSize:
    """Stub QSize for headless tests."""

    def __init__(self, w: int = 0, h: int = 0) -> None:
        self._w = w
        self._h = h

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h


class FakeInputDialog:
    """Stub QInputDialog for headless tests.

    ``getText`` is a static method that tests can monkeypatch to control
    the return value.
    """

    @staticmethod
    def getText(*_args: object, **_kwargs: object) -> tuple[str, bool]:
        return ("", False)

    @staticmethod
    def getMultiLineText(*_args: object, **_kwargs: object) -> tuple[str, bool]:
        return ("", False)


class FakeDateTime:
    """Stub QDateTime for headless tests."""

    def __init__(self, *args: object) -> None:
        self._args = args

    @staticmethod
    def currentDateTime() -> "FakeDateTime":
        return FakeDateTime()

    @staticmethod
    def fromString(text: str, _format: str = "") -> "FakeDateTime":
        return FakeDateTime(text)

    def toString(self, _format: str = "") -> str:
        if self._args and isinstance(self._args[0], str):
            return self._args[0]
        return ""

    def toPython(self) -> Any:
        from datetime import datetime

        return datetime.now()


class FakeDateTimeEdit:
    """Stub QDateTimeEdit for headless tests."""

    def __init__(self, value: object = None) -> None:
        self._datetime = value if value is not None else FakeDateTime()
        self._display_format = ""
        self._calendar_popup = False
        self._minimum: object | None = None
        self._maximum: object | None = None
        self.dateTimeChanged = FakeSignal()

    def setDateTime(self, value: object) -> None:
        self._datetime = value

    def dateTime(self) -> object:
        return self._datetime

    def setDisplayFormat(self, fmt: str) -> None:
        self._display_format = fmt

    def setCalendarPopup(self, enabled: bool) -> None:
        self._calendar_popup = enabled

    def setMinimumDateTime(self, value: object) -> None:
        self._minimum = value

    def setMaximumDateTime(self, value: object) -> None:
        self._maximum = value


class FakeSpinBox:
    """Stub QSpinBox for headless tests."""

    def __init__(self) -> None:
        self._value = 0
        self._minimum = 0
        self._maximum = 99
        self._suffix = ""
        self._single_step = 1
        self.valueChanged = FakeSignal()

    def setValue(self, value: int) -> None:
        self._value = value

    def value(self) -> int:
        return self._value

    def setMinimum(self, value: int) -> None:
        self._minimum = value

    def setMaximum(self, value: int) -> None:
        self._maximum = value

    def setRange(self, minimum: int, maximum: int) -> None:
        self._minimum = minimum
        self._maximum = maximum

    def setSuffix(self, suffix: str) -> None:
        self._suffix = suffix

    def setSingleStep(self, step: int) -> None:
        self._single_step = step


class FakeDoubleSpinBox:
    """Stub QDoubleSpinBox for headless tests."""

    def __init__(self) -> None:
        self._value: float = 0.0
        self._minimum = 0.0
        self._maximum = 99.99
        self._decimals = 2
        self._single_step = 0.1
        self._suffix = ""
        self.valueChanged = FakeSignal()

    def setValue(self, value: float) -> None:
        self._value = value

    def value(self) -> float:
        return self._value

    def setMinimum(self, value: float) -> None:
        self._minimum = value

    def setMaximum(self, value: float) -> None:
        self._maximum = value

    def setRange(self, minimum: float, maximum: float) -> None:
        self._minimum = minimum
        self._maximum = maximum

    def setDecimals(self, decimals: int) -> None:
        self._decimals = decimals

    def setSingleStep(self, step: float) -> None:
        self._single_step = step

    def setSuffix(self, suffix: str) -> None:
        self._suffix = suffix


def install_presentation_stubs() -> dict[str, Any]:
    """Install fake PyQt6 modules and return the saved originals."""
    import sys

    saved_modules: dict[str, Any] = {
        "PyQt6": sys.modules.get("PyQt6"),
        "PyQt6.QtCore": sys.modules.get("PyQt6.QtCore"),
        "PyQt6.QtGui": sys.modules.get("PyQt6.QtGui"),
        "PyQt6.QtWidgets": sys.modules.get("PyQt6.QtWidgets"),
    }

    fake_qt = types.ModuleType("PyQt6")
    fake_qt_gui = types.ModuleType("PyQt6.QtGui")
    fake_qt_widgets = types.ModuleType("PyQt6.QtWidgets")
    fake_qt_core = types.ModuleType("PyQt6.QtCore")

    fake_qt_gui.QAction = FakeAction
    fake_qt_gui.QCloseEvent = FakeQCloseEvent
    fake_qt_gui.QDesktopServices = FakeDesktopServices
    fake_qt_gui.QIcon = FakeIcon
    fake_qt_gui.QPixmap = FakePixmap

    fake_qt_core.QSize = FakeQSize
    fake_qt_core.Qt = FakeQt
    fake_qt_core.QTimer = FakeQTimer
    fake_qt_core.QUrl = FakeUrl
    fake_qt_core.QDateTime = FakeDateTime

    fake_qt_widgets.QAbstractItemView = FakeAbstractItemView
    fake_qt_widgets.QApplication = FakeApplication
    fake_qt_widgets.QCheckBox = FakeCheckBox
    fake_qt_widgets.QComboBox = FakeComboBox
    fake_qt_widgets.QDateTimeEdit = FakeDateTimeEdit
    fake_qt_widgets.QDialog = FakeDialog
    fake_qt_widgets.QDialogButtonBox = FakeDialogButtonBox
    fake_qt_widgets.QDoubleSpinBox = FakeDoubleSpinBox
    fake_qt_widgets.QFileDialog = FakeFileDialog
    fake_qt_widgets.QFormLayout = FakeFormLayout
    fake_qt_widgets.QGroupBox = FakeGroupBox
    fake_qt_widgets.QHBoxLayout = FakeHBoxLayout
    fake_qt_widgets.QGridLayout = FakeGridLayout
    fake_qt_widgets.QHeaderView = FakeHeaderView
    fake_qt_widgets.QInputDialog = FakeInputDialog
    fake_qt_widgets.QLabel = FakeLabel
    fake_qt_widgets.QLineEdit = FakeLineEdit
    fake_qt_widgets.QMenu = FakeMenu
    fake_qt_widgets.QMessageBox = FakeMessageBox
    fake_qt_widgets.QProgressBar = FakeProgressBar
    fake_qt_widgets.QPushButton = FakePushButton
    fake_qt_widgets.QSpinBox = FakeSpinBox
    fake_qt_widgets.QSplitter = FakeSplitter
    fake_qt_widgets.QStyle = FakeStyle
    fake_qt_widgets.QSystemTrayIcon = FakeSystemTrayIcon
    fake_qt_widgets.QTableWidget = FakeTableWidget
    fake_qt_widgets.QTableWidgetItem = FakeTableWidgetItem
    fake_qt_widgets.QTextEdit = FakeTextEdit
    fake_qt_widgets.QTreeWidget = FakeTreeWidget
    fake_qt_widgets.QTreeWidgetItem = FakeTreeWidgetItem
    fake_qt_widgets.QVBoxLayout = FakeVBoxLayout
    fake_qt_widgets.QWidget = object
    fake_qt_widgets.QWidgetAction = FakeWidgetAction
    fake_qt_widgets.QStackedWidget = FakeStackedWidget
    fake_qt_widgets.QListWidget = FakeListWidget
    fake_qt_widgets.QListWidgetItem = FakeListWidgetItem
    fake_qt_widgets.QToolBar = FakeToolBar
    fake_qt_widgets.QScrollArea = FakeScrollArea
    fake_qt_widgets.QWizard = FakeWizard
    fake_qt_widgets.QWizardPage = FakeWizardPage

    sys.modules["PyQt6"] = fake_qt
    sys.modules["PyQt6.QtCore"] = fake_qt_core
    sys.modules["PyQt6.QtGui"] = fake_qt_gui
    sys.modules["PyQt6.QtWidgets"] = fake_qt_widgets

    return saved_modules


def restore_modules(saved_modules: dict[str, Any]) -> None:
    """Restore the original PyQt6 modules and clear submodule caches."""
    import sys

    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    sys.modules.pop("doctoragent.presentation.tray", None)
    sys.modules.pop("doctoragent.presentation.connection_dialog", None)
    sys.modules.pop("doctoragent.presentation.settings_dialog", None)
    sys.modules.pop("doctoragent.presentation.vault_browser", None)
    sys.modules.pop("doctoragent.presentation.first_run_wizard", None)
    sys.modules.pop("doctoragent.presentation.audit_viewer", None)
    sys.modules.pop("doctoragent.presentation.backup_dialog", None)
    sys.modules.pop("doctoragent.presentation.key_management_dialog", None)
    sys.modules.pop("doctoragent.presentation.skill_manager", None)
    sys.modules.pop("doctoragent.presentation.agent_config_dialog", None)
    sys.modules.pop("doctoragent.presentation.monitor_panel", None)
    # Force re-import of presentation submodules on the next test by clearing
    # the package-level cache as well.
    presentation = sys.modules.get("doctoragent.presentation")
    if presentation is not None:
        presentation.__dict__.pop("tray", None)
        presentation.__dict__.pop("connection_dialog", None)
        presentation.__dict__.pop("settings_dialog", None)
        presentation.__dict__.pop("vault_browser", None)
        presentation.__dict__.pop("first_run_wizard", None)
        presentation.__dict__.pop("audit_viewer", None)
        presentation.__dict__.pop("backup_dialog", None)
        presentation.__dict__.pop("key_management_dialog", None)
        presentation.__dict__.pop("skill_manager", None)
        presentation.__dict__.pop("agent_config_dialog", None)
        presentation.__dict__.pop("monitor_panel", None)

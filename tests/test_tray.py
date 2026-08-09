"""Tests for the system tray application."""

# mypy: ignore-errors

# ruff: noqa: N802

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.config import AegisConfig
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore

from .presentation_stubs import (
    FakeAction,
    FakeApplication,
    FakeDesktopServices,
    FakeMenu,
    FakeMessageBox,
    install_presentation_stubs,
    restore_modules,
)

pytestmark = pytest.mark.gui


@pytest.fixture
def qt_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace PyQt6 widgets with stubs so tests run without a display."""
    saved = install_presentation_stubs()
    FakeApplication._instance = None
    yield
    FakeApplication._instance = None
    restore_modules(saved)


@pytest.fixture
def config(tmp_path: Path) -> AegisConfig:
    """Test configuration with isolated paths."""
    cfg = AegisConfig()
    cfg.paths.index = tmp_path / "Index"
    cfg.paths.connections = tmp_path / "connections.json"
    cfg.paths.inbox = tmp_path / "Inbox"
    cfg.paths.vault = tmp_path / "Vault"
    return cfg


def _menu_texts(menu: FakeMenu) -> list[str]:
    """Collect text from actions and nested menus in a FakeMenu."""
    texts: list[str] = []
    for action in menu.actions:
        if action is None:
            continue
        if isinstance(action, FakeMenu):
            texts.append(action.title)
            texts.extend(_menu_texts(action))
        elif isinstance(action, FakeAction):
            texts.append(action.text)
    return texts


def _find_action(menu: FakeMenu, predicate: Callable[[FakeAction], bool]) -> FakeAction | None:
    """Find the first action matching predicate in the menu."""
    for action in menu.actions:
        if isinstance(action, FakeAction) and predicate(action):
            return action
    return None


def _find_nested_menu(menu: FakeMenu, title: str) -> FakeMenu | None:
    """Find a nested FakeMenu by title."""
    for action in menu.actions:
        if isinstance(action, FakeMenu) and action.title == title:
            return action
    return None


def test_tray_header_is_present(qt_stubs: None, config: AegisConfig) -> None:
    """Tray initializes a header label with app name, version and status summary."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray._refresh_header()

    assert isinstance(tray._header_label, object)
    assert "DoctorAgent" in tray._header_label.text
    # Version is read from doctoragent.__version__; just assert a v-prefixed
    # version is present rather than hard-coding the literal (which broke
    # every release bump).
    from doctoragent import __version__

    assert f"v{__version__}" in tray._header_label.text
    assert "完成" in tray._header_label.text


def test_tray_quick_actions_are_present(qt_stubs: None, config: AegisConfig) -> None:
    """Tray menu exposes quick-entry actions with icons."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray._add_quick_actions(tray.menu)

    texts = _menu_texts(tray.menu)
    assert "📥 Open Inbox" in texts
    assert "🔐 Open Vault" in texts
    assert "🔍 Search Vault..." in texts
    assert "📊 Dashboard" in texts
    assert "🔔 Notifications (0)" in texts
    assert any("Activity" in text for text in texts)

    notifications = _find_action(tray.menu, lambda a: "Notifications" in a.text)
    assert notifications is not None
    assert notifications.enabled is False


def test_tray_connections_submenu_exists(qt_stubs: None, config: AegisConfig) -> None:
    """Tray has a Connections submenu listing enabled connections."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray._build_connections_menu()
    tray.menu.addMenu(tray.connections_menu)

    connections_menu = _find_nested_menu(tray.menu, "Connections")
    assert connections_menu is not None
    texts = _menu_texts(connections_menu)
    assert any("Local Ollama" in text for text in texts)
    assert any("Manage Connections..." in text for text in texts)


def test_tray_without_config_shows_not_configured(qt_stubs: None) -> None:
    """Tray shows a placeholder when no task store is configured."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication()
    tray._build_tasks_menu()

    assert tray.task_store is None
    texts = [a.text for a in tray.tasks_menu.actions if a is not None]
    assert any("not configured" in text for text in texts)
    assert any("Refresh" in text for text in texts)


def test_tray_builds_tasks_menu_with_progress(qt_stubs: None, config: AegisConfig) -> None:
    """Tray renders task sections and an overall progress bar."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    task_id = uuid4()
    store.create(task_id, Path("/tmp/file.txt"))
    store.update_state(task_id, TaskState.ENCRYPTING, "working")

    tray = TrayApplication(config=config)
    tray._build_tasks_menu()

    assert tray.task_store is not None
    assert tray._tasks_progress_bar.range == (0, 100)
    assert tray._tasks_progress_bar.value == 0
    assert "完成 0/1" in tray._tasks_progress_bar.format

    texts = _menu_texts(tray.tasks_menu)
    assert any("进行中" in text for text in texts)
    assert any(str(task_id)[:8] in text for text in texts)
    assert any("加密中" in text for text in texts)
    assert any("最近完成" in text for text in texts)
    assert any("需关注" in text for text in texts)
    assert any("打开任务中心..." in text for text in texts)
    assert any("Refresh" in text for text in texts)


def test_tray_progress_reflects_completed_tasks(qt_stubs: None, config: AegisConfig) -> None:
    """Progress bar shows completed / total ratio."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    completed_id = uuid4()
    pending_id = uuid4()
    store.create(completed_id, Path("/tmp/done.txt"))
    store.update_state(completed_id, TaskState.COMPLETED)
    store.create(pending_id, Path("/tmp/pending.txt"))

    tray = TrayApplication(config=config)
    tray._build_tasks_menu()

    assert tray._tasks_progress_bar.value == 50
    assert "完成 1/2" in tray._tasks_progress_bar.format


def test_tray_menu_refreshes_on_about_to_show(qt_stubs: None, config: AegisConfig) -> None:
    """Menu refreshes when aboutToShow fires, reflecting state changes."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    task_id = uuid4()
    store.create(task_id, Path("/tmp/file.txt"))

    tray = TrayApplication(config=config)
    tray._build_tasks_menu()

    store.update_state(task_id, TaskState.COMPLETED)
    assert tray._tasks_progress_bar.value == 0

    tray.tasks_menu.emit_about_to_show()

    assert tray._tasks_progress_bar.value == 100
    assert "完成 1/1" in tray._tasks_progress_bar.format


def test_tray_refresh_action_is_present(qt_stubs: None, config: AegisConfig) -> None:
    """A Refresh action is available in the tasks menu."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray._build_tasks_menu()

    refresh_actions = [
        a
        for a in tray.tasks_menu.actions
        if a is not None and isinstance(a, FakeAction) and "Refresh" in a.text
    ]
    assert len(refresh_actions) == 1
    assert len(refresh_actions[0].triggered.connected) == 1


def test_tray_task_center_placeholder_is_present(qt_stubs: None, config: AegisConfig) -> None:
    """A task center placeholder action is present in the tasks menu."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray._build_tasks_menu()

    task_center = _find_action(tray.tasks_menu, lambda a: "打开任务中心" in a.text)
    assert task_center is not None
    assert len(task_center.triggered.connected) == 1


def test_tray_attention_section_shows_failed_and_quarantined(
    qt_stubs: None, config: AegisConfig
) -> None:
    """The attention section surfaces FAILED and QUARANTINED tasks."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    failed_id = uuid4()
    quarantined_id = uuid4()
    store.create(failed_id, Path("/tmp/failed.txt"))
    store.update_state(failed_id, TaskState.FAILED)
    store.create(quarantined_id, Path("/tmp/bad.txt"))
    store.update_state(quarantined_id, TaskState.QUARANTINED)

    tray = TrayApplication(config=config)
    tray._build_tasks_menu()

    texts = _menu_texts(tray.tasks_menu)
    assert any(str(failed_id)[:8] in text for text in texts)
    assert any(str(quarantined_id)[:8] in text for text in texts)
    assert any("失败" in text for text in texts)
    assert any("已隔离" in text for text in texts)


def test_tray_status_summary_without_task_store(qt_stubs: None) -> None:
    """Status summary works when no task store is configured."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication()
    summary = tray._status_summary()

    assert "未配置本地连接" in summary or "本地连接正常" in summary
    assert "📦" in summary
    assert "完成 0" in summary


def test_tray_status_summary_with_failed_and_quarantined(
    qt_stubs: None, config: AegisConfig
) -> None:
    """Status summary reports failed and quarantined counts."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    failed_id = uuid4()
    quarantined_id = uuid4()
    store.create(failed_id, Path("/tmp/failed.txt"))
    store.update_state(failed_id, TaskState.FAILED)
    store.create(quarantined_id, Path("/tmp/bad.txt"))
    store.update_state(quarantined_id, TaskState.QUARANTINED)

    tray = TrayApplication(config=config)
    summary = tray._status_summary()

    assert "失败 1" in summary
    assert "隔离 1" in summary


def test_tray_vault_size_text(qt_stubs: None, config: AegisConfig) -> None:
    """Vault size is calculated from files in the vault directory."""
    from doctoragent.presentation.tray import TrayApplication

    config.paths.vault.mkdir(parents=True, exist_ok=True)
    (config.paths.vault / "data.bin").write_bytes(b"x" * 1500)

    tray = TrayApplication(config=config)
    size_text = tray._vault_size_text()

    assert "KB" in size_text or "B" in size_text


def test_tray_no_enabled_connections_shows_placeholder(qt_stubs: None, config: AegisConfig) -> None:
    """Connections menu shows a placeholder when no connections are enabled."""
    from doctoragent.presentation.tray import TrayApplication

    # Use a fresh empty connections file so the default Ollama connection is not seeded.
    empty_path = config.paths.connections.parent / "empty_connections.json"
    empty_path.write_text('{"version": 1, "connections": []}')
    tray = TrayApplication(connections_path=empty_path, config=config)

    # Disable any seeded connections.
    for conn in tray.connection_manager.list_all():
        conn.is_enabled = False
        tray.connection_manager.update(conn)

    tray._refresh_connections_menu()
    texts = [a.text for a in tray.connections_menu.actions if a is not None]
    assert any("No connections enabled" in text for text in texts)


def test_tray_remote_connection_marked_unverified(qt_stubs: None, config: AegisConfig) -> None:
    """A remote enabled connection is labelled as unverified."""
    from doctoragent.presentation.tray import TrayApplication

    from doctoragent.connections.models import AuthMethod, Connection, PlatformType

    tray = TrayApplication(config=config)
    # Remove any seeded connections and add a remote one.
    for conn in list(tray.connection_manager.list_all()):
        tray.connection_manager.delete(conn.id)
    remote = Connection(
        name="Remote API",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url="https://example.com/v1",
        auth_method=AuthMethod.BEARER,
        api_key="secret",
        is_local=False,
        is_enabled=True,
    )
    tray.connection_manager.add(remote)

    tray._refresh_connections_menu()
    texts = [a.text for a in tray.connections_menu.actions if a is not None]
    assert any("远程 / 未验证" in text for text in texts)


def test_tray_run_builds_menu_and_execs(qt_stubs: None, config: AegisConfig) -> None:
    """run() builds the menu, shows the tray icon and calls app.exec()."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray.run()

    assert tray.tray.visible is True
    assert tray.tray.tooltip == "DoctorAgent"
    assert tray.tray.menu is tray.menu
    texts = _menu_texts(tray.menu)
    assert "🚪 Quit" in texts
    assert any("About DoctorAgent" in text for text in texts)


def test_tray_quick_action_handlers_invoke_real_behaviour(
    qt_stubs: None, config: AegisConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quick action handlers invoke real behaviour instead of printing placeholders."""
    from doctoragent.presentation import tray as tray_module
    from doctoragent.presentation.tray import TrayApplication

    # Reset shared stub state to avoid cross-test pollution.
    FakeMessageBox._last_information = None
    FakeMessageBox._last_about = None
    FakeDesktopServices.opened_urls = []

    opened_paths: list[Path] = []
    monkeypatch.setattr(
        tray_module,
        "open_path",
        lambda path: opened_paths.append(path),
    )

    vault_browser_opened: list[tuple[object, object, object]] = []

    class FakeVaultBrowser:
        def __init__(self, task_store: object, vault_path: object, vault_key: object) -> None:
            vault_browser_opened.append((task_store, vault_path, vault_key))

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(tray_module, "VaultBrowser", FakeVaultBrowser)

    tray = TrayApplication(config=config)

    tray._open_inbox()
    tray._open_vault()
    tray._open_dashboard()
    tray._show_about()
    tray._open_docs()
    tray._open_task_center()

    # _open_inbox / _open_vault delegate to the file manager helper.
    assert opened_paths == [config.paths.inbox, config.paths.vault]

    # _open_dashboard shows an information dialog with task statistics.
    assert FakeMessageBox._last_information is not None
    _, dash_title, dash_text = FakeMessageBox._last_information
    assert "Dashboard" in dash_title
    assert "DoctorAgent Dashboard" in dash_text
    assert "总计" in dash_text

    # _show_about shows an About dialog referencing the version.
    assert FakeMessageBox._last_about is not None
    _, about_title, about_text = FakeMessageBox._last_about
    assert "About DoctorAgent" in about_title
    # Version is sourced from doctoragent.__version__; assert it's present
    # rather than hard-coding the literal (broke on every version bump).
    from doctoragent import __version__

    assert __version__ in about_text

    # _open_docs opens the documentation URL in the browser.
    assert len(FakeDesktopServices.opened_urls) == 1
    assert FakeDesktopServices.opened_urls[0].toString() == "https://github.com/weed33834/DoctorAgent"

    # _open_task_center opens the Vault Browser.
    assert len(vault_browser_opened) == 1
    assert vault_browser_opened[0][0] is tray.task_store
    assert vault_browser_opened[0][1] == config.paths.vault


def test_tray_dashboard_without_task_store(qt_stubs: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard shows a not-configured message when no task store is available."""
    from doctoragent.presentation.tray import TrayApplication

    FakeMessageBox._last_information = None

    tray = TrayApplication()
    tray._open_dashboard()

    assert FakeMessageBox._last_information is not None
    _, title, text = FakeMessageBox._last_information
    assert title == "Dashboard"
    assert "not configured" in text


def test_tray_activity_summary_text(qt_stubs: None, config: AegisConfig) -> None:
    """Activity summary reflects task counts in the quick actions panel."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    active_id = uuid4()
    completed_id = uuid4()
    failed_id = uuid4()
    store.create(active_id, Path("/tmp/active.txt"))
    store.create(completed_id, Path("/tmp/done.txt"))
    store.update_state(completed_id, TaskState.COMPLETED)
    store.create(failed_id, Path("/tmp/failed.txt"))
    store.update_state(failed_id, TaskState.FAILED)

    tray = TrayApplication(config=config)
    summary = tray._activity_summary_text()

    assert "总计 3" in summary
    assert "进行中 1" in summary
    assert "完成 1" in summary
    assert "失败 1" in summary


def test_tray_activity_summary_without_store(qt_stubs: None) -> None:
    """Activity summary reports not configured when no task store exists."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication()
    assert tray._activity_summary_text() == "📦 Tasks not configured"


def test_tray_activity_summary_with_quarantined(qt_stubs: None, config: AegisConfig) -> None:
    """Activity summary includes the quarantined count."""
    from doctoragent.presentation.tray import TrayApplication

    store = TaskStore(config.paths.index / "tasks.db")
    quarantined_id = uuid4()
    store.create(quarantined_id, Path("/tmp/bad.txt"))
    store.update_state(quarantined_id, TaskState.QUARANTINED)

    tray = TrayApplication(config=config)
    summary = tray._activity_summary_text()

    assert "隔离 1" in summary


def test_tray_task_action_includes_tooltip(qt_stubs: None, config: AegisConfig) -> None:
    """Task actions expose a tooltip with state details."""
    from doctoragent.presentation.tray import TrayApplication

    from doctoragent.api.schemas import TaskSummary

    tray = TrayApplication(config=config)
    task = TaskSummary(
        task_id=uuid4(),
        state=TaskState.ENCRYPTING.name,
        message="processing",
    )
    action = tray._task_action(task, tray.tasks_menu)

    assert "加密中" in action.text
    assert "processing" in action.text
    assert action.tooltip == "正在加密并写入保险库"


def test_tray_open_connection_manager(qt_stubs: None, config: AegisConfig) -> None:
    """Opening the connection manager creates a ConnectionManagerDialog."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    # Dialog exec is a no-op with stubs; this exercises the creation path.
    tray._open_connection_manager()


def test_tray_vault_size_text_in_tb(qt_stubs: None, config: AegisConfig) -> None:
    """Vault size falls back to TB for very large directories."""
    from types import SimpleNamespace

    from doctoragent.presentation.tray import TrayApplication

    class FakeVault:
        def exists(self) -> bool:
            return True

        def rglob(self, _pattern: str) -> list["FakeVault"]:
            return [self]

        def is_file(self) -> bool:
            return True

        def stat(self) -> object:
            return SimpleNamespace(st_size=5 * 1024**4)

    tray = TrayApplication(config=config)
    tray.config.paths.vault = FakeVault()  # type: ignore[assignment]

    assert tray._vault_size_text().endswith("TB")


def test_tray_search_vault_opens_browser(qt_stubs: None, config: AegisConfig) -> None:
    """Clicking Search Vault opens the Vault Browser (not a separate dialog).

    The old ``SearchVaultDialog`` was removed and its functionality merged
    into ``VaultBrowser``.  This test verifies that the tray delegates to
    ``VaultBrowser`` and optionally applies a search filter.
    """
    from doctoragent.presentation import tray as tray_module
    from doctoragent.presentation.tray import TrayApplication
    from PyQt6.QtWidgets import QInputDialog

    opened: list[tuple[object, object, object]] = []
    search_queries: list[str] = []

    class FakeVaultBrowser:
        def __init__(
            self, task_store: object, vault_path: object, vault_key: object
        ) -> None:
            opened.append((task_store, vault_path, vault_key))

        def set_search_filter(self, query: str) -> None:
            search_queries.append(query)

        def exec(self) -> int:
            return 1

    original_browser = tray_module.VaultBrowser
    original_input = QInputDialog.getText
    tray_module.VaultBrowser = FakeVaultBrowser  # type: ignore[misc]
    QInputDialog.getText = lambda *a, **kw: ("finance", True)  # type: ignore[assignment]
    try:
        tray = TrayApplication(config=config, vault_key=b"x" * 32)
        tray._search_vault()
    finally:
        tray_module.VaultBrowser = original_browser
        QInputDialog.getText = original_input  # type: ignore[assignment]

    assert len(opened) == 1
    assert opened[0][0] is tray.task_store
    assert opened[0][1] == config.paths.vault
    assert opened[0][2] == b"x" * 32
    assert search_queries == ["finance"]


def test_tray_settings_menu_item_opens_dialog(
    qt_stubs: None, config: AegisConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Settings... menu item opens the settings dialog."""
    from doctoragent.presentation import tray as tray_module
    from doctoragent.presentation.tray import TrayApplication

    opened: list[AegisConfig] = []

    class FakeSettingsDialog:
        def __init__(self, cfg: AegisConfig) -> None:
            opened.append(cfg)

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(tray_module, "SettingsDialog", FakeSettingsDialog)

    tray = TrayApplication(config=config)
    tray._open_settings()

    assert len(opened) == 1
    assert opened[0] is tray.config


def test_tray_vault_browser_menu_item_opens_dialog(
    qt_stubs: None, config: AegisConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Vault Browser... menu item opens the Vault browser dialog."""
    from doctoragent.presentation import tray as tray_module
    from doctoragent.presentation.tray import TrayApplication

    opened: list[tuple[object, object, object]] = []

    class FakeVaultBrowser:
        def __init__(self, task_store: object, vault_path: object, vault_key: object) -> None:
            opened.append((task_store, vault_path, vault_key))

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(tray_module, "VaultBrowser", FakeVaultBrowser)

    tray = TrayApplication(config=config, vault_key=b"k" * 32)
    tray._open_vault_browser()

    assert len(opened) == 1
    assert opened[0][0] is tray.task_store
    assert opened[0][1] == config.paths.vault
    assert opened[0][2] == b"k" * 32


def test_tray_menu_contains_settings_and_vault_browser(qt_stubs: None, config: AegisConfig) -> None:
    """The main tray menu exposes Settings... and Vault Browser... entries."""
    from doctoragent.presentation.tray import TrayApplication

    tray = TrayApplication(config=config)
    tray._build_menu()

    texts = _menu_texts(tray.menu)
    assert "⚙️ Settings..." in texts
    assert "🗄️ Vault Browser..." in texts

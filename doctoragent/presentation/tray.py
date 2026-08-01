"""System tray application with connection management entry."""

import time
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import (
        QApplication,
        QDialog,
        QLabel,
        QMenu,
        QProgressBar,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidgetAction,
    )
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent import __version__
from doctoragent.api.schemas import TaskSummary
from doctoragent.config import AegisConfig
from doctoragent.connections.manager import ConnectionManager
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.presentation.agent_config_dialog import AgentConfigDialog
from doctoragent.presentation.audit_viewer import AuditLogViewerDialog
from doctoragent.presentation.backup_dialog import BackupDialog
from doctoragent.presentation.connection_dialog import ConnectionManagerDialog
from doctoragent.presentation.key_management_dialog import KeyManagementDialog
from doctoragent.presentation.monitor_panel import MonitorPanel
from doctoragent.presentation.settings_dialog import SettingsDialog
from doctoragent.presentation.skill_manager import SkillManagerDialog
from doctoragent.presentation.utils import human_size, open_path
from doctoragent.presentation.vault_browser import VaultBrowser

_STATE_ICONS: dict[str, str] = {
    TaskState.IDLE.name: "⏳",
    TaskState.CLASSIFYING.name: "🔍",
    TaskState.ENCRYPTING.name: "🔒",
    TaskState.INDEXING.name: "📊",
    TaskState.COMPLETED.name: "✅",
    TaskState.FAILED.name: "❌",
    TaskState.QUARANTINED.name: "⚠️",
}

_STATE_SHORT_LABELS: dict[str, str] = {
    TaskState.IDLE.name: "等待中",
    TaskState.CLASSIFYING.name: "识别中",
    TaskState.ENCRYPTING.name: "加密中",
    TaskState.INDEXING.name: "索引中",
    TaskState.COMPLETED.name: "已完成",
    TaskState.FAILED.name: "失败",
    TaskState.QUARANTINED.name: "已隔离",
}

_STATE_DETAILS: dict[str, str] = {
    TaskState.IDLE.name: "任务等待处理",
    TaskState.CLASSIFYING.name: "正在识别内容类别",
    TaskState.ENCRYPTING.name: "正在加密并写入保险库",
    TaskState.INDEXING.name: "正在建立索引",
    TaskState.COMPLETED.name: "已完成加密归档",
    TaskState.FAILED.name: "处理失败，需要关注",
    TaskState.QUARANTINED.name: "已隔离，等待人工复核",
}

_STATUS_ICONS = {
    "secure": "🔐",
    "warning": "⚠️",
    "offline": "🔌",
    "online": "🌐",
}


class TrayApplication:
    """System tray application."""

    def __init__(
        self,
        connections_path: Path | None = None,
        config: AegisConfig | None = None,
        vault_key: bytes | None = None,
        audit_logger: Any = None,
    ) -> None:
        existing = QApplication.instance()
        self.app = existing if isinstance(existing, QApplication) else QApplication([])
        self.tray = QSystemTrayIcon()
        self.menu = QMenu()
        self.config = config
        self.vault_key = vault_key
        self.audit_logger = audit_logger
        self.connections_path = connections_path or (
            config.paths.connections
            if config is not None
            else Path.home() / "DoctorAgent" / "Config" / "connections.json"
        )
        self.task_store: TaskStore | None = None
        if config is not None:
            self.task_store = TaskStore(config.paths.index / "tasks.db")

        self.connection_manager = ConnectionManager(self.connections_path)

        # Cache for the human-readable vault size to avoid walking the entire
        # Vault directory on the UI thread every time the menu is shown.
        self._vault_size_cache: tuple[str, str, float] | None = None
        self._vault_size_ttl_seconds: float = 60.0

        self._header_action = QWidgetAction(self.menu)
        self._header_label = QLabel()
        self._header_action.setDefaultWidget(self._header_label)

        self.tasks_menu = QMenu("Tasks")
        self._tasks_progress_action = QWidgetAction(self.tasks_menu)
        self._tasks_progress_bar = QProgressBar()
        self._tasks_progress_bar.setRange(0, 100)
        self._tasks_progress_bar.setTextVisible(True)
        self._tasks_progress_action.setDefaultWidget(self._tasks_progress_bar)

        self.connections_menu = QMenu("Connections")

    def run(self) -> None:
        """Start the tray application."""
        self._build_menu()
        self._refresh_header()
        self.tray.setContextMenu(self.menu)
        self.tray.setVisible(True)
        self.tray.setToolTip("DoctorAgent")
        self.app.exec()

    def _build_menu(self) -> None:
        """Construct the context menu structure."""
        self.menu.aboutToShow.connect(self._refresh_header)
        self.menu.addAction(self._header_action)
        self.menu.addSeparator()

        self._add_quick_actions(self.menu)
        self.menu.addSeparator()

        self._build_connections_menu()
        self.menu.addMenu(self.connections_menu)

        self._build_tasks_menu()
        self.menu.addMenu(self.tasks_menu)

        self.menu.addSeparator()
        self._add_enterprise_actions(self.menu)

        self.menu.addSeparator()

        settings_action = QAction("⚙️ Settings...", self.menu)
        settings_action.triggered.connect(self._open_settings)
        self.menu.addAction(settings_action)

        vault_browser_action = QAction("🗄️ Vault Browser...", self.menu)
        vault_browser_action.triggered.connect(self._open_vault_browser)
        self.menu.addAction(vault_browser_action)

        self.menu.addSeparator()
        self.menu.addSection("ℹ️ Help")

        about_action = QAction(f"About DoctorAgent v{__version__}", self.menu)
        about_action.triggered.connect(self._show_about)
        self.menu.addAction(about_action)

        docs_action = QAction("📖 Open Documentation", self.menu)
        docs_action.triggered.connect(self._open_docs)
        self.menu.addAction(docs_action)

        self.menu.addSeparator()
        quit_action = QAction("🚪 Quit", self.menu)
        quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(quit_action)

    def _add_quick_actions(self, menu: QMenu) -> None:
        """Add static quick-entry actions with icons and grouping."""
        menu.addSection("⚡ Quick Actions")

        open_inbox = QAction("📥 Open Inbox", menu)
        open_inbox.triggered.connect(self._open_inbox)
        menu.addAction(open_inbox)

        open_vault = QAction("🔐 Open Vault", menu)
        open_vault.triggered.connect(self._open_vault)
        menu.addAction(open_vault)

        search_vault = QAction("🔍 Search Vault...", menu)
        search_vault.triggered.connect(self._search_vault)
        menu.addAction(search_vault)

        dashboard = QAction("📊 Dashboard", menu)
        dashboard.triggered.connect(self._open_dashboard)
        menu.addAction(dashboard)

        menu.addSeparator()
        menu.addSection("🔔 Activity")
        alert_count = self._alert_count()
        self._notifications_action = QAction(f"🔔 Notifications ({alert_count})", menu)
        self._notifications_action.setEnabled(alert_count > 0)
        menu.addAction(self._notifications_action)

        status_summary = QAction(self._activity_summary_text(), menu)
        status_summary.setEnabled(False)
        menu.addAction(status_summary)

    def _activity_summary_text(self) -> str:
        """Return a short activity summary string for the quick actions panel."""
        if self.task_store is None:
            return "📦 Tasks not configured"
        counts = self.task_store.counts_by_state()
        total = sum(counts.values())
        completed = counts.get(TaskState.COMPLETED.name, 0)
        failed = counts.get(TaskState.FAILED.name, 0)
        quarantined = counts.get(TaskState.QUARANTINED.name, 0)
        active = total - completed - failed - quarantined
        parts = [f"📋 总计 {total}"]
        if active:
            parts.append(f"⚙️ 进行中 {active}")
        parts.append(f"✅ 完成 {completed}")
        if failed:
            parts.append(f"❌ 失败 {failed}")
        if quarantined:
            parts.append(f"⚠️ 隔离 {quarantined}")
        return " · ".join(parts)

    def _add_enterprise_actions(self, menu: QMenu) -> None:
        """Add the enterprise management section with all six new dialogs."""
        menu.addSection("🏢 Enterprise")

        audit_action = QAction("📋 Audit Log...", menu)
        audit_action.triggered.connect(self._open_audit_viewer)
        menu.addAction(audit_action)

        backup_action = QAction("💾 Backup Manager...", menu)
        backup_action.triggered.connect(self._open_backup_dialog)
        menu.addAction(backup_action)

        key_action = QAction("🔑 Key Management...", menu)
        key_action.triggered.connect(self._open_key_management)
        menu.addAction(key_action)

        skill_action = QAction("🧠 Skill Manager...", menu)
        skill_action.triggered.connect(self._open_skill_manager)
        menu.addAction(skill_action)

        agent_action = QAction("🤖 Agent Config...", menu)
        agent_action.triggered.connect(self._open_agent_config)
        menu.addAction(agent_action)

        monitor_action = QAction("📊 Monitor Panel...", menu)
        monitor_action.triggered.connect(self._open_monitor_panel)
        menu.addAction(monitor_action)

    # ── Enterprise dialog handlers ───────────────────────────────────────

    def _ensure_config(self) -> AegisConfig:
        """Load the config from disk if it has not been set yet."""
        if self.config is None:
            self.config = AegisConfig.load_from_file()
        return self.config

    def _create_llm_provider(self) -> Any:
        """Create an LLM provider from the default chat connection, if any."""
        try:
            conn = self.connection_manager.get_default_chat_connection()
            if conn is None:
                return None
            from doctoragent.model.provider import create_provider

            return create_provider(conn)
        except Exception:
            return None

    def _create_skill_registry(self) -> Any:
        """Create a default skill registry with all built-in skills."""
        try:
            from doctoragent.model.skills import create_default_skill_registry

            llm = self._create_llm_provider()
            return create_default_skill_registry(llm_provider=llm)
        except Exception:
            return None

    def _create_tool_registry(self) -> Any:
        """Create a default tool registry with all built-in tools."""
        try:
            from doctoragent.model.tools import create_default_registry

            llm = self._create_llm_provider()
            return create_default_registry(
                task_store=self.task_store,
                llm_provider=llm,
            )
        except Exception:
            return None

    def _create_master_key_provider(self) -> Any:
        """Create a master key provider from the current config, if possible."""
        if self.config is None:
            return None
        try:
            from doctoragent.security.master_key import create_master_key_provider

            provider_name = self.config.security.master_key_provider
            storage_path = self.config.paths.vault / ".master_key"
            password = self.config.security.master_key_password
            return create_master_key_provider(
                provider_name=provider_name,
                storage_path=storage_path,
                password=password,
            )
        except Exception:
            return None

    def _create_backpressure_guard(self) -> Any:
        """Create a BackpressureGuard from the configured resource limits."""
        if self.config is None:
            return None
        try:
            from doctoragent.security.resources import BackpressureGuard

            res = self.config.resources
            return BackpressureGuard(
                high_watermark=res.inbox_backlog_high_watermark,
                low_watermark=res.inbox_backlog_low_watermark,
            )
        except Exception:
            return None

    def _open_audit_viewer(self) -> None:
        """Open the audit log viewer dialog."""
        dialog = AuditLogViewerDialog(self.audit_logger, parent=self.menu)
        dialog.exec()

    def _open_backup_dialog(self) -> None:
        """Open the backup manager dialog."""
        config = self._ensure_config()
        dialog = BackupDialog(
            config=config,
            connection_manager=self.connection_manager,
            audit_logger=self.audit_logger,
            parent=self.menu,
        )
        dialog.exec()

    def _open_key_management(self) -> None:
        """Open the key management dialog."""
        config = self._ensure_config()
        provider = self._create_master_key_provider()
        dialog = KeyManagementDialog(
            config=config,
            vault_key=self.vault_key,
            master_key_provider=provider,
            audit_logger=self.audit_logger,
            parent=self.menu,
        )
        dialog.exec()

    def _open_skill_manager(self) -> None:
        """Open the skill manager dialog."""
        registry = self._create_skill_registry()
        if registry is None:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self.menu,
                "Skill Manager",
                "Could not initialise the skill registry. "
                "Check that a model connection is configured.",
            )
            return
        llm = self._create_llm_provider()
        dialog = SkillManagerDialog(
            skill_registry=registry,
            llm_provider=llm,
            parent=self.menu,
        )
        dialog.exec()

    def _open_agent_config(self) -> None:
        """Open the agent configuration dialog."""
        config = self._ensure_config()
        tool_registry = self._create_tool_registry()
        skill_registry = self._create_skill_registry()
        dialog = AgentConfigDialog(
            config=config,
            connection_manager=self.connection_manager,
            tool_registry=tool_registry,
            skill_registry=skill_registry,
            parent=self.menu,
        )
        dialog.exec()

    def _open_monitor_panel(self) -> None:
        """Open the monitoring panel in a modal dialog wrapper."""
        config = self._ensure_config()
        guard = self._create_backpressure_guard()
        panel = MonitorPanel(
            config=config,
            task_store=self.task_store,
            connection_manager=self.connection_manager,
            backpressure_guard=guard,
            audit_logger=self.audit_logger,
        )
        wrapper = QDialog(self.menu)
        wrapper.setWindowTitle("DoctorAgent Monitor")
        wrapper.setMinimumSize(650, 500)
        layout = QVBoxLayout(wrapper)
        layout.addWidget(panel)
        wrapper.exec()
        panel.stop_timer()

    def _alert_count(self) -> int:
        """Return the number of pending security alerts.

        Delegates to the AuditLogger's ``pending_alert_count`` property which,
        when an :class:`AlertManager` is attached, returns the count of
        notified-but-unresolved CRITICAL/WARNING alerts.  Falls back to the
        legacy decrypt-failure heuristic when no AlertManager is present.
        """
        if self.audit_logger is None:
            return 0
        return getattr(self.audit_logger, "pending_alert_count", 0) or 0

    def _open_inbox(self) -> None:
        """Open the configured inbox directory in the file manager."""
        inbox = self.config.paths.inbox if self.config else Path.home() / "DoctorAgent" / "Inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        open_path(inbox)

    def _open_vault(self) -> None:
        """Open the configured vault directory in the file manager."""
        vault = self.config.paths.vault if self.config else Path.home() / "DoctorAgent" / "Vault"
        vault.mkdir(parents=True, exist_ok=True)
        open_path(vault)

    def _search_vault(self) -> None:
        """Open the Vault Browser with a search prompt.

        Replaces the removed ``SearchVaultDialog`` by delegating to the
        full-featured ``VaultBrowser`` which already supports keyword,
        category, sensitivity and tag filtering.
        """
        from PyQt6.QtWidgets import QInputDialog

        vault_path = (
            self.config.paths.vault
            if self.config is not None
            else Path.home() / "DoctorAgent" / "Vault"
        )
        query, ok = QInputDialog.getText(self.menu, "🔍 Search Vault", "Search keyword:", text="")
        dialog = VaultBrowser(self.task_store, vault_path, self.vault_key)
        if ok and query.strip():
            dialog.set_search_filter(query.strip())
        dialog.exec()

    def _open_dashboard(self) -> None:
        """Show a dashboard summary dialog with task statistics."""
        from PyQt6.QtWidgets import QMessageBox

        if self.task_store is None:
            QMessageBox.information(self.menu, "Dashboard", "Task store not configured.")
            return

        counts = self.task_store.counts_by_state()
        total = sum(counts.values())
        completed = counts.get(TaskState.COMPLETED.name, 0)
        failed = counts.get(TaskState.FAILED.name, 0)
        quarantined = counts.get(TaskState.QUARANTINED.name, 0)
        active = total - completed - failed - quarantined

        vault_size = self._vault_size_text()
        local_ok = any(
            conn.is_enabled and conn.is_trusted_local()
            for conn in self.connection_manager.list_all()
        )
        conn_status = "✅ 本地连接正常" if local_ok else "⚠️ 未配置本地连接"

        text = (
            f"🔐 DoctorAgent Dashboard\n\n"
            f"━━━ 任务统计 ━━━\n"
            f"📋 总计: {total}\n"
            f"⚙️ 进行中: {active}\n"
            f"✅ 已完成: {completed}\n"
            f"❌ 失败: {failed}\n"
            f"⚠️ 隔离: {quarantined}\n\n"
            f"━━━ 存储状态 ━━━\n"
            f"📦 Vault 大小: {vault_size}\n"
            f"🔌 连接状态: {conn_status}"
        )
        QMessageBox.information(self.menu, "📊 Dashboard", text)

    def _show_about(self) -> None:
        """Show the About dialog with version and description."""
        from PyQt6.QtWidgets import QMessageBox

        text = (
            f"<h3>🔐 DoctorAgent</h3>"
            f"<p>Version {__version__}</p>"
            f"<p>Local private content management agent.</p>"
            f"<p>Inbox → Classify → Encrypt → Vault</p>"
            f"<hr/>"
            f"<p><small>AES-256-GCM encryption · Argon2id key derivation · "
            f"Sandboxed execution · Offline verification</small></p>"
        )
        QMessageBox.about(self.menu, "About DoctorAgent", text)

    def _open_docs(self) -> None:
        """Open the documentation in the default browser."""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        url = QUrl("https://github.com/weed33834/DoctorAgent")
        QDesktopServices.openUrl(url)

    def _refresh_header(self) -> None:
        """Update the header label with app name, version and status summary."""
        status = self._status_summary()
        self._header_label.setText(
            f"<b>🔐 DoctorAgent</b> <span style='color:#888'>v{__version__}</span>"
            f"<br/><small>{status}</small>"
        )

    def _status_summary(self) -> str:
        """Build a one-line status summary for the header."""
        local_ok = any(
            conn.is_enabled and conn.is_trusted_local()
            for conn in self.connection_manager.list_all()
        )
        connection_text = (
            f"{_STATUS_ICONS['online']} 本地连接正常"
            if local_ok
            else f"{_STATUS_ICONS['offline']} 未配置本地连接"
        )

        completed = failed = quarantined = 0
        if self.task_store is not None:
            counts = self.task_store.counts_by_state()
            completed = counts.get(TaskState.COMPLETED.name, 0)
            failed = counts.get(TaskState.FAILED.name, 0)
            quarantined = counts.get(TaskState.QUARANTINED.name, 0)

        vault_size = self._vault_size_text()
        secure_text = (
            f"{_STATUS_ICONS['secure']} 已加密"
            if completed
            else f"{_STATUS_ICONS['warning']} 等待文件"
        )
        parts = [secure_text, connection_text, f"完成 {completed}"]
        if failed:
            parts.append(f"{_STATE_ICONS[TaskState.FAILED.name]} 失败 {failed}")
        if quarantined:
            parts.append(f"{_STATE_ICONS[TaskState.QUARANTINED.name]} 隔离 {quarantined}")
        parts.append(f"📦 {vault_size}")
        return " · ".join(parts)

    def _vault_size_text(self) -> str:
        """Return a human-readable vault size.

        The result is cached per vault path with a short TTL so the UI thread
        does not synchronously walk the entire Vault directory on every menu
        refresh.
        """
        vault = self.config.paths.vault if self.config else Path.home() / "DoctorAgent" / "Vault"
        vault_key = str(vault)
        now = time.monotonic()
        if self._vault_size_cache is not None:
            cached_key, cached_text, cached_at = self._vault_size_cache
            if cached_key == vault_key and (now - cached_at) < self._vault_size_ttl_seconds:
                return cached_text

        if not vault.exists():
            text = "0 B"
        else:
            total_bytes = sum(f.stat().st_size for f in vault.rglob("*") if f.is_file())
            text = human_size(total_bytes)

        self._vault_size_cache = (vault_key, text, now)
        return text

    def _build_connections_menu(self) -> None:
        """Create the connections submenu and wire lazy refresh."""
        self.connections_menu.aboutToShow.connect(self._refresh_connections_menu)
        self._refresh_connections_menu()

    def _refresh_connections_menu(self) -> None:
        """Refresh the connections submenu with status icons and grouping."""
        self.connections_menu.clear()
        self.connections_menu.addSection("🔌 Enabled Connections")
        enabled = self.connection_manager.list_enabled()

        if not enabled:
            none_action = QAction("No connections enabled", self.connections_menu)
            none_action.setEnabled(False)
            self.connections_menu.addAction(none_action)
        else:
            for conn in enabled:
                if conn.is_trusted_local():
                    icon = _STATUS_ICONS["secure"]
                    mark = "本地 · 可信"
                else:
                    icon = _STATUS_ICONS["online"]
                    mark = "远程 / 未验证"
                label = f"{icon} {conn.name} ({conn.platform_type.value}) · {mark}"
                action = QAction(label, self.connections_menu)
                action.setEnabled(False)
                action.setToolTip(conn.base_url)
                self.connections_menu.addAction(action)

        self.connections_menu.addSeparator()
        manage_action = QAction("⚙️ Manage Connections...", self.connections_menu)
        manage_action.triggered.connect(self._open_connection_manager)
        self.connections_menu.addAction(manage_action)

    def _build_tasks_menu(self) -> None:
        """Create the initial tasks submenu and wire refresh."""
        self.tasks_menu.aboutToShow.connect(self._refresh_tasks_menu)
        self._refresh_tasks_menu()

    def _refresh_tasks_menu(self) -> None:
        """Refresh the tasks submenu from the task store."""
        self.tasks_menu.clear()
        self.tasks_menu.addSection("📈 Task Activity")

        if self.task_store is None:
            not_configured = QAction("Tasks not configured", self.tasks_menu)
            not_configured.setEnabled(False)
            self.tasks_menu.addAction(not_configured)
            self.tasks_menu.addSeparator()
            self.tasks_menu.addAction(self._refresh_action())
            return

        counts = self.task_store.counts_by_state()
        total = sum(counts.values())
        completed = counts.get(TaskState.COMPLETED.name, 0)
        failed = counts.get(TaskState.FAILED.name, 0)
        quarantined = counts.get(TaskState.QUARANTINED.name, 0)
        progress = int(100 * completed / total) if total else 0

        self._tasks_progress_bar.setValue(progress)
        status_icon = (
            _STATUS_ICONS["secure"]
            if failed == 0 and quarantined == 0
            else _STATUS_ICONS["warning"]
        )
        self._tasks_progress_bar.setFormat(
            f"{status_icon} 完成 {completed}/{total} · 失败 {failed} · 隔离 {quarantined}"
        )
        self.tasks_menu.addAction(self._tasks_progress_action)
        self.tasks_menu.addSeparator()

        active = self.task_store.list_active(limit=3)
        recent_completed = [
            task
            for task in self.task_store.list_recent(limit=10)
            if task.state == TaskState.COMPLETED.name
        ][:5]
        attention = self.task_store.list_attention(limit=3)

        self._add_task_section(self.tasks_menu, "🔥 进行中", active, empty_text="暂无进行中的任务")
        self._add_task_section(
            self.tasks_menu,
            "✅ 最近完成",
            recent_completed,
            empty_text="暂无已完成任务",
        )
        self._add_task_section(
            self.tasks_menu,
            "⚠️ 需关注",
            attention,
            empty_text="暂无失败或隔离任务",
        )

        self.tasks_menu.addSeparator()
        task_center = QAction("🗂️ 打开任务中心...", self.tasks_menu)
        task_center.triggered.connect(self._open_task_center)
        self.tasks_menu.addAction(task_center)
        self.tasks_menu.addAction(self._refresh_action())

    def _add_task_section(
        self,
        menu: QMenu,
        title: str,
        tasks: list[TaskSummary],
        empty_text: str,
    ) -> None:
        """Add a labelled section of task actions."""
        menu.addSection(title)
        if not tasks:
            empty_action = QAction(empty_text, menu)
            empty_action.setEnabled(False)
            menu.addAction(empty_action)
        else:
            for task in tasks:
                action = self._task_action(task, menu)
                menu.addAction(action)
        menu.addSeparator()

    def _task_action(self, task: TaskSummary, parent: QMenu) -> QAction:
        """Build a disabled action representing a task row."""
        state = task.state
        short_id = str(task.task_id)[:8]
        icon = _STATE_ICONS.get(state, "•")
        short_state = _STATE_SHORT_LABELS.get(state, state)
        detail = _STATE_DETAILS.get(state, "")
        message = task.message or ""
        label = f"{icon} {short_id} · {short_state}"
        if message:
            snippet = message.replace("\n", " ")[:40]
            label = f"{label} · {snippet}"
        action = QAction(label, parent)
        action.setEnabled(False)
        action.setToolTip(detail)
        return action

    def _refresh_action(self) -> QAction:
        """Return a Refresh action wired to the tasks menu refresh handler."""
        refresh_action = QAction("🔄 Refresh", self.tasks_menu)
        refresh_action.triggered.connect(self._refresh_tasks_menu)
        return refresh_action

    def _open_task_center(self) -> None:
        """Open the Vault Browser as the task center."""
        self._open_vault_browser()

    def _open_connection_manager(self) -> None:
        """Open the platform connection manager dialog."""
        dialog = ConnectionManagerDialog(self.connection_manager)
        dialog.exec()
        # Reload the connection manager so subsequent menu renders reflect any
        # changes made inside the dialog, then refresh the connections menu.
        self.connection_manager = ConnectionManager(self.connections_path)
        self._refresh_connections_menu()

    def _open_settings(self) -> None:
        """Open the settings dialog."""
        if self.config is None:
            self.config = AegisConfig.load_from_file()
        dialog = SettingsDialog(self.config)
        dialog.exec()

    def _open_vault_browser(self) -> None:
        """Open the Vault browser dialog."""
        vault_path = (
            self.config.paths.vault
            if self.config is not None
            else Path.home() / "DoctorAgent" / "Vault"
        )
        dialog = VaultBrowser(self.task_store, vault_path, self.vault_key)
        dialog.exec()

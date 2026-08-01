"""Agent configuration dialog (PyQt6).

Provides a UI to tune Agent behavior parameters, select the LLM provider and
model, and toggle individual tools and skills. Configuration is persisted to
a JSON file so it survives restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via stubs in tests
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent.model.agent import AgentConfig


class AgentConfigDialog(QDialog):
    """Dialog to configure Agent behavior, model, tools, and skills."""

    def __init__(
        self,
        config: Any = None,
        agent_config: AgentConfig | None = None,
        connection_manager: Any = None,
        tool_registry: Any = None,
        skill_registry: Any = None,
        config_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.agent_config = agent_config or AgentConfig()
        self.connection_manager = connection_manager
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self.config_path = config_path or (
            Path.home() / "DoctorAgent" / "Config" / "agent_config.json"
        )
        self.setWindowTitle("Agent Configuration")
        self.setMinimumSize(700, 600)

        # Load persisted config if the file exists and no explicit config was passed.
        if agent_config is None and self.config_path.exists():
            self._load_config()

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_parameters_group())
        layout.addWidget(self._build_model_group())
        layout.addWidget(self._build_tools_group())
        layout.addWidget(self._build_skills_group())
        layout.addLayout(self._build_button_row())

        self._populate_from_config()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_parameters_group(self) -> QGroupBox:
        """Build the agent behavior parameter form."""
        group = QGroupBox("Agent Parameters")
        form = QFormLayout(group)

        self.max_iterations_spin = QSpinBox()
        self.max_iterations_spin.setRange(1, 100)
        self.max_iterations_spin.setSuffix(" iterations")
        form.addRow("Max Iterations:", self.max_iterations_spin)

        self.max_tool_calls_spin = QSpinBox()
        self.max_tool_calls_spin.setRange(0, 50)
        self.max_tool_calls_spin.setSuffix(" calls")
        form.addRow("Max Tool Calls:", self.max_tool_calls_spin)

        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setSingleStep(0.1)
        form.addRow("Temperature:", self.temperature_spin)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(100, 32000)
        self.max_tokens_spin.setSuffix(" tokens")
        form.addRow("Max Tokens:", self.max_tokens_spin)

        self.planning_check = QCheckBox("Enable planning phase")
        self.planning_check.setToolTip(
            "When enabled, the agent creates an execution plan before acting."
        )
        form.addRow(self.planning_check)

        self.reflection_check = QCheckBox("Enable reflection")
        self.reflection_check.setToolTip(
            "When enabled, the agent evaluates its own answer for completeness."
        )
        form.addRow(self.reflection_check)

        self.safety_check = QCheckBox("Safety mode")
        self.safety_check.setToolTip(
            "When enabled, the agent enforces safety guardrails (local-only for sensitive tasks)."
        )
        form.addRow(self.safety_check)

        return group

    def _build_model_group(self) -> QGroupBox:
        """Build the LLM provider/model selection group."""
        group = QGroupBox("Model Selection")
        form = QFormLayout(group)

        self.connection_combo = QComboBox()
        self._populate_connections()
        form.addRow("Connection:", self.connection_combo)

        self.model_name_input = QLineEdit()
        self.model_name_input.setPlaceholderText("Model name (e.g. qwen2.5:7b)")
        form.addRow("Model Name:", self.model_name_input)

        return group

    def _populate_connections(self) -> None:
        """Fill the connection combo with enabled connections."""
        self.connection_combo.clear()
        self.connection_combo.addItem("Default (from config)", userData=None)
        if self.connection_manager is not None:
            try:
                for conn in self.connection_manager.list_enabled():
                    label = f"{conn.name} ({conn.platform_type.value})"
                    self.connection_combo.addItem(label, userData=str(conn.id))
            except Exception:
                pass

    def _build_tools_group(self) -> QGroupBox:
        """Build the tool selection list."""
        group = QGroupBox("Enabled Tools")
        layout = QVBoxLayout(group)
        self.tool_list = QListWidget()
        layout.addWidget(self.tool_list)
        self._populate_tools()
        return group

    def _populate_tools(self) -> None:
        """Fill the tool list with checkboxes from the tool registry."""
        self.tool_list.clear()
        if self.tool_registry is None:
            return
        try:
            for tool_def in self.tool_registry.list_tools():
                item = QListWidgetItem(f"{tool_def.name}: {tool_def.description}")
                item.setData(Qt.ItemDataRole.UserRole, tool_def.name)
                item.setCheckState(Qt.CheckState.Checked)
                self.tool_list.addItem(item)
        except Exception:
            pass

    def _build_skills_group(self) -> QGroupBox:
        """Build the skill selection list."""
        group = QGroupBox("Enabled Skills")
        layout = QVBoxLayout(group)
        self.skill_list = QListWidget()
        layout.addWidget(self.skill_list)
        self._populate_skills()
        return group

    def _populate_skills(self) -> None:
        """Fill the skill list with checkboxes from the skill registry."""
        self.skill_list.clear()
        if self.skill_registry is None:
            return
        try:
            for skill_def in self.skill_registry.list_skills():
                item = QListWidgetItem(f"{skill_def.name}: {skill_def.description}")
                item.setData(Qt.ItemDataRole.UserRole, skill_def.name)
                item.setCheckState(Qt.CheckState.Checked)
                self.skill_list.addItem(item)
        except Exception:
            pass

    def _build_button_row(self) -> QHBoxLayout:
        """Build the action button row."""
        row = QHBoxLayout()

        self.save_button = QPushButton("💾 Save Config")
        self.save_button.clicked.connect(self._save_config)
        row.addWidget(self.save_button)

        self.load_button = QPushButton("📂 Load Config")
        self.load_button.clicked.connect(self._load_config_dialog)
        row.addWidget(self.load_button)

        row.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_and_save)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)

        return row

    # ── Config <-> form sync ─────────────────────────────────────────────

    def _populate_from_config(self) -> None:
        """Set widget values from the current AgentConfig."""
        self.max_iterations_spin.setValue(self.agent_config.max_iterations)
        self.max_tool_calls_spin.setValue(self.agent_config.max_tool_calls)
        self.temperature_spin.setValue(self.agent_config.temperature)
        self.max_tokens_spin.setValue(self.agent_config.max_tokens)
        self.planning_check.setChecked(self.agent_config.enable_planning)
        self.reflection_check.setChecked(self.agent_config.enable_reflection)
        self.safety_check.setChecked(self.agent_config.safety_mode)

        # Pre-fill model name from config if available.
        if self.config is not None:
            try:
                self.model_name_input.setText(self.config.model.model_name)
            except Exception:
                pass

    def _collect_config(self) -> AgentConfig:
        """Build an AgentConfig from the current widget values."""
        return AgentConfig(
            max_iterations=self.max_iterations_spin.value(),
            max_tool_calls=self.max_tool_calls_spin.value(),
            temperature=self.temperature_spin.value(),
            max_tokens=self.max_tokens_spin.value(),
            enable_planning=self.planning_check.isChecked(),
            enable_reflection=self.reflection_check.isChecked(),
            safety_mode=self.safety_check.isChecked(),
        )

    def _selected_tool_names(self) -> list[str]:
        """Return the names of checked tools."""
        names: list[str] = []
        for i in range(self.tool_list.count()):
            item = self.tool_list.item(i)
            if item and item.checkState() == Qt.CheckState.Checked:
                name = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(name, str):
                    names.append(name)
        return names

    def _selected_skill_names(self) -> list[str]:
        """Return the names of checked skills."""
        names: list[str] = []
        for i in range(self.skill_list.count()):
            item = self.skill_list.item(i)
            if item and item.checkState() == Qt.CheckState.Checked:
                name = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(name, str):
                    names.append(name)
        return names

    def _config_dict(self) -> dict[str, Any]:
        """Serialise the full dialog state to a JSON-serialisable dict."""
        return {
            "agent": self._collect_config().model_dump(),
            "model_name": self.model_name_input.text().strip(),
            "connection_id": self.connection_combo.currentData(),
            "enabled_tools": self._selected_tool_names(),
            "enabled_skills": self._selected_skill_names(),
        }

    def _apply_config_dict(self, data: dict[str, Any]) -> None:
        """Apply a loaded config dict to the dialog widgets."""
        agent_data = data.get("agent", {})
        if agent_data:
            self.agent_config = AgentConfig(**agent_data)
            self._populate_from_config()

        model_name = data.get("model_name")
        if isinstance(model_name, str):
            self.model_name_input.setText(model_name)

        conn_id = data.get("connection_id")
        if conn_id:
            idx = self.connection_combo.findData(conn_id)
            if idx >= 0:
                self.connection_combo.setCurrentIndex(idx)

        enabled_tools = set(data.get("enabled_tools", []))
        for i in range(self.tool_list.count()):
            item = self.tool_list.item(i)
            if item:
                name = item.data(Qt.ItemDataRole.UserRole)
                state = (
                    Qt.CheckState.Checked
                    if (not enabled_tools or name in enabled_tools)
                    else Qt.CheckState.Unchecked
                )
                item.setCheckState(state)

        enabled_skills = set(data.get("enabled_skills", []))
        for i in range(self.skill_list.count()):
            item = self.skill_list.item(i)
            if item:
                name = item.data(Qt.ItemDataRole.UserRole)
                state = (
                    Qt.CheckState.Checked
                    if (not enabled_skills or name in enabled_skills)
                    else Qt.CheckState.Unchecked
                )
                item.setCheckState(state)

    # ── Persistence ──────────────────────────────────────────────────────

    def _save_config(self) -> None:
        """Persist the current configuration to the config path."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(self._config_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            QMessageBox.information(
                self, "Saved", f"Agent configuration saved to:\n{self.config_path}"
            )
        except OSError as exc:
            QMessageBox.warning(self, "Save Failed", f"Failed to save config:\n{exc}")

    def _load_config(self) -> None:
        """Load configuration from the config path if it exists."""
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                self._apply_config_dict(data)
        except (OSError, json.JSONDecodeError):
            pass

    def _load_config_dialog(self) -> None:
        """Open a file chooser to load a configuration from an arbitrary path."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Agent Configuration",
            str(self.config_path.parent),
            "JSON (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self._apply_config_dict(data)
            QMessageBox.information(self, "Loaded", f"Configuration loaded from:\n{path}")
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Load Failed", f"Failed to load config:\n{exc}")

    def _accept_and_save(self) -> None:
        """Update the agent config and persist before accepting."""
        self.agent_config = self._collect_config()
        self._save_config()
        self.accept()

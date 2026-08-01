"""Skill manager dialog (PyQt6).

Provides a full management UI for Agent skills: list, enable/disable, test,
import, export, and AI-generate new skill definitions. The dialog wraps the
backend :class:`doctoragent.model.skills.SkillRegistry` and adds an
enable/disable layer that is persisted to a JSON state file.

This is the user-facing surface for the skill lifecycle:
  * register built-in skills at startup
  * toggle which skills are active during Agent execution
  * test a skill against a sample query
  * import / export skill definitions as portable JSON
  * auto-generate a new skill definition from a natural-language description
    via the configured LLM provider
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPushButton,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via stubs in tests
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent.model.skills import (
    Skill,
    SkillCategory,
    SkillDefinition,
    SkillRegistry,
    SkillResult,
)


class LLMSkill(Skill):
    """A skill whose ``execute`` delegates to an LLM provider.

    Used for imported and AI-generated skill definitions that do not have a
    dedicated Python implementation. The skill's triggers and description are
    used to scope the LLM call so the generated answer is relevant to the
    skill's purpose.
    """

    def __init__(self, definition: SkillDefinition, llm_provider: Any = None) -> None:
        self._definition = definition
        self._llm = llm_provider

    @property
    def definition(self) -> SkillDefinition:
        return self._definition

    async def execute(self, query: str, context: dict[str, Any] | None = None) -> SkillResult:
        """Execute the skill by prompting the LLM with the skill context."""
        if self._llm is None:
            return SkillResult(
                success=False,
                skill_name=self._definition.name,
                error="No LLM provider configured for this skill.",
            )
        try:
            system_prompt = (
                f"You are a document management skill named '{self._definition.name}'.\n"
                f"Description: {self._definition.description}\n"
                f"Category: {self._definition.category.value}\n"
                "Respond to the user's query based on the available vault context."
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ]
            # Prefer the sync wrapper when available; fall back to async.
            if hasattr(self._llm, "chat_completion_sync"):
                answer = self._llm.chat_completion_sync(messages)
            else:
                import asyncio

                answer = asyncio.run(self._llm.chat_completion(messages))
            return SkillResult(
                success=True,
                skill_name=self._definition.name,
                result={"answer": answer or ""},
                steps_taken=["llm_skill_executed"],
            )
        except Exception as exc:
            return SkillResult(
                success=False,
                skill_name=self._definition.name,
                error=str(exc),
            )


class SkillManagerDialog(QDialog):
    """Dialog to manage Agent skills."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        llm_provider: Any = None,
        state_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.registry = skill_registry
        self.llm_provider = llm_provider
        self.state_path = state_path or (
            Path.home() / "DoctorAgent" / "Config" / "skills_state.json"
        )
        self.setWindowTitle("Skill Manager")
        self.setMinimumSize(800, 600)

        # Load persisted enable/disable state. Maps skill name -> bool (enabled).
        self._skill_state: dict[str, bool] = self._load_state()

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_skill_list_group())
        layout.addWidget(self._build_test_group(), stretch=1)
        layout.addLayout(self._build_button_row())

        self._refresh_skill_list()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_skill_list_group(self) -> QGroupBox:
        """Build the skill list with toggle controls."""
        group = QGroupBox("Registered Skills")
        layout = QVBoxLayout(group)

        self.skill_list = QListWidget()
        self.skill_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.skill_list)

        button_row = QHBoxLayout()
        self.toggle_button = QPushButton("Toggle Enabled")
        self.toggle_button.clicked.connect(self._toggle_selected)
        button_row.addWidget(self.toggle_button)

        self.detail_label = QLabel("Select a skill to see details.")
        self.detail_label.setWordWrap(True)
        button_row.addWidget(self.detail_label, stretch=1)

        layout.addLayout(button_row)

        # Wire selection change to update the detail label.
        self.skill_list.itemSelectionChanged.connect(self._update_detail)

        return group

    def _build_test_group(self) -> QGroupBox:
        """Build the skill testing panel."""
        group = QGroupBox("Test Skill")
        layout = QVBoxLayout(group)

        form = QFormLayout()
        self.test_query_input = QLineEdit()
        self.test_query_input.setPlaceholderText("Enter a test query...")
        form.addRow("Query:", self.test_query_input)
        layout.addLayout(form)

        self.test_button = QPushButton("🧪 Run Test")
        self.test_button.clicked.connect(self._test_skill)
        layout.addWidget(self.test_button)

        self.test_result = QTextEdit()
        self.test_result.setReadOnly(True)
        self.test_result.setPlaceholderText("Test results will appear here.")
        layout.addWidget(self.test_result, stretch=1)

        return group

    def _build_button_row(self) -> QHBoxLayout:
        """Build the action button row."""
        row = QHBoxLayout()

        self.import_button = QPushButton("📥 Import Skill...")
        self.import_button.clicked.connect(self._import_skill)
        row.addWidget(self.import_button)

        self.export_button = QPushButton("📤 Export Skill...")
        self.export_button.clicked.connect(self._export_skill)
        row.addWidget(self.export_button)

        self.generate_button = QPushButton("🤖 AI Generate Skill...")
        self.generate_button.clicked.connect(self._ai_generate_skill)
        row.addWidget(self.generate_button)

        row.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)

        return row

    # ── State persistence ────────────────────────────────────────────────

    def _load_state(self) -> dict[str, bool]:
        """Load the persisted enable/disable state from disk."""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                return {k: bool(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self) -> None:
        """Persist the current enable/disable state to disk."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._skill_state, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _is_skill_enabled(self, name: str) -> bool:
        """Return True if the skill is enabled (default: True)."""
        return self._skill_state.get(name, True)

    # ── Skill list ───────────────────────────────────────────────────────

    def _refresh_skill_list(self) -> None:
        """Reload the skill list from the registry."""
        self.skill_list.clear()
        for definition in self.registry.list_skills():
            enabled = self._is_skill_enabled(definition.name)
            status = "✅" if enabled else "⛔"
            triggers = ", ".join(definition.triggers) or "—"
            label = (
                f"{status} {definition.name} [{definition.category.value}]\n"
                f"   {definition.description}\n"
                f"   Triggers: {triggers}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, definition.name)
            self.skill_list.addItem(item)

    def _selected_skill_name(self) -> str | None:
        """Return the name of the currently selected skill, or None."""
        items = self.skill_list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def _update_detail(self) -> None:
        """Update the detail label when the selection changes."""
        name = self._selected_skill_name()
        if name is None:
            self.detail_label.setText("Select a skill to see details.")
            return
        skill = self.registry.get(name)
        if skill is None:
            self.detail_label.setText("Skill not found.")
            return
        d = skill.definition
        enabled = self._is_skill_enabled(d.name)
        examples = "\n".join(f"  - {e}" for e in d.examples) or "  —"
        self.detail_label.setText(
            f"<b>{d.name}</b> ({d.category.value}) — {'Enabled' if enabled else 'Disabled'}<br/>"
            f"<i>{d.description}</i><br/>"
            f"<b>Triggers:</b> {', '.join(d.triggers) or '—'}<br/>"
            f"<b>Examples:</b><br/>{examples}"
        )

    def _toggle_selected(self) -> None:
        """Toggle the enabled state of the selected skill."""
        name = self._selected_skill_name()
        if name is None:
            QMessageBox.information(self, "Toggle", "Please select a skill first.")
            return
        current = self._is_skill_enabled(name)
        self._skill_state[name] = not current
        self._save_state()
        self._refresh_skill_list()
        # Reselect the item so the detail label stays in sync.
        for i in range(self.skill_list.count()):
            item = self.skill_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == name:
                self.skill_list.setCurrentRow(i)
                break

    # ── Testing ──────────────────────────────────────────────────────────

    def _test_skill(self) -> None:
        """Run the selected skill (or auto-detect) against the test query."""
        query = self.test_query_input.text().strip()
        if not query:
            QMessageBox.warning(self, "Test", "Please enter a test query.")
            return

        name = self._selected_skill_name()
        self.test_result.setPlainText("Running skill test...\n")

        try:
            if name:
                result = self.registry.execute(name, query)
            else:
                result = self.registry.execute_auto(query)
        except Exception as exc:
            self.test_result.setPlainText(f"Test failed with exception:\n{exc}")
            return

        output = self._format_skill_result(result, query)
        self.test_result.setPlainText(output)

    def _format_skill_result(self, result: SkillResult, query: str) -> str:
        """Format a SkillResult for display in the test output area."""
        lines = [
            f"Query: {query}",
            f"Skill: {result.skill_name}",
            f"Success: {result.success}",
        ]
        if result.error:
            lines.append(f"Error: {result.error}")
        if result.result:
            lines.append(
                f"Result: {json.dumps(result.result, indent=2, ensure_ascii=False, default=str)}"
            )
        if result.steps_taken:
            lines.append(f"Steps: {' → '.join(result.steps_taken)}")
        if result.metadata:
            lines.append(
                f"Metadata: {json.dumps(result.metadata, indent=2, ensure_ascii=False, default=str)}"
            )
        return "\n".join(lines)

    # ── Import / Export ──────────────────────────────────────────────────

    def _skill_definition_to_dict(self, definition: SkillDefinition) -> dict[str, Any]:
        """Serialise a SkillDefinition to a plain dict for JSON export."""
        return {
            "name": definition.name,
            "description": definition.description,
            "category": definition.category.value,
            "triggers": list(definition.triggers),
            "examples": list(definition.examples),
        }

    def _dict_to_skill_definition(self, data: dict[str, Any]) -> SkillDefinition:
        """Deserialise a plain dict into a SkillDefinition."""
        return SkillDefinition(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            category=SkillCategory(data.get("category", "conversation")),
            triggers=list(data.get("triggers", [])),
            examples=list(data.get("examples", [])),
        )

    def _import_skill(self) -> None:
        """Import a skill definition from a JSON file."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Skill Definition",
            str(Path.home()),
            "JSON (*.json);;All Files (*)",
        )
        if not path:
            return

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            definition = self._dict_to_skill_definition(data)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Import", f"Failed to read JSON:\n{exc}")
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Import", f"Invalid skill definition:\n{exc}")
            return

        if not definition.name:
            QMessageBox.warning(self, "Import", "Skill name is required.")
            return

        skill = LLMSkill(definition, self.llm_provider)
        self.registry.register(skill)
        self._refresh_skill_list()
        QMessageBox.information(self, "Import", f"Skill '{definition.name}' imported successfully.")

    def _export_skill(self) -> None:
        """Export the selected skill definition to a JSON file."""
        name = self._selected_skill_name()
        if name is None:
            QMessageBox.information(self, "Export", "Please select a skill first.")
            return
        skill = self.registry.get(name)
        if skill is None:
            QMessageBox.warning(self, "Export", "Selected skill not found.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Skill Definition",
            f"{name}.json",
            "JSON (*.json);;All Files (*)",
        )
        if not path:
            return

        try:
            data = self._skill_definition_to_dict(skill.definition)
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            QMessageBox.information(self, "Export", f"Skill exported to:\n{path}")
        except OSError as exc:
            QMessageBox.warning(self, "Export", f"Failed to write file:\n{exc}")

    # ── AI generation ────────────────────────────────────────────────────

    def _ai_generate_skill(self) -> None:
        """Generate a new skill definition from a natural-language description."""
        if self.llm_provider is None:
            QMessageBox.warning(
                self,
                "AI Generate",
                "No LLM provider is configured. Please configure a model connection first.",
            )
            return

        description, ok = QInputDialog.getText(
            self,
            "AI Generate Skill",
            "Describe the skill you want to create:\n"
            "(e.g. 'A skill that finds expired contracts and lists their expiry dates')",
        )
        if not ok or not description.strip():
            return

        self.generate_button.setEnabled(False)
        try:
            definition = self._call_llm_for_skill_definition(description.strip())
        except Exception as exc:
            QMessageBox.warning(self, "AI Generate", f"Failed to generate skill:\n{exc}")
            return
        finally:
            self.generate_button.setEnabled(True)

        if definition is None:
            QMessageBox.warning(
                self,
                "AI Generate",
                "The LLM did not return a valid skill definition. Please try again.",
            )
            return

        skill = LLMSkill(definition, self.llm_provider)
        self.registry.register(skill)
        self._refresh_skill_list()
        QMessageBox.information(
            self,
            "AI Generate",
            f"Skill '{definition.name}' generated and registered.\n"
            f"Description: {definition.description}",
        )

    def _call_llm_for_skill_definition(self, description: str) -> SkillDefinition | None:
        """Ask the LLM to produce a SkillDefinition JSON from *description*."""
        prompt = (
            "You are a skill definition generator for a document management agent.\n"
            "Create a skill definition as a JSON object with these fields:\n"
            '- "name": short snake_case identifier\n'
            '- "description": one-sentence description\n'
            '- "category": one of "retrieval", "analysis", "management", "extraction", "conversation"\n'
            '- "triggers": list of keywords that activate this skill\n'
            '- "examples": list of 2-3 example queries\n\n'
            f"User request: {description}\n\n"
            "Return ONLY the JSON object, no markdown fences, no explanation."
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": description},
        ]
        if hasattr(self.llm_provider, "chat_completion_sync"):
            raw = self.llm_provider.chat_completion_sync(messages)
        else:
            import asyncio

            raw = asyncio.run(self.llm_provider.chat_completion(messages))

        return self._parse_skill_json(raw)

    def _parse_skill_json(self, raw: str) -> SkillDefinition | None:
        """Parse an LLM response into a SkillDefinition, tolerating fences."""
        if not raw:
            return None
        text = raw.strip()
        # Strip markdown code fences if present.
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove the first fence line and the last fence line.
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return self._dict_to_skill_definition(data)
        except (ValueError, TypeError):
            return None

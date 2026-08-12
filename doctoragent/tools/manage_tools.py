"""Conversation-driven management tools.

Agent :class:`~doctoragent.model.tools.Tool`s that let the user manage prompt
templates, skills and custom experts **from within the chat**. They read/write
the same :class:`~doctoragent.workspace_config.WorkspaceConfig` store as the
management API, so anything changed in the chat is immediately visible in the
management interface.

Register via :func:`register_workspace_tools`.
"""

from __future__ import annotations

import logging
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


def _result(ok: bool, data: Any = None, error: str = "") -> ToolResult:
    return ToolResult(success=ok, data=data, error=error or None, tool_name="")


class _WorkspaceTool(Tool):
    name = ""
    description = ""
    category = "manage"
    parameters: list[dict[str, Any]] = []

    def __init__(self, store: Any) -> None:
        self.store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[ToolParameter(**p) for p in self.parameters],
            category=self.category,
        )


class ListPromptsTool(_WorkspaceTool):
    name = "list_prompts"
    description = "List all prompt templates in the workspace (names + summaries)."
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            items = self.store.list_prompts()
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(
            True,
            [
                {
                    "name": p["name"],
                    "description": p["description"],
                    "template": p["template"][:120],
                }
                for p in items
            ],
        )


class CreatePromptTool(_WorkspaceTool):
    name = "create_prompt"
    description = "Create or update a prompt template (system prompt) by name."
    parameters: list[dict[str, Any]] = [
        {"name": "name", "type": "string", "required": True, "description": "template name"},
        {
            "name": "template",
            "type": "string",
            "required": True,
            "description": "prompt text, may use {var}",
        },
        {"name": "description", "type": "string", "required": False, "description": "purpose"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            p = self.store.upsert_prompt(
                kwargs.get("name", ""),
                kwargs.get("template", ""),
                description=kwargs.get("description", ""),
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(True, {"ok": True, "prompt": p["name"]})


class UpdatePromptTool(_WorkspaceTool):
    name = "update_prompt"
    description = "Update an existing prompt template (alias of create_prompt)."
    parameters: list[dict[str, Any]] = [
        {"name": "name", "type": "string", "required": True, "description": "template name"},
        {"name": "template", "type": "string", "required": True, "description": "new prompt text"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            p = self.store.upsert_prompt(kwargs.get("name", ""), kwargs.get("template", ""))
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(True, {"ok": True, "prompt": p["name"]})


class ListSkillsTool(_WorkspaceTool):
    name = "list_skills"
    description = "List all custom skills in the workspace."
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            items = self.store.list_skills()
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(
            True,
            [
                {"name": s["name"], "description": s["description"], "triggers": s["triggers"]}
                for s in items
            ],
        )


class RegisterSkillTool(_WorkspaceTool):
    name = "register_skill"
    description = "Register a new custom skill (a reusable capability pack)."
    parameters: list[dict[str, Any]] = [
        {"name": "name", "type": "string", "required": True, "description": "skill name"},
        {
            "name": "description",
            "type": "string",
            "required": True,
            "description": "what the skill does",
        },
        {"name": "triggers", "type": "list", "required": False, "description": "trigger keywords"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            s = self.store.register_skill(
                kwargs.get("name", ""),
                kwargs.get("description", ""),
                triggers=kwargs.get("triggers"),
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(True, {"ok": True, "skill": s["name"]})


class ListExpertsTool(_WorkspaceTool):
    name = "list_experts"
    description = "List all custom experts (role presets)."
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            items = self.store.list_experts()
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(
            True,
            [
                {"name": e["name"], "title": e["title"], "system_prompt": e["system_prompt"][:120]}
                for e in items
            ],
        )


class CreateExpertTool(_WorkspaceTool):
    name = "create_expert"
    description = (
        "Create a custom expert: a named persona with its own system prompt and optional tools."
    )
    parameters: list[dict[str, Any]] = [
        {"name": "name", "type": "string", "required": True, "description": "expert name"},
        {"name": "title", "type": "string", "required": True, "description": "display title"},
        {
            "name": "system_prompt",
            "type": "string",
            "required": True,
            "description": "persona system prompt",
        },
        {"name": "tools", "type": "list", "required": False, "description": "tool names to enable"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            e = self.store.create_expert(
                kwargs.get("name", ""),
                kwargs.get("title", ""),
                kwargs.get("system_prompt", ""),
                tools=kwargs.get("tools"),
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))
        return _result(True, {"ok": True, "expert": e["name"]})


_TOOL_CLASSES = (
    ListPromptsTool,
    CreatePromptTool,
    UpdatePromptTool,
    ListSkillsTool,
    RegisterSkillTool,
    ListExpertsTool,
    CreateExpertTool,
)


def register_workspace_tools(registry: Any, store: Any) -> list[str]:
    """Register all conversation-management tools into *registry*."""
    names: list[str] = []
    for cls in _TOOL_CLASSES:
        tool = cls(store)
        if registry.get(tool.name) is None:
            registry.register(tool)
            names.append(tool.name)
    return names

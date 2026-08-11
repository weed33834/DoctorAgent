"""Conversation-driven clinical management tools.

Let a doctor do in plain conversation what would otherwise require clicking
through the console: switch specialty role, manage knowledge bases, import
documents, and inspect system status. Wired to the SAME backend services the
management UI uses, so conversational changes are immediately reflected.

Register via :func:`register_conversation_tools`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class ConversationToolsContext:
    """Duck-typed handle to the app services used by the tools.

    Built from ``request.app.state`` at registration so the tools read/write
    the same state the REST API and management UI use.
    """

    def __init__(self, state: Any, agent: Any = None) -> None:
        self.state = state
        self.agent = agent  # AegisAgent (for doc ingestion / status)

    def service(self, name: str) -> Any:
        return getattr(self.state, name, None)

    @property
    def kb(self) -> Any:
        return self.service("kb_manager")

    @property
    def ws(self) -> Any:
        return self.service("workspace_config")

    @property
    def pricing(self) -> Any:
        return self.service("pricing")

    @property
    def cost(self) -> Any:
        return self.service("cost_tracker")

    @property
    def role(self) -> str:
        return getattr(self.state, "clinical_role", "general") or "general"


class _CtxTool(Tool):
    name = ""
    description = ""
    category = "clinical_manage"
    parameters: list[dict[str, Any]] = []

    def __init__(self, ctx: ConversationToolsContext) -> None:
        self.ctx = ctx

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[ToolParameter(**p) for p in self.parameters],
            category=self.category,
        )

    def _ok(self, data: Any) -> ToolResult:
        return ToolResult(success=True, data=data)

    def _err(self, msg: str) -> ToolResult:
        return ToolResult(success=False, error=msg)


class SwitchRoleTool(_CtxTool):
    name = "switch_role"
    description = "切换当前临床科室角色（如 心内科/外科/麻醉/急诊/ICU/儿科/临床药师 等），之后回答将以该专科视角进行。"
    parameters: list[dict[str, Any]] = [
        {"name": "code", "type": "string", "required": True,
         "description": "角色代码：general/cardiology/surgery/anesthesia/emergency/icu/pediatrics/obgyn/neurology/respiratory/endocrinology/oncology/nephrology/gastroenterology/psychiatry/laboratory/radiology/pharmacy"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        from doctoragent.clinical.roles import get_role

        code = (kwargs.get("code") or "").strip().lower()
        role = get_role(code)
        if role is None:
            return self._err(f"未知角色 {code!r}；可用：general/cardiology/surgery/…")
        self.ctx.state.clinical_role = code
        if self.ctx.ws is not None:
            try:
                self.ctx.ws.set_settings({"clinical_role": code})
            except Exception:  # noqa: BLE001
                pass
        return self._ok({"role": role.code, "name": role.name, "prompt": role.prompt[:120]})


class ListKnowledgeBasesTool(_CtxTool):
    name = "list_knowledge_bases"
    description = "列出知识库中已创建的知识库（名称、可见范围、文档数）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        kb = self.ctx.kb
        if kb is None:
            return self._err("知识库服务未启用")
        try:
            items = kb.list()
            return self._ok([{"name": k["name"], "visibility": k["visibility"],
                              "docs": k["doc_count"], "embedding": k["embedding_model"]} for k in items])
        except Exception as exc:  # noqa: BLE001
            return self._err(str(exc))


class CreateKnowledgeBaseTool(_CtxTool):
    name = "create_knowledge_base"
    description = "创建一个知识库（命名分类用），例如「糖尿病资料库」。"
    parameters: list[dict[str, Any]] = [
        {"name": "name", "type": "string", "required": True, "description": "知识库名称"},
        {"name": "description", "type": "string", "required": False, "description": "用途说明"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        kb = self.ctx.kb
        if kb is None:
            return self._err("知识库服务未启用")
        try:
            row = kb.create(kwargs.get("name", ""), description=kwargs.get("description", ""))
            return self._ok({"created": True, "id": row["id"], "name": row["name"]})
        except Exception as exc:  # noqa: BLE001
            return self._err(str(exc))


class ImportDocumentTool(_CtxTool):
    name = "import_document"
    description = "把一个本地文档（PDF/DOCX/TXT 等）导入知识库；给出文件的绝对路径即可。"
    parameters: list[dict[str, Any]] = [
        {"name": "path", "type": "string", "required": True, "description": "文档绝对路径"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        src = Path(kwargs.get("path", "") or "")
        if not src.is_file():
            return self._err(f"文件不存在或不可读：{src}")
        # 目标：写入 Inbox 供管线处理；若可拿到 agent 则直接触发入库。
        cfg = self.ctx.service("config")
        inbox = getattr(getattr(cfg, "paths", None), "inbox", None)
        if inbox is None:
            return self._err("Inbox 路径未配置")
        inbox = Path(inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / src.name
        if dest.exists():
            dest = inbox / f"{src.stem}_1{src.suffix}"
        try:
            import shutil

            shutil.copy2(src, dest)
        except OSError as exc:  # noqa: BLE001
            return self._err(f"复制失败：{exc}")
        # 触发入库（若 agent 可用）
        if self.ctx.agent is not None and hasattr(self.ctx.agent, "on_file_event"):
            try:
                from doctoragent.api.schemas import FileEvent

                status = await self.ctx.agent.on_file_event(
                    FileEvent(event_id=__import__("uuid").uuid4(),
                              source_path=dest, event_type="created")
                )
                state = getattr(status, "state", "?")
                return self._ok({"imported": state == "COMPLETED", "path": str(dest), "state": state})
            except Exception as exc:  # noqa: BLE001
                return self._ok({"imported": False, "path": str(dest), "staged": True, "note": f"已放入收件箱，稍后自动入库（{exc}）"})
        return self._ok({"imported": True, "path": str(dest), "note": "已放入收件箱，将自动处理入库"})


class SystemStatusTool(_CtxTool):
    name = "system_status"
    description = "查看系统当前状态：科室角色、知识库数量、内置知识、模型、运行成本。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        from doctoragent.clinical.roles import get_role
        from doctoragent.clinical.knowledge import KNOWLEDGE_DOCS

        role = get_role(self.ctx.role)
        kb_count = 0
        if self.ctx.kb is not None:
            try:
                kb_count = len(self.ctx.kb.list())
            except Exception:  # noqa: BLE001
                pass
        model = "未配置"
        provider = getattr(self.ctx.agent, "llm_provider", None) or (
            getattr(getattr(self.ctx.agent, "classifier", None), "provider", None))
        if provider is not None:
            model = getattr(getattr(provider, "connection", None), "model_name", None) or "?"
        cost = 0.0
        if self.ctx.cost is not None:
            try:
                s = self.ctx.cost.get_summary()
                cost = s.get("total_cost_usd", 0.0) if isinstance(s, dict) else 0.0
            except Exception:  # noqa: BLE001
                pass
        return self._ok({
            "role": {"code": self.ctx.role, "name": role.name if role else self.ctx.role},
            "knowledge_bases": kb_count,
            "builtin_knowledge_docs": len(KNOWLEDGE_DOCS),
            "model": model,
            "estimated_cost_usd": round(cost, 4),
        })


_TOOLS = (SwitchRoleTool, ListKnowledgeBasesTool, CreateKnowledgeBaseTool,
          ImportDocumentTool, SystemStatusTool)


def register_conversation_tools(registry: Any, state: Any, agent: Any = None) -> list[str]:
    """Register all conversational clinical-management tools."""
    ctx = ConversationToolsContext(state, agent)
    names: list[str] = []
    for cls in _TOOLS:
        t = cls(ctx)
        if registry.get(t.name) is None:
            registry.register(t)
            names.append(t.name)
    return names

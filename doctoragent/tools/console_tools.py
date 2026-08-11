"""Console conversation tools — do EVERYTHING via chat.

Wraps the same backend services the management console uses, so a doctor or
admin can perform any console operation by simply describing it in chat:

* documents: list / search / delete
* models: list configured models / compare pricing / cost report
* config: view / set settings
* connections: list / add
* enterprise: org/user/api-key summary + user create
* memory: view / clear
* security: overview / red-team run / input scan
* system: health + version
* knowledge: seed built-in docs / list
* tasks: list task center

Register via :func:`register_console_tools`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class ConsoleContext:
    """Duck-typed handle to app services (built from request.app.state)."""

    def __init__(self, state: Any, agent: Any = None) -> None:
        self.state = state
        self.agent = agent

    def s(self, name: str) -> Any:
        return getattr(self.state, name, None)

    @property
    def task_store(self) -> Any:
        return self.s("task_store") or getattr(self.agent, "task_store", None)

    @property
    def config(self) -> Any:
        return self.s("config")

    @property
    def cm(self) -> Any:
        return getattr(self.agent, "connection_manager", None)


class _CTool(Tool):
    name = ""
    description = ""
    category = "console_ops"
    parameters: list[dict[str, Any]] = []

    def __init__(self, ctx: ConsoleContext) -> None:
        self.ctx = ctx

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description,
                              parameters=[ToolParameter(**p) for p in self.parameters],
                              category=self.category)

    def _ok(self, data: Any) -> ToolResult:
        return ToolResult(success=True, data=data)

    def _err(self, msg: str) -> ToolResult:
        return ToolResult(success=False, error=msg)

    def _safe(self, fn) -> ToolResult:
        try:
            return self._ok(fn())
        except Exception as exc:  # noqa: BLE001
            return self._err(str(exc))


# ── Documents ──────────────────────────────────────────────────────────


class ListDocumentsTool(_CTool):
    name = "list_documents"
    description = "列出文档库（Vault）中的文档，可按分类过滤。"
    parameters: list[dict[str, Any]] = [
        {"name": "category", "type": "string", "required": False, "description": "分类名（可选）"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            store = self.ctx.task_store
            if store is None or not hasattr(store, "list_vault_files"):
                return {"total": 0, "files": []}
            files = store.list_vault_files(kwargs.get("category"))
            return {"total": len(files), "files": [
                {"name": getattr(f, "name", str(f)), "path": getattr(f, "path", "")}
                for f in files[:100]
            ]}
        return self._safe(_run)


class SearchVaultTool(_CTool):
    name = "search_vault"
    description = "在文档库（Vault）中按关键词检索文档。"
    parameters: list[dict[str, Any]] = [
        {"name": "query", "type": "string", "required": True, "description": "检索词"},
        {"name": "top_k", "type": "integer", "required": False, "description": "返回条数"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            store = self.ctx.task_store
            if store is None or not hasattr(store, "search"):
                return {"total": 0, "results": []}
            return store.search(kwargs.get("query", ""), top_k=int(kwargs.get("top_k", 5)))
        return self._safe(_run)


# ── Models / cost ──────────────────────────────────────────────────────


class ListModelsTool(_CTool):
    name = "list_models"
    description = "列出已配置的模型连接（provider、模型名、状态）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            cm = self.ctx.cm
            if cm is None:
                return {"models": []}
            conns = cm.list_connections() if hasattr(cm, "list_connections") else []
            out = []
            for c in conns:
                out.append({
                    "name": getattr(c, "name", ""),
                    "model": getattr(c, "model_name", ""),
                    "provider": getattr(c, "platform_type", "").value if hasattr(getattr(c, "platform_type", ""), "value") else "",
                    "enabled": getattr(c, "is_enabled", False),
                })
            return {"models": out}
        return self._safe(_run)


class CompareModelsTool(_CTool):
    name = "compare_models"
    description = "对比多个模型的价格与上下文（模型比价器）。"
    parameters: list[dict[str, Any]] = [
        {"name": "models", "type": "list", "required": False, "description": "模型名列表，如 gpt-4o,deepseek-v3"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            pricing = self.ctx.s("pricing")
            if pricing is None:
                return {"models": []}
            names = kwargs.get("models") or ["gpt-4o", "deepseek-v3", "qwen2.5-7b"]
            return {"models": pricing.compare(names)}
        return self._safe(_run)


class CostReportTool(_CTool):
    name = "cost_report"
    description = "查看运行成本（今日/近7天/总额）与模型用量。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            ct = self.ctx.s("cost_tracker")
            if ct is None or not hasattr(ct, "get_summary"):
                return {"cost": "未启用"}
            summary = ct.get_summary()
            daily = ct.get_daily_costs(7) if hasattr(ct, "get_daily_costs") else []
            return {"summary": summary, "daily_days": len(daily)}
        return self._safe(_run)


# ── Config ─────────────────────────────────────────────────────────────


class ConfigViewTool(_CTool):
    name = "config_view"
    description = "查看当前系统配置（env、路径、模型、安全等）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            cfg = self.ctx.config
            if cfg is None:
                return {}
            return {
                "app": cfg.app_name, "env": cfg.env, "debug": cfg.debug,
                "model_provider": getattr(cfg.model, "provider", ""),
                "model_name": getattr(cfg.model, "name", ""),
            }
        return self._safe(_run)


class ConfigSetTool(_CTool):
    name = "config_set"
    description = "修改系统设置（键值对，持久化到工作区）。"
    parameters: list[dict[str, Any]] = [
        {"name": "key", "type": "string", "required": True, "description": "设置键"},
        {"name": "value", "type": "string", "required": True, "description": "设置值"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            ws = self.ctx.s("workspace_config")
            if ws is None or not hasattr(ws, "set_settings"):
                return {"ok": False, "error": "workspace config unavailable"}
            ws.set_settings({kwargs.get("key", ""): kwargs.get("value", "")})
            return {"ok": True, "key": kwargs.get("key"), "value": kwargs.get("value")}
        return self._safe(_run)


# ── Connections ────────────────────────────────────────────────────────


class ListConnectionsTool(_CTool):
    name = "list_connections"
    description = "列出所有模型连接（LLM 提供商）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            cm = self.ctx.cm
            if cm is None or not hasattr(cm, "list_connections"):
                return {"connections": []}
            return {"connections": [
                {"name": getattr(c, "name", ""), "model": getattr(c, "model_name", ""),
                 "enabled": getattr(c, "is_enabled", False)}
                for c in cm.list_connections()
            ]}
        return self._safe(_run)


# ── Enterprise ─────────────────────────────────────────────────────────


class EnterpriseSummaryTool(_CTool):
    name = "enterprise_summary"
    description = "查看企业平台概览（组织/用户/公告/API密钥）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            es = self.ctx.s("enterprise_service")
            if es is None:
                return {"enterprise": "未启用"}
            return {
                "orgs": len(es.list_orgs()),
                "users": es.store.count_users(),
                "announcements": len(es.list_announcements()),
                "maintenance": es.get_maintenance().enabled,
                "api_keys": len(es.list_api_keys()),
            }
        return self._safe(_run)


class ListUsersTool(_CTool):
    name = "list_users"
    description = "列出组织用户（角色/状态）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            es = self.ctx.s("enterprise_service")
            if es is None:
                return {"users": []}
            users = es.list_users()
            return {"users": [{"email": u.email, "role": u.role.value, "status": u.status.value} for u in users[:50]]}
        return self._safe(_run)


class CreateUserTool(_CTool):
    name = "create_user"
    description = "创建企业用户账号（邮箱+密码+角色）。"
    parameters: list[dict[str, Any]] = [
        {"name": "email", "type": "string", "required": True, "description": "邮箱"},
        {"name": "password", "type": "string", "required": True, "description": "密码（需含大小写+数字）"},
        {"name": "org_id", "type": "string", "required": False, "description": "组织ID（默认取第一个组织）"},
        {"name": "role", "type": "string", "required": False, "description": "member/org_admin"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        from doctoragent.enterprise import UserRole

        def _run():
            es = self.ctx.s("enterprise_service")
            if es is None:
                raise RuntimeError("企业服务未启用")
            orgs = es.list_orgs()
            org_id = kwargs.get("org_id") or (orgs[0].id if orgs else "default")
            role = UserRole(kwargs.get("role", "member"))
            u = es.create_user(org_id, kwargs.get("email", ""), kwargs.get("password", ""),
                               display_name=kwargs.get("display_name", ""), role=role)
            return {"created": True, "email": u.email, "org": org_id, "role": u.role.value}
        return self._safe(_run)


class ListApiKeysTool(_CTool):
    name = "list_api_keys"
    description = "列出企业 API 密钥。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            es = self.ctx.s("enterprise_service")
            if es is None:
                return {"keys": []}
            return {"keys": [{"id": k.id, "label": k.label, "prefix": k.prefix} for k in es.list_api_keys()]}
        return self._safe(_run)


# ── Memory ─────────────────────────────────────────────────────────────


class MemoryViewTool(_CTool):
    name = "memory_view"
    description = "查看长期记忆（事实/情景）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            mem = getattr(self.ctx.agent, "memory", None) or getattr(self.ctx.agent, "memory_system", None)
            if mem is None:
                return {"memory": "未启用"}
            try:
                facts = mem.recall_facts("", limit=10)
                return {"facts": [{"content": f.content, "type": f.memory_type} for f in facts]}
            except Exception:
                return {"facts": []}
        return self._safe(_run)


class MemoryClearTool(_CTool):
    name = "memory_clear"
    description = "清空长期记忆。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            mem = getattr(self.ctx.agent, "memory", None) or getattr(self.ctx.agent, "memory_system", None)
            if mem is None or not hasattr(mem, "consolidate_memories"):
                return {"ok": True, "note": "记忆系统不可用或无需清理"}
            # best-effort: prune
            try:
                mem.prune_memories(force=True)
            except Exception:
                pass
            return {"ok": True}
        return self._safe(_run)


# ── Security ───────────────────────────────────────────────────────────


class SecurityStatusTool(_CTool):
    name = "security_status"
    description = "查看安全态势（威胁用例/事件/拦截率/红队）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            ts = self.ctx.s("threat_service")
            if ts is None:
                return {"security": "未启用"}
            return ts.overview()
        return self._safe(_run)


class RunRedteamTool(_CTool):
    name = "run_redteam"
    description = "对护栏运行一次红队演练（跑威胁用例，报告拦截率/绕过）。"
    parameters: list[dict[str, Any]] = [
        {"name": "name", "type": "string", "required": False, "description": "演练名"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            ts = self.ctx.s("threat_service")
            if ts is None:
                raise RuntimeError("安全服务未启用")
            r = ts.run_redteam(kwargs.get("name", "chat-redteam"))
            return {"run_id": r["run_id"], "block_rate": r["report"]["block_rate"],
                    "cases": r["report"]["cases"], "bypass": r["report"]["bypass"]}
        return self._safe(_run)


# ── System / knowledge / tasks ─────────────────────────────────────────


class HealthStatusTool(_CTool):
    name = "health_status"
    description = "查看服务健康与版本。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        import doctoragent

        return self._ok({"status": "ok", "version": doctoragent.__version__})


class SeedKnowledgeTool(_CTool):
    name = "seed_knowledge"
    description = "把内置的医学基础知识写入知识库（Vault）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            from doctoragent.clinical.knowledge import seed_knowledge

            cfg = self.ctx.config
            vault = getattr(getattr(cfg, "paths", None), "vault", None)
            if vault is None:
                raise RuntimeError("vault path 未配置")
            n = seed_knowledge(Path(vault))
            return {"seeded": n, "note": "写入 Vault/临床知识/（已有文件不覆盖）"}
        return self._safe(_run)


class KnowledgeListTool(_CTool):
    name = "knowledge_list"
    description = "列出内置医学知识文档。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            from doctoragent.clinical.knowledge import list_knowledge

            return {"topics": [k["topic"] for k in list_knowledge()]}
        return self._safe(_run)


class TaskListTool(_CTool):
    name = "task_list"
    description = "列出后台任务中心的任务（导入/备份/重索引等）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        def _run():
            tc = self.ctx.s("task_center")
            if tc is None:
                return {"tasks": []}
            return {"tasks": [{"name": t["name"], "type": t["task_type"], "status": t["status"]} for t in tc.list(limit=20)]}
        return self._safe(_run)


_TOOLS = (
    ListDocumentsTool, SearchVaultTool, ListModelsTool, CompareModelsTool,
    CostReportTool, ConfigViewTool, ConfigSetTool, ListConnectionsTool,
    EnterpriseSummaryTool, ListUsersTool, CreateUserTool, ListApiKeysTool,
    MemoryViewTool, MemoryClearTool, SecurityStatusTool, RunRedteamTool,
    HealthStatusTool, SeedKnowledgeTool, KnowledgeListTool, TaskListTool,
)


def register_console_tools(registry: Any, state: Any, agent: Any = None) -> list[str]:
    """Register all console conversation tools."""
    ctx = ConsoleContext(state, agent)
    names: list[str] = []
    for cls in _TOOLS:
        t = cls(ctx)
        if registry.get(t.name) is None:
            registry.register(t)
            names.append(t.name)
    return names

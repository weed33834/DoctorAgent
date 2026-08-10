"""Error code system (M19 docs-support).

A structured, machine-readable error catalog: every API/agent error has a
stable code, an HTTP status, a human message and operator guidance. This gives
the platform a consistent "三段式" error contract (code + message + hint) and
is served at ``GET /api/v1/errors`` so clients and docs can discover codes.

Error codes follow a prefix scheme: ``E<domain><NNN>`` where domain is one of
AUTH / VALIDATION / MODEL / TOOL / CLINICAL / SYSTEM / VOICE / ENTERPRISE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ErrorInfo:
    code: str
    http_status: int
    message: str
    hint: str
    domain: str


ERROR_CATALOG: dict[str, ErrorInfo] = {
    # auth
    "EAUTH401": ErrorInfo("EAUTH401", 401, "认证失败", "请检查 API Token / OIDC 令牌是否有效或过期。", "auth"),
    "EAUTH403": ErrorInfo("EAUTH403", 403, "无权限执行该操作", "该操作需要更高权限（如 DOCTORAGENT_API_TOKEN 未配置时敏感端点被拒）。", "auth"),
    "EAUTH404": ErrorInfo("EAUTH404", 404, "账户不存在或已被禁用", "核对邮箱/组织 ID，或联系管理员启用账号。", "auth"),
    # validation
    "EVAL422": ErrorInfo("EVAL422", 422, "请求参数校验失败", "检查请求体是否符合接口 schema，字段类型/必填项是否正确。", "validation"),
    "EVAL400": ErrorInfo("EVAL400", 400, "无效的请求载荷", "检查 JSON 结构与非空约束。", "validation"),
    # model
    "EMODEL502": ErrorInfo("EMODEL502", 502, "模型服务不可用", "检查 LLM 连接（Ollama/网关）是否在线，模型名是否正确，密钥是否有效。", "model"),
    "EMODEL504": ErrorInfo("EMODEL504", 504, "模型调用超时", "增大超时，或检查模型负载；可切换 fallback 模型。", "model"),
    "EMODEL429": ErrorInfo("EMODEL429", 429, "超出模型限流/配额", "降低调用频率，检查预算与配额配置。", "model"),
    # tool
    "ETOOL500": ErrorInfo("ETOOL500", 500, "工具执行失败", "查看工具错误详情，确认参数与外部依赖（MCP/浏览器/网络）可用。", "tool"),
    "ETOOL404": ErrorInfo("ETOOL404", 404, "工具不存在或未注册", "确认工具已注册到 registry（如 MCP 连接后导入）。", "tool"),
    # clinical
    "ECLINIC400": ErrorInfo("ECLINIC400", 400, "临床请求参数缺失", "用药/过敏/生命体征/检验字段不完整，检查 patient_context。", "clinical"),
    "ECLINIC403": ErrorInfo("ECLINIC403", 403, "临床敏感操作被拒", "PHI 操作需要更高权限或令牌配置。", "clinical"),
    # system
    "ESYS503": ErrorInfo("ESYS503", 503, "依赖子系统未配置", "相关服务（记忆/治理/语音/企业）未启用，检查配置与环境变量。", "system"),
    "ESYS500": ErrorInfo("ESYS500", 500, "内部错误", "查看服务日志定位堆栈，必要时提交 issue。", "system"),
    # voice
    "EVOICE501": ErrorInfo("EVOICE501", 501, "语音能力未配置", "设置 DOCTORAGENT_VOICE__*_BASE_URL / *_MODEL 环境变量。", "voice"),
    "EVOICE400": ErrorInfo("EVOICE400", 400, "语音请求无效", "检查音频格式、大小与 text 参数。", "voice"),
    # enterprise
    "EENT400": ErrorInfo("EENT400", 400, "企业平台参数错误", "如用户已存在/密码不合规/scope_id 缺失，查看 detail。", "enterprise"),
    "EENT409": ErrorInfo("EENT409", 409, "资源冲突", "如邮箱已存在或重复创建，改用更新接口。", "enterprise"),
}


def lookup(code: str) -> ErrorInfo | None:
    return ERROR_CATALOG.get(code)


def catalog() -> list[dict[str, Any]]:
    return [
        {"code": e.code, "http_status": e.http_status, "message": e.message,
         "hint": e.hint, "domain": e.domain}
        for e in ERROR_CATALOG.values()
    ]


def http_status_for_code(code: str) -> int:
    info = lookup(code)
    return info.http_status if info else 500

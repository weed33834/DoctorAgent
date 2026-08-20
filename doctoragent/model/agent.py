"""Agent framework for DoctorAgent - implements reasoning loop and tool calling.

Agent architecture built on top of the original ReAct loop:

- **Plan-and-Execute**: structured, dependency-ordered execution plans with
  validation, dynamic re-planning and deviation tracking (replaces the old
  "inject plan into system message" pseudo-planning).
- **Deep reflection**: LLM-based multi-dimensional scoring (1-5) of the
  produced answer with up to ``max_reflection_rounds`` supplementary
  execution rounds focused on the weakest dimension.
- **Four-layer memory integration**: short-term conversation window, working
  memory during execution, episodic trajectory storage and long-term fact
  recall/storage wired into :class:`~doctoragent.model.rag.MemorySystem`.
- **Parallel tool execution**: independent tool calls are dispatched
  concurrently via :func:`asyncio.gather` with resource-based dependency
  detection.
- **Multi-agent collaboration**: :class:`OrchestratorAgent` /
  :class:`WorkerAgent` orchestrator-worker pattern backed by the
  ``TaskStore`` subtask API.
- **Error recovery & circuit breaker**: per-tool retry with exponential
  backoff, error classification (transient/permanent/fatal), same-category
  alternative tools and session-scoped circuit breakers.
- **Smart tool-result truncation**: dynamic token-budget aware truncation
  preserving head, tail and query-keyword paragraphs.

Backward compatibility: when ``enable_planning=False``,
``enable_reflection=False`` and ``enable_multi_agent=False`` the agent
reduces to the original ReAct loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from doctoragent._utils import async_to_sync

# Checkpoint persistence (lazy import to avoid module-load cycles).
from doctoragent.agent.checkpoint import AgentCheckpoint, CheckpointStore

from .provider import ChatCompletionResponse
from .tools import ToolChoice, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token estimation (lightweight, self-contained)
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Estimate the token count of *text*.

    Uses tiktoken's ``cl100k_base`` encoding when available (mirroring
    :mod:`doctoragent.model.rag`), falling back to the ``len(text) // 4``
    heuristic. Kept local to avoid importing from ``rag`` (which is being
    modified independently).
    """
    if not text:
        return 0
    # Cache BOTH success and failure so an offline / rate-limited tiktoken
    # download is not retried on every call (which blocks for seconds).
    # See issue #8.
    enc = getattr(_estimate_tokens, "_enc", None)
    if enc is None and not getattr(_estimate_tokens, "_enc_tried", False):
        _estimate_tokens._enc_tried = True  # type: ignore[attr-defined]
        try:
            import tiktoken

            _estimate_tokens._enc = tiktoken.get_encoding("cl100k_base")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - tiktoken unavailable / download failed
            _estimate_tokens._enc = False  # type: ignore[attr-defined]
        enc = getattr(_estimate_tokens, "_enc", None)
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


def _extract_json(text: str) -> Any:
    """Delegate to the shared :func:`extract_json` in :mod:`doctoragent._utils`.

    Kept as a thin wrapper for backward compatibility — 8 other modules
    import ``_extract_json`` from here.
    """
    from doctoragent._utils import extract_json

    return extract_json(text)


def _truncate_args(args: Any, max_len: int = 80) -> str:
    """Render tool-call arguments as a short single-line string for logs."""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# ---------------------------------------------------------------------------
# Error classification (error-recovery layer)
# ---------------------------------------------------------------------------


class ErrorClass(str, Enum):
    """Classification of a tool execution error."""

    TRANSIENT = "transient"  # network/timeout -> retryable
    PERMANENT = "permanent"  # bad args / not found -> not retryable
    FATAL = "fatal"  # system error -> abort the run


_FATAL_KEYWORDS = (
    "fatal",
    "critical",
    "out of memory",
    "segfault",
    "system error",
)
_PERMANENT_KEYWORDS = (
    "not found",
    "invalid",
    "not initialized",
    "argument",
    "parameter",
    "missing",
    "typeerror",
    "valueerror",
    "unsupported",
    "no such",
    "permission denied",
    "readonly",
)
_TRANSIENT_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporarily",
    "unavailable",
    "503",
    "502",
    "429",
    "reset",
    "busy",
    "again",
    "try again",
    "deadline",
)


def _classify_error(message: str) -> ErrorClass:
    """Classify an error message into :class:`ErrorClass`."""
    msg = (message or "").lower()
    if any(k in msg for k in _FATAL_KEYWORDS):
        return ErrorClass.FATAL
    if any(k in msg for k in _TRANSIENT_KEYWORDS):
        return ErrorClass.TRANSIENT
    if any(k in msg for k in _PERMANENT_KEYWORDS):
        return ErrorClass.PERMANENT
    # Unknown errors default to transient (worth one retry).
    return ErrorClass.TRANSIENT


def _incr_agent_iteration_metric(task_id: str, outcome: str) -> None:
    """Increment ``doctoragent_agent_iterations`` counter.

    Safe no-op when prometheus_client is absent (the metric is an in-process
    stub) or when observability failed to import for any reason.
    """
    try:
        from doctoragent.observability.metrics import doctoragent_agent_iterations

        doctoragent_agent_iterations.labels(task_id=task_id, outcome=outcome).inc()
    except Exception:  # noqa: BLE001 - metrics must never break agent loop
        pass


def _incr_error_metric(component: str) -> None:
    """Increment ``doctoragent_errors_total`` counter for *component*.

    Safe no-op when prometheus_client is absent.
    """
    try:
        from doctoragent.observability.metrics import doctoragent_errors_total

        doctoragent_errors_total.labels(component=component).inc()
    except Exception:  # noqa: BLE001 - metrics must never break error path
        pass


def _agent_run_task_id(agent: Any) -> str:
    """Return the current run-scoped task id of *agent* (or ``"unknown"``)."""
    return str(getattr(agent, "_current_task_id", "unknown") or "unknown")


# ---------------------------------------------------------------------------
# Agent State
# ---------------------------------------------------------------------------


class AgentState(str, Enum):
    """Current state of the agent."""

    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    FINISHED = "finished"
    ERROR = "error"


class StepType(str, Enum):
    """Type of step in the agent trajectory."""

    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    ANSWER = "answer"
    PLANNING = "planning"
    REFLECTION = "reflection"


class AgentStep(BaseModel):
    """Single step in agent execution."""

    step_type: StepType
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: ToolResult | None = None
    timestamp: float = 0


class AgentTrajectory(BaseModel):
    """Complete trajectory of agent execution."""

    steps: list[AgentStep] = Field(default_factory=list)
    total_tokens_used: int = 0
    total_tool_calls: int = 0
    total_time_ms: float = 0
    # Deep-reflection log: one ReflectionScore (dict) per reflection round.
    reflection_log: list[dict[str, Any]] = Field(default_factory=list)
    # Plan-vs-actual deviation records (Plan-and-Execute layer).
    plan_deviations: list[dict[str, Any]] = Field(default_factory=list)

    def add_step(self, step: AgentStep) -> None:
        self.steps.append(step)

    def get_thoughts(self) -> list[str]:
        return [s.content for s in self.steps if s.step_type == StepType.THOUGHT]

    def get_tool_calls(self) -> list[dict[str, Any]]:
        return [
            {
                "tool": s.tool_name,
                "args": s.tool_args,
                "result": s.tool_result.data if s.tool_result else None,
            }
            for s in self.steps
            if s.step_type == StepType.ACTION
        ]


# ---------------------------------------------------------------------------
# Plan-and-Execute data models
# ---------------------------------------------------------------------------


class PlanStepStatus(str, Enum):
    """Lifecycle status of a single plan step."""

    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """A single step in a structured execution plan."""

    step_id: str
    description: str = ""
    tool_hint: str = ""
    depends_on: list[str] = Field(default_factory=list)
    expected_output: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING
    actual_output: str = ""
    deviation: str = ""


class ExecutionPlan(BaseModel):
    """A structured, dependency-ordered execution plan."""

    steps: list[PlanStep] = Field(default_factory=list)
    created_at: float = 0
    # Number of times the plan has been regenerated mid-execution.
    replan_count: int = 0

    def get_step(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def pending_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == PlanStepStatus.PENDING]


# ---------------------------------------------------------------------------
# Deep reflection data models
# ---------------------------------------------------------------------------

_REFLECTION_DIMENSIONS = (
    "accuracy",
    "completeness",
    "relevance",
    "source_support",
)


class ReflectionScore(BaseModel):
    """Multi-dimensional LLM score (1-5) of a produced answer."""

    accuracy: float = 3.0
    completeness: float = 3.0
    relevance: float = 3.0
    source_support: float = 3.0
    critique: str = ""
    overall: float = 3.0
    lowest_dimension: str = "completeness"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReflectionScore:
        """Build a :class:`ReflectionScore` from a parsed LLM JSON dict.

        Tolerates missing keys and clamps each dimension to ``[1, 5]``.
        ``overall`` defaults to the mean of the dimensions when the LLM does
        not provide it.
        """

        def _dim(key: str) -> float:
            val = data.get(key, data.get(key, 3.0))
            try:
                v = float(val)
            except (TypeError, ValueError):
                v = 3.0
            return max(1.0, min(5.0, v))

        accuracy = _dim("accuracy")
        completeness = _dim("completeness")
        relevance = _dim("relevance")
        source_support = _dim("source_support")
        try:
            overall = float(data.get("overall", 0))
        except (TypeError, ValueError):
            overall = 0.0
        if overall <= 0:
            overall = (accuracy + completeness + relevance + source_support) / 4
        dims = {
            "accuracy": accuracy,
            "completeness": completeness,
            "relevance": relevance,
            "source_support": source_support,
        }
        lowest = min(dims, key=dims.get)  # type: ignore[arg-type]
        return cls(
            accuracy=accuracy,
            completeness=completeness,
            relevance=relevance,
            source_support=source_support,
            critique=str(data.get("critique", data.get("reasoning", ""))),
            overall=round(overall, 2),
            lowest_dimension=lowest,
        )

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "completeness": self.completeness,
            "relevance": self.relevance,
            "source_support": self.source_support,
            "overall": self.overall,
            "lowest_dimension": self.lowest_dimension,
            "critique": self.critique,
        }


# ---------------------------------------------------------------------------
# Agent Configuration
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Configuration for agent behavior.

    New flags default so that an ``AgentConfig()`` with no overrides keeps
    the legacy behaviour (planning + reflection on, multi-agent off, modest
    recovery). The GUI dialog constructs ``AgentConfig`` with a subset of
    these fields, so every new field is optional with a safe default.
    """

    # Original knobs (unchanged defaults).
    max_iterations: int = 10
    max_tool_calls: int = 5
    tool_choice: ToolChoice = ToolChoice.AUTO
    temperature: float = 0.7
    max_tokens: int = 2000
    enable_planning: bool = True
    enable_reflection: bool = True
    safety_mode: bool = True

    # --- Plan-and-Execute ---
    max_plan_steps: int = 8
    max_replans: int = 1  # number of dynamic re-plans allowed per run

    # --- Deep reflection ---
    max_reflection_rounds: int = 3  # supplementary execution rounds (was 1)
    reflection_threshold: float = 3.0  # overall score below this triggers a round

    # --- Memory integration ---
    enable_memory: bool = True
    short_term_window: int = 5  # recent turns injected into the system prompt

    # --- Parallel tool execution ---
    enable_parallel_tools: bool = True

    # --- Error recovery & circuit breaker ---
    max_tool_retries: int = 2
    circuit_breaker_threshold: int = 3

    # --- Multi-agent collaboration ---
    enable_multi_agent: bool = False
    max_subtasks: int = 5  # cap on worker subtasks per orchestration
    worker_timeout: float = 120.0  # 单个 worker 超时时间（秒）

    # --- Checkpoint persistence ---
    # Off by default for backward compatibility; opt in to save/resume.
    enable_checkpointing: bool = False

    # --- LLM 调用超时与运行中周期性 checkpoint ---
    # 单次 LLM 调用超时（秒）：用 asyncio.wait_for 包裹，防止流式输出永久挂起
    llm_timeout: float = 120.0
    # ReAct 循环中每 N 轮迭代自动保存一次 checkpoint，运行中崩溃可从中断点恢复
    checkpoint_interval: int = 5


# ---------------------------------------------------------------------------
# Agent Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个智能文档管理助手，帮助用户管理、搜索和分析他们的加密文档库。

你可以使用以下工具：
{tools_description}

## 工作原理

当你收到用户的问题时，你应该：
1. **思考** - 分析用户需要什么信息
2. **行动** - 选择合适的工具来获取信息
3. **观察** - 分析工具返回的结果
4. **回答** - 基于观察结果给出最终答案

## 重要规则

1. 总是先搜索文档再回答问题
2. 如果没有找到相关信息，诚实告知用户
3. 引用来源时要准确
4. 保护用户隐私，不要泄露敏感信息
5. 对于复杂问题，可以使用多个工具来收集信息

## 工具使用示例

用户问："我的合同什么时候到期？"
1. 思考：需要查找合同文件
2. 行动：调用 search_documents 搜索"合同 到期"
3. 观察：找到合同文件，包含到期日期
4. 回答：基于文档内容回答
"""

# Injected just after the base system prompt when memory is available.
MEMORY_PROMPT_SECTION = """
## 相关记忆

{memory_context}
"""

# Injected when there is recent short-term conversation history.
SHORT_TERM_PROMPT_SECTION = """
## 近期对话

{short_term_history}
"""

PLANNING_PROMPT = """作为文档管理专家，请为以下任务制定结构化执行计划。

用户任务：{task}

可用工具：{tools_list}

请输出一个 JSON 格式的执行计划，包含一个 "steps" 数组，每个步骤包含：
- step_id: 步骤唯一标识（如 "s1"）
- description: 该步骤要做什么
- tool_hint: 建议使用的工具名（如不确定可填 "" 或 "none"）
- depends_on: 前置步骤的 step_id 列表（无依赖则空数组）
- expected_output: 该步骤预期得到的结果

最多 {max_steps} 步。只输出 JSON，不要多余解释。

示例：
```json
{{
    "steps": [
        {{"step_id": "s1", "description": "搜索相关合同文档", "tool_hint": "search_documents", "depends_on": [], "expected_output": "合同文件列表"}},
        {{"step_id": "s2", "description": "分析合同到期日期", "tool_hint": "analyze_document", "depends_on": ["s1"], "expected_output": "到期日期"}}
    ]
}}
```"""

REPLAN_PROMPT = """在执行计划时，某一步骤未能按预期完成，需要重新规划剩余步骤。

用户任务：{task}
失败步骤：{failed_step}（{failed_desc}）
失败原因/实际结果：{failure_detail}
已完成步骤及其结果：
{completed_summary}

请输出一个新的 JSON 执行计划，仅包含**尚未完成**的步骤（包含因失败而需要重做的步骤）。
每个步骤仍需包含 step_id, description, tool_hint, depends_on, expected_output。
只输出 JSON。"""

SYNTHESIS_PROMPT = """你已经按计划完成了若干步骤并收集了以下中间结果。请基于这些结果，综合回答用户的原始问题。

用户原始问题：{task}

各步骤结果：
{results_summary}

请给出最终答案，必要时引用相关来源。"""

REFLECTION_PROMPT = """请对以下回答做多维度质量评分（每项 1-5 分，5 分最佳）。

用户问题：{question}
你的回答：{answer}
使用的工具：{tools_used}

请从以下四个维度评分，并给出简要评语：
- accuracy（准确性）：回答是否事实正确
- completeness（完整性）：是否覆盖了用户问题的所有方面
- relevance（相关性）：回答是否切题
- source_support（来源支撑度）：回答是否有工具/文档结果支撑

请严格以 JSON 格式输出，字段：accuracy, completeness, relevance, source_support, overall（1-5），critique（字符串）。只输出 JSON。
示例：
```json
{{"accuracy": 4, "completeness": 2, "relevance": 5, "source_support": 3, "overall": 3.5, "critique": "完整性不足，缺少到期日期"}}
```"""

REFINE_PROMPT = """反思评分显示你的回答在【{dimension}】维度不足（得分 {score}/5）。
评语：{critique}

请针对该维度补充信息或修正回答；如有必要请再次调用工具收集证据。"""

ORCHESTRATOR_DECOMPOSE_PROMPT = """你是一个任务分解专家。请将以下复杂任务分解为若干可以独立执行的子任务。

任务：{task}

请输出一个 JSON 数组，每个元素是一个子任务的描述字符串（最多 {max_subtasks} 个，按执行顺序排列）。只输出 JSON 数组。
示例：["子任务1的描述", "子任务2的描述"]"""

ORCHESTRATOR_SYNTHESIS_PROMPT = """你是协调者。多个 worker 分别完成了以下子任务，请将它们的结果综合为对原始任务的最终回答。

原始任务：{task}

各子任务结果：
{results}

请给出综合后的最终答案。"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Main agent class implementing the ReAct architecture.

    The agent supports an optional :class:`~doctoragent.model.rag.MemorySystem`
    and ``task_store``. When ``enable_multi_agent`` is set and a
    ``task_store`` is provided, :meth:`run` delegates to the orchestrator
    worker pattern; otherwise it runs the single-agent
    plan/ReAct/reflection pipeline.
    """

    def __init__(
        self,
        llm_provider: Any,
        tool_registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_system: Any = None,
        task_store: Any = None,
        checkpoint_store: CheckpointStore | None = None,
        session_id: str | None = None,
    ):
        self.llm_provider = llm_provider
        self.tools = tool_registry
        # Keep the registry's circuit-breaker threshold in sync with config.
        self.tools.circuit_threshold = config.circuit_breaker_threshold if config else 3
        self.config = config or AgentConfig()
        self.memory_system = memory_system
        self.task_store = task_store
        # 外部传入的会话ID，用于多轮对话记忆关联
        self.session_id = session_id
        # Optional checkpoint store so the final trajectory can be persisted
        # on shutdown via :meth:`aclose` (otherwise save_checkpoint is never
        # invoked and in-flight runs are lost when the process exits).
        self.checkpoint_store = checkpoint_store
        self.trajectory = AgentTrajectory()
        self.state = AgentState.IDLE
        # Per-run ephemeral state (reset in run()).
        self._working_memory: dict[str, Any] = {}
        self._short_term_history: list[dict[str, str]] = []
        self._tool_calls_used = 0
        # Per-run task identifier for metrics/checkpoint tagging. Stamped in
        # ``_run_single`` (and ``_run_multi_agent``); defaults to "unknown" so
        # the metrics helper always has a stable label.
        self._current_task_id: str = "unknown"
        # LLM 调用超时（秒）：用于 asyncio.wait_for 包裹，防止流式输出永久挂起
        self._llm_timeout: float = self.config.llm_timeout
        # ReAct 循环中自动保存 checkpoint 的迭代间隔（每 N 轮保存一次）
        self._checkpoint_interval: int = self.config.checkpoint_interval
        # 当前执行计划（_generate_plan 生成后留存于 self，便于 checkpoint 保存/恢复）
        self._plan: ExecutionPlan | None = None

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _format_tools_for_prompt(self) -> str:
        """Format tool descriptions for system prompt."""
        tools = self.tools.list_tools()
        descriptions = []
        for tool in tools:
            params = ", ".join(
                f"{p.name} ({p.type}){'[可选]' if not p.required else ''}" for p in tool.parameters
            )
            descriptions.append(f"- **{tool.name}**: {tool.description}\n  参数: {params}")
        return "\n\n".join(descriptions)

    def _build_system_prompt(
        self,
        memory_context: str = "",
        short_term_history: str = "",
    ) -> str:
        """Build the complete system prompt.

        ``memory_context`` (long-term facts + episodic recall) and
        ``short_term_history`` (recent conversation turns) are appended when
        present so the LLM can leverage prior context.
        """
        tools_desc = self._format_tools_for_prompt()
        prompt = SYSTEM_PROMPT.format(tools_description=tools_desc)
        if memory_context:
            prompt += MEMORY_PROMPT_SECTION.format(memory_context=memory_context)
        if short_term_history:
            prompt += SHORT_TERM_PROMPT_SECTION.format(short_term_history=short_term_history)
        return prompt

    def _format_short_term(self) -> str:
        """Render the recent conversation turns for the system prompt."""
        if not self._short_term_history:
            return ""
        window = self._short_term_history[-self.config.short_term_window :]
        return "\n".join(f"{t['role']}: {t['content']}" for t in window)

    # ------------------------------------------------------------------
    # Tool-call parsing
    # ------------------------------------------------------------------

    def _parse_tool_calls(self, response: str) -> list[dict[str, Any]]:
        """Parse tool calls from LLM response."""
        tool_calls = []

        # Try to parse as JSON
        try:
            data = json.loads(response)
            if isinstance(data, dict) and "tool_calls" in data:
                return data["tool_calls"]
        except json.JSONDecodeError:
            pass

        # Look for tool call patterns in text
        pattern = r"(\w+)\((.*?)\)"
        matches = re.findall(pattern, response)

        for tool_name, args_str in matches:
            if self.tools.get(tool_name):
                args = {}
                if args_str:
                    for pair in args_str.split(","):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            value = value.strip().strip('"').strip("'")
                            try:
                                value = json.loads(value)
                            except (json.JSONDecodeError, ValueError):
                                pass
                            args[key.strip()] = value
                tool_calls.append({"name": tool_name, "arguments": args})

        return tool_calls

    # ------------------------------------------------------------------
    # Layer 2: Memory integration (记忆层)
    # ------------------------------------------------------------------

    def _recall_memory(self, query: str) -> str:
        """Recall long-term facts and episodic memory for *query*."""
        if not (self.config.enable_memory and self.memory_system):
            return ""
        parts: list[str] = []
        try:
            facts = self.memory_system.recall_facts(query, limit=5)
            if facts:
                parts.append("长期记忆:\n" + "\n".join(f"- {f.content}" for f in facts))
        except Exception as e:  # noqa: BLE001
            logger.debug("recall_facts failed: %s", e)
        try:
            episodes = self.memory_system.recall_episodes(query, limit=3)
            if episodes:
                parts.append(
                    "历史交互:\n"
                    + "\n".join(
                        f"- 用户曾问: {(e.get('user_message') or '')[:80]}" for e in episodes
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("recall_episodes failed: %s", e)
        return "\n\n".join(parts)

    def _extract_facts(self, query: str, answer: str) -> list[str]:
        """Heuristically extract key facts for long-term storage."""
        facts: list[str] = []
        patterns = [
            r"(?:包含|提到|说明|描述了?|记录了?|到期|有效期|金额|总计)(.{5,60})",
            r"(?:日期|时间|金额|数量|有效期)(?:是|为|等于)(.{3,40})",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, answer)
            facts.extend(matches[:2])
        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique = []
        for f in facts:
            f = f.strip().rstrip("。.;；")
            if f and f not in seen:
                seen.add(f)
                unique.append(f)
        return unique[:5]

    def _store_memory(self, query: str, answer: str) -> None:
        """Persist the completed interaction to episodic + long-term memory."""
        if not (self.config.enable_memory and self.memory_system):
            return
        try:
            # 复用外部传入的 session_id，没有则创建新的
            session_id = self.session_id or self.memory_system.create_session()
            facts = self._extract_facts(query, answer)
            self.memory_system.store_episode(
                session_id=session_id,
                user_message=query,
                assistant_response=answer,
                context_summary=answer[:200],
                key_facts=facts,
            )
            # 持久化对话轮次，使后端记忆系统完整记录多轮对话
            self.memory_system.add_turn(session_id, "user", query)
            self.memory_system.add_turn(session_id, "assistant", answer)
            for fact in facts:
                self.memory_system.store_fact(fact, importance=0.6)
        except Exception as e:  # noqa: BLE001
            logger.debug("store_memory failed: %s", e)

    def _working_memory_summary(self) -> str:
        """Render working-memory entries for prompt injection."""
        if not self._working_memory:
            return "(尚无已完成步骤)"
        lines = []
        for key, value in self._working_memory.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines.append(f"[{key}] {text[:300]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Layer 3: Plan-and-Execute (推理规划层)
    # ------------------------------------------------------------------

    async def _generate_plan(self, query: str, messages: list[dict[str, Any]]) -> ExecutionPlan:
        """Generate a structured execution plan via the LLM."""
        tools_list = self._format_tools_for_prompt() or "(暂无可用工具)"
        prompt = PLANNING_PROMPT.format(
            task=query,
            tools_list=tools_list,
            max_steps=self.config.max_plan_steps,
        )
        plan_messages = [
            {"role": "system", "content": "你是一个严谨的任务规划专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            plan_response = await self._llm_chat(plan_messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Planning LLM call failed: %s", e)
            plan_response = ""

        steps = self._parse_plan_steps(plan_response)
        plan = ExecutionPlan(steps=steps, created_at=time.time())
        plan = self._validate_plan(plan)

        self._add_step(StepType.PLANNING, f"为任务生成执行计划: {query}")
        for step in plan.steps:
            self._add_step(
                StepType.PLANNING,
                f"步骤 {step.step_id}: 调用 {step.tool_hint or '(无工具)'} - {step.description}",
            )
        # Inject the validated plan as guidance for subsequent LLM calls.
        if plan.steps:
            plan_text = "\n".join(
                f"{s.step_id}. [{s.tool_hint or '无工具'}] {s.description} "
                f"(依赖: {s.depends_on or '无'}) -> 期望: {s.expected_output}"
                for s in plan.steps
            )
            messages.append(
                {
                    "role": "system",
                    "content": "已生成结构化执行计划，将按依赖顺序逐步执行：\n" + plan_text,
                }
            )
        return plan

    @staticmethod
    def _parse_plan_steps(response: str) -> list[PlanStep]:
        """Parse an LLM plan response into :class:`PlanStep` objects."""
        data = _extract_json(response)
        if data is None:
            return []
        raw_steps = data.get("steps", data) if isinstance(data, dict) else data
        if not isinstance(raw_steps, list):
            return []
        steps: list[PlanStep] = []
        for i, item in enumerate(raw_steps):
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("step_id") or item.get("step") or f"s{i + 1}")
            depends = item.get("depends_on") or item.get("depends") or []
            if not isinstance(depends, list):
                depends = [str(depends)]
            steps.append(
                PlanStep(
                    step_id=step_id,
                    description=str(item.get("description") or item.get("purpose") or ""),
                    tool_hint=str(item.get("tool_hint") or item.get("tool") or ""),
                    depends_on=[str(d) for d in depends],
                    expected_output=str(item.get("expected_output") or ""),
                )
            )
        return steps

    def _validate_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Validate feasibility: clear unknown tool hints and break cycles."""
        known_tools = {t.name for t in self.tools.list_tools()}
        for step in plan.steps:
            hint = step.tool_hint.strip()
            if hint and hint.lower() not in ("none", "llm", "") and hint not in known_tools:
                # Unknown tool -> clear the hint so the LLM picks freely.
                self._record_deviation(
                    step.step_id,
                    f"工具提示 '{hint}' 不存在，已清除",
                )
                step.tool_hint = ""
        # Break dependency cycles by ignoring back-edges (topological order).
        ordered = self._topological_order(plan)
        # Re-seat steps in topological order for deterministic execution.
        plan.steps = ordered
        return plan

    def _topological_order(self, plan: ExecutionPlan) -> list[PlanStep]:
        """Return plan steps in dependency order (Kahn's algorithm).

        Steps involved in a cycle are appended in their original order so the
        plan remains executable rather than deadlocking.
        """
        by_id = {s.step_id: s for s in plan.steps}
        indeg: dict[str, int] = {s.step_id: 0 for s in plan.steps}
        adj: dict[str, list[str]] = {s.step_id: [] for s in plan.steps}
        for s in plan.steps:
            for dep in s.depends_on:
                if dep in by_id and dep != s.step_id:
                    adj[dep].append(s.step_id)
                    indeg[s.step_id] += 1
        # Preserve original insertion order for deterministic ties.
        queue = [s.step_id for s in plan.steps if indeg[s.step_id] == 0]
        result: list[PlanStep] = []
        while queue:
            sid = queue.pop(0)
            result.append(by_id[sid])
            for nxt in adj[sid]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        # Append any cyclic nodes in original order.
        if len(result) < len(plan.steps):
            placed = {s.step_id for s in result}
            for s in plan.steps:
                if s.step_id not in placed:
                    self._record_deviation(s.step_id, "检测到依赖环，已按原序保留以避免死锁")
                    result.append(s)
        return result

    def _record_deviation(self, step_id: str, deviation: str) -> None:
        """Record a plan-vs-actual deviation on the trajectory."""
        self.trajectory.plan_deviations.append({"step_id": step_id, "deviation": deviation})

    async def _execute_plan(
        self,
        query: str,
        plan: ExecutionPlan,
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]] | None,
    ) -> str:
        """Execute a validated plan step-by-step in dependency order."""
        if not plan.steps:
            # Empty plan -> fall back to a plain ReAct loop.
            return await self._react_loop(messages, tools_spec)

        replans_left = self.config.max_replans
        i = 0
        while i < len(plan.steps):
            step = plan.steps[i]
            if step.status in (PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED):
                i += 1
                continue

            step.status = PlanStepStatus.EXECUTING
            self._add_step(
                StepType.PLANNING,
                f"开始执行步骤 {step.step_id}: {step.description}",
            )

            try:
                output = await self._execute_step(step, messages, tools_spec)
            except Exception as e:  # noqa: BLE001
                logger.warning("Step %s raised: %s", step.step_id, e)
                output = ""

            step.actual_output = output
            self._working_memory[step.step_id] = output

            if self._step_succeeded(step, output):
                step.status = PlanStepStatus.COMPLETED
                self._add_step(
                    StepType.OBSERVATION,
                    f"步骤 {step.step_id} 完成: {output[:200]}",
                )
                i += 1
            else:
                step.status = PlanStepStatus.FAILED
                self._record_deviation(
                    step.step_id,
                    f"步骤未产生预期结果 (期望: {step.expected_output})",
                )
                self._add_step(
                    StepType.OBSERVATION,
                    f"步骤 {step.step_id} 失败，实际结果: {output[:200]}",
                )
                if replans_left > 0 and self._has_tool_budget():
                    replans_left -= 1
                    new_steps = await self._replan(query, plan, step, messages, output)
                    if new_steps:
                        plan.replan_count += 1
                        # Replace the remaining steps with the re-planned ones.
                        plan.steps = plan.steps[: i + 1] + new_steps
                        continue
                # No replan possible -> skip and proceed.
                step.status = PlanStepStatus.SKIPPED
                i += 1

        return await self._synthesize_answer(query, plan, messages, tools_spec)

    async def _execute_step(
        self,
        step: PlanStep,
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]] | None,
    ) -> str:
        """Execute a single plan step via a focused mini ReAct iteration."""
        wm_summary = self._working_memory_summary()
        step_prompt = (
            f"请执行计划步骤 [{step.step_id}]: {step.description}\n"
            f"建议工具: {step.tool_hint or '由你决定'}\n"
            f"预期输出: {step.expected_output or '由你判断'}\n"
            f"已完成步骤结果:\n{wm_summary}\n"
            f"请调用合适的工具完成此步骤；若无需工具，直接给出该步骤的结果。"
        )
        step_messages = list(messages) + [{"role": "user", "content": step_prompt}]

        content, tool_calls = await self._call_llm_with_tools(step_messages, tools_spec)

        # Execute any requested tools (parallel + recovery aware).
        if tool_calls and self._has_tool_budget():
            await self._dispatch_tool_calls(tool_calls, step_messages, tools_spec)
            # Ask the LLM to summarise the step result using the observations.
            if self._has_tool_budget():
                step_messages.append(
                    {
                        "role": "user",
                        "content": "请根据以上工具结果，给出本步骤的结论性结果。",
                    }
                )
                content, _ = await self._call_llm_with_tools(step_messages, tools_spec)

        return content or ""

    def _step_succeeded(self, step: PlanStep, output: str) -> bool:
        """Heuristic: did the step produce usable output?"""
        if not output:
            return False
        failure_markers = ("失败", "error", "无法", "未能", "not found")
        return not any(m in output.lower() for m in failure_markers)

    async def _replan(
        self,
        query: str,
        plan: ExecutionPlan,
        failed_step: PlanStep,
        messages: list[dict[str, Any]],
        failure_detail: str,
    ) -> list[PlanStep]:
        """Ask the LLM to regenerate the remaining steps after a failure."""
        completed_summary = (
            "\n".join(
                f"[{s.step_id}] {s.actual_output[:200]}"
                for s in plan.steps
                if s.status == PlanStepStatus.COMPLETED
            )
            or "(无)"
        )
        prompt = REPLAN_PROMPT.format(
            task=query,
            failed_step=failed_step.step_id,
            failed_desc=failed_step.description,
            failure_detail=(failure_detail or "(无详情)")[:500],
            completed_summary=completed_summary,
        )
        replan_messages = [
            {"role": "system", "content": "你是一个严谨的任务规划专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = await self._llm_chat(replan_messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Replan LLM call failed: %s", e)
            return []
        new_steps = self._parse_plan_steps(response)
        # Re-validate tool hints on the new steps.
        known_tools = {t.name for t in self.tools.list_tools()}
        for s in new_steps:
            if (
                s.tool_hint
                and s.tool_hint.lower() not in ("none", "llm")
                and s.tool_hint not in known_tools
            ):
                s.tool_hint = ""
        self._add_step(
            StepType.PLANNING,
            f"动态重新规划：为失败步骤 {failed_step.step_id} 生成 {len(new_steps)} 个新步骤",
        )
        return new_steps

    async def _synthesize_answer(
        self,
        query: str,
        plan: ExecutionPlan,
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]] | None,
    ) -> str:
        """Synthesize the final answer from the gathered step results."""
        results_summary = self._working_memory_summary()
        prompt = SYNTHESIS_PROMPT.format(task=query, results_summary=results_summary)
        synth_messages = list(messages) + [{"role": "user", "content": prompt}]
        # Final synthesis should not request more tools.
        try:
            answer, _ = await self._call_llm_with_tools(synth_messages, None)
        except Exception as e:  # noqa: BLE001
            logger.warning("Synthesis LLM call failed: %s", e)
            answer = ""
        if not answer:
            # Fall back to the last step output or a ReAct consolidation.
            answer = plan.steps[-1].actual_output if plan.steps else ""
            if not answer:
                answer = await self._react_loop(messages, tools_spec)
        self._add_step(StepType.ANSWER, answer)
        return answer

    # ------------------------------------------------------------------
    # Layer 3: Deep reflection (推理规划层)
    # ------------------------------------------------------------------

    async def _reflect(self, query: str, answer: str, tools_used: list[str]) -> ReflectionScore:
        """LLM-based multi-dimensional scoring of the produced answer."""
        tools_used_str = ", ".join(tools_used) if tools_used else "(未使用工具)"
        prompt = REFLECTION_PROMPT.format(
            question=query,
            answer=answer,
            tools_used=tools_used_str,
        )
        reflect_messages = [
            {
                "role": "system",
                "content": "你是一个严谨的评审专家，请客观评估回答质量，只输出 JSON。",
            },
            {"role": "user", "content": prompt},
        ]
        reflection_text = ""
        try:
            reflection_text = await self._llm_chat(reflect_messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Reflection LLM call failed: %s", e)

        data = _extract_json(reflection_text)
        if isinstance(data, dict):
            score = ReflectionScore.from_dict(data)
        else:
            score = ReflectionScore(critique=reflection_text or "(反思未产生结构化内容)")
        self._add_step(
            StepType.REFLECTION,
            f"反思评分: 总分 {score.overall}/5, 最弱维度 {score.lowest_dimension}. "
            f"评语: {score.critique[:200]}",
        )
        self.trajectory.reflection_log.append(score.to_log_dict())
        return score

    def _reflection_needs_more(self, score: ReflectionScore) -> bool:
        """Whether the score is below the threshold and merits another round."""
        return score.overall < self.config.reflection_threshold

    def _has_tool_budget(self) -> bool:
        """Whether there is remaining tool-call budget for an extra round."""
        return self._tool_calls_used < self.config.max_tool_calls

    def _collect_tools_used(self) -> list[str]:
        """Distinct tool names used so far in the trajectory."""
        seen: list[str] = []
        for s in self.trajectory.steps:
            if s.step_type == StepType.ACTION and s.tool_name and s.tool_name not in seen:
                seen.append(s.tool_name)
        return seen

    # ------------------------------------------------------------------
    # Layer 4: Tool Execution (执行工具层)
    # ------------------------------------------------------------------

    async def _llm_chat(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str | ChatCompletionResponse:
        """Async LLM call that stays compatible with sync-only providers.

        Real providers (:class:`OpenAICompatibleProvider`) expose an async
        :meth:`chat_completion` — awaited directly so the event loop stays
        responsive (LangGraph's parallel specialist nodes actually run in
        parallel; ``asyncio.wait_for`` can cancel an in-flight call).

        Legacy sync-only providers (notably the scripted mocks in the test
        suite) only expose ``chat_completion_sync``. Those are offloaded via
        :func:`asyncio.to_thread` so they still don't block the loop, while
        keeping the mock's simple synchronous contract.
        """
        provider = self.llm_provider
        chat = getattr(provider, "chat_completion", None)
        if chat is not None:
            return await chat(messages, **kwargs)
        return await asyncio.to_thread(provider.chat_completion_sync, messages, **kwargs)

    async def _call_llm_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Call the LLM, preferring native function calling when tools exist.

        Returns ``(content, tool_calls)`` where ``tool_calls`` is a list of
        ``{"name", "arguments", "id"}`` dicts.

        Uses the provider's **async** :meth:`chat_completion` so the event loop
        stays responsive. The previous sync ``chat_completion_sync`` blocked
        the loop, which (a) serialized LangGraph's parallel specialist nodes
        into sequential execution and (b) made ``asyncio.wait_for`` timeouts
        ineffective against an in-progress blocking HTTP call (a 60s timeout
        only regained control ~97s later, after the sync call returned).
        """
        if tools_spec:
            try:
                response = await self._llm_chat(
                    messages,
                    tools=tools_spec,
                    tool_choice=self.config.tool_choice.value,
                    max_tokens=self.config.max_tokens,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Native tool calling unavailable, falling back to text parsing: %s", e
                )
                response = None

            if isinstance(response, ChatCompletionResponse):
                return response.content, list(response.tool_calls)

            content = response if isinstance(response, str) else None
            if content is None:
                try:
                    content = (
                        await self._llm_chat(messages, max_tokens=self.config.max_tokens) or ""
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error("LLM call failed: %s", e)
                    return "", []
            return content, self._parse_tool_calls(content)

        try:
            content = await self._llm_chat(messages, max_tokens=self.config.max_tokens) or ""
        except Exception as e:  # noqa: BLE001
            logger.error("LLM call failed: %s", e)
            return "", []
        return content, []

    def _add_step(self, step_type: StepType, content: str, **kwargs: Any) -> AgentStep:
        """Add a step to the trajectory."""
        step = AgentStep(
            step_type=step_type,
            content=content,
            timestamp=time.time() * 1000,
            **kwargs,
        )
        self.trajectory.add_step(step)
        return step

    # --- Smart truncation ------------------------------------------------

    def _remaining_token_budget(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the remaining token budget for tool results."""
        used = sum(_estimate_tokens(m.get("content", "")) for m in messages)
        return max(200, self.config.max_tokens - used)

    def _smart_truncate(self, text: str, query: str, budget_tokens: int) -> str:
        """Truncate *text* within *budget_tokens* preserving head/tail/keywords.

        Keeps the leading portion (summary), the trailing portion (conclusion)
        and any paragraphs containing query keywords. Falls back to a plain
        hard truncation when no keywords match.
        """
        if not text:
            return text
        max_chars = max(200, budget_tokens * 4)
        if len(text) <= max_chars:
            return text
        head_len = int(max_chars * 0.4)
        tail_len = int(max_chars * 0.3)
        head = text[:head_len]
        tail = text[-tail_len:]
        middle = text[head_len:-tail_len]
        keywords = [k for k in re.split(r"\s+", query.lower()) if len(k) > 1]
        paragraphs = middle.split("\n")
        matched = [p for p in paragraphs if any(k in p.lower() for k in keywords)]
        kw_budget = max_chars - head_len - tail_len - 40
        kw_text = "\n".join(matched)[: max(0, kw_budget)]
        if kw_text:
            return f"{head}\n...[已截断-保留关键段]...\n{kw_text}\n...[已截断]...\n{tail}"
        return f"{head}\n...[已截断]...\n{tail}"

    def _format_tool_result(
        self,
        result: ToolResult,
        query: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Format a tool result for injection into the conversation."""
        if not result.success:
            return f"Error: {result.error}"
        try:
            raw = json.dumps(result.data, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(result.data)
        budget = self._remaining_token_budget(messages)
        return self._smart_truncate(raw, query, budget)

    # --- Parallel + recovery-aware tool dispatch -------------------------

    @staticmethod
    def _resource_key(tc: dict[str, Any]) -> str:
        """Derive a resource key for dependency detection.

        Two tool calls that target the same file/resource share a key and are
        executed sequentially; all others are independent and may run in
        parallel.
        """
        name = tc.get("name", "")
        args = tc.get("arguments", {}) or {}
        if not isinstance(args, dict):
            return f"{name}:unique:{id(tc)}"
        for key in ("file_path", "vault_path", "path", "source_path"):
            if args.get(key):
                return f"{name}:{args[key]}"
        return f"{name}:unique:{id(tc)}"

    async def _execute_tool_with_recovery(self, tc: dict[str, Any]) -> ToolResult:
        """Execute a single tool call with retry, classification and circuit breaker.

        - Skips tools whose circuit breaker is open.
        - Retries transient errors up to ``max_tool_retries`` with exponential
          backoff.
        - On permanent/fatal failure or an open circuit, attempts a
          same-category alternative tool.
        - Fatal errors flip the agent into the ``ERROR`` state.
        """
        name = tc.get("name", "")
        args = tc.get("arguments", {}) or {}
        if not isinstance(args, dict):
            args = {}

        if self.tools.is_circuit_open(name):
            self._add_step(
                StepType.OBSERVATION,
                f"工具 {name} 已熔断（连续失败 {self.tools.get_failure_count(name)} 次），本次跳过",
            )
            alt = self.tools.get_alternative_tool(name)
            if alt:
                # Same-category alternatives have DIFFERENT signatures (e.g.
                # check_vitals(drugs=...) → TypeError). Filter args to the
                # alternative's declared params and skip the doomed substitute
                # call when a required param is missing.
                alt_args, ok = self.tools.compatible_args(alt, args)
                if ok:
                    self._add_step(
                        StepType.OBSERVATION,
                        f"尝试替代工具 {alt}（同类别）",
                    )
                    try:
                        result = await self.tools.execute(alt, **alt_args)
                        if result.success:
                            self.tools.record_success(alt)
                        return result
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Alternative tool %s failed: %s", alt, e)
                        return ToolResult(success=False, error=str(e), tool_name=alt)
                self._add_step(
                    StepType.OBSERVATION,
                    f"替代工具 {alt} 参数不兼容，跳过",
                )
            return ToolResult(
                success=False,
                error=f"工具 {name} 已熔断且无可用替代工具",
                tool_name=name,
            )

        last_error = ""
        for attempt in range(self.config.max_tool_retries + 1):
            try:
                result = await self.tools.execute(name, **args)
                if result.success:
                    self.tools.record_success(name)
                    return result
                last_error = result.error or "工具返回失败"
            except Exception as e:  # noqa: BLE001
                last_error = str(e)

            error_class = _classify_error(last_error)
            if error_class == ErrorClass.FATAL:
                self.tools.record_failure(name)
                self.state = AgentState.ERROR
                logger.error("Fatal tool error for %s: %s", name, last_error)
                break
            if error_class == ErrorClass.PERMANENT:
                self.tools.record_failure(name)
                break
            # Transient: back off and retry if budget remains.
            if attempt < self.config.max_tool_retries:
                await asyncio.sleep(0.1 * (2**attempt))
                continue
            self.tools.record_failure(name)
            break

        # Attempt an alternative tool from the same category.
        alt = self.tools.get_alternative_tool(name)
        if alt:
            # Same-category alternatives have DIFFERENT signatures (e.g.
            # read_patient_record(count=...) → TypeError). Filter args to
            # the alternative's declared params and skip the doomed substitute
            # call when a required param is missing.
            alt_args, ok = self.tools.compatible_args(alt, args)
            if ok:
                self._add_step(
                    StepType.OBSERVATION,
                    f"工具 {name} 失败（{last_error[:80]}），尝试替代工具 {alt}",
                )
                try:
                    result = await self.tools.execute(alt, **alt_args)
                    if result.success:
                        self.tools.record_success(alt)
                    return result
                except Exception as e:  # noqa: BLE001
                    logger.warning("Alternative tool %s failed: %s", alt, e)
                    return ToolResult(success=False, error=str(e), tool_name=alt)
            self._add_step(
                StepType.OBSERVATION,
                f"替代工具 {alt} 参数不兼容，跳过",
            )
        return ToolResult(success=False, error=last_error, tool_name=name)

    async def _dispatch_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]] | None,
        query: str = "",
    ) -> list[ToolResult]:
        """Dispatch tool calls in parallel groups, recording observations.

        Calls are grouped by :meth:`_resource_key`; groups run concurrently via
        :func:`asyncio.gather` while calls within a group run sequentially
        (they share a resource). Results are recorded into the trajectory and
        the conversation in the original request order.
        """
        # Record the assistant tool-call request for context.
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": ""}
        if tools_spec:
            # Materialize a stable fallback id up-front and reuse it for both
            # the assistant tool_call and the matching tool result. Deriving it
            # lazily from ``self._tool_calls_used`` at result-append time is
            # wrong: the counter is incremented during execution, so the fallback
            # id would drift from the assistant's and violate the provider's
            # ``tool_call_id`` contract (OpenAI/Anthropic reject or mismap it).
            for idx, tc in enumerate(tool_calls):
                if not tc.get("id"):
                    tc["id"] = f"call_{self._tool_calls_used}_{idx}"
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        # Partition into dependency groups (preserving original indices).
        groups: OrderedDict[str, list[tuple[int, dict[str, Any]]]] = OrderedDict()
        for idx, tc in enumerate(tool_calls):
            key = self._resource_key(tc)
            groups.setdefault(key, []).append((idx, tc))

        async def _run_group(
            items: list[tuple[int, dict[str, Any]]],
        ) -> list[tuple[int, dict[str, Any], ToolResult]]:
            out: list[tuple[int, dict[str, Any], ToolResult]] = []
            for idx, tc in items:
                if not self._has_tool_budget():
                    out.append(
                        (
                            idx,
                            tc,
                            ToolResult(
                                success=False,
                                error="工具调用预算已耗尽",
                                tool_name=tc.get("name", ""),
                            ),
                        )
                    )
                    continue
                self.state = AgentState.ACTING
                self._add_step(
                    StepType.ACTION,
                    f"调用工具 {tc.get('name', '')}",
                    tool_name=tc.get("name", ""),
                    tool_args=tc.get("arguments", {}),
                )
                result = await self._execute_tool_with_recovery(tc)
                self._tool_calls_used += 1
                out.append((idx, tc, result))
            return out

        if self.config.enable_parallel_tools and len(groups) > 1:
            group_outputs = await asyncio.gather(*(_run_group(items) for items in groups.values()))
        else:
            group_outputs = []
            for items in groups.values():
                group_outputs.append(await _run_group(items))

        # Flatten and sort by original index for deterministic ordering.
        flat: list[tuple[int, dict[str, Any], ToolResult]] = []
        for grp in group_outputs:
            flat.extend(grp)
        flat.sort(key=lambda x: x[0])

        for _idx, tc, result in flat:
            observation = self._format_tool_result(result, query, messages)
            self._add_step(
                StepType.OBSERVATION,
                f"{tc.get('name', '')} 的结果: {observation}",
                tool_name=tc.get("name", ""),
                tool_result=result,
            )
            call_id = tc["id"]
            messages.append({"role": "tool", "tool_call_id": call_id, "content": observation})

        return [r for _, _, r in flat]

    async def _react_loop(
        self,
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]] | None,
        query: str = "",
    ) -> str:
        """Run one ReAct (Reasoning + Acting) loop.

        Repeatedly asks the LLM what to do, executes any requested tools
        (in parallel when independent, with error recovery), and appends the
        observations to the conversation. Returns the final answer string
        when the LLM stops requesting tools or the budget is exhausted.
        """
        for iteration in range(self.config.max_iterations):
            if self.state == AgentState.ERROR:
                return ""

            # Think
            self.state = AgentState.THINKING
            content, tool_calls = await self._call_llm_with_tools(messages, tools_spec)

            if not content and not tool_calls:
                self.state = AgentState.ERROR
                self._add_step(StepType.THOUGHT, "LLM 返回了空响应")
                _incr_error_metric("agent.empty_response")
                return ""

            # No tool calls (or budget exhausted) -> this content is the answer.
            if not tool_calls or not self._has_tool_budget():
                self.state = AgentState.FINISHED
                self._add_step(StepType.ANSWER, content)
                _incr_agent_iteration_metric(self._current_task_id, "finished")
                return content

            # Act: execute the requested tool calls (parallel + recovery).
            remaining = self.config.max_tool_calls - self._tool_calls_used
            executable = tool_calls[: max(0, remaining)]
            if executable:
                # Preserve any reasoning text the LLM emitted alongside tools.
                if content:
                    messages.append({"role": "assistant", "content": content})
                await self._dispatch_tool_calls(executable, messages, tools_spec, query=query)
            # 运行中周期性 checkpoint：每 N 轮迭代自动保存一次，
            # 进程崩溃时可从中断点恢复
            if (
                self.checkpoint_store is not None
                and (iteration + 1) % self._checkpoint_interval == 0
            ):
                try:
                    self.save_checkpoint(self.checkpoint_store, self._current_task_id)
                except Exception:  # noqa: BLE001 - checkpoint 失败不影响主流程
                    pass

        # Iterations exhausted -> ask the LLM for a final consolidated answer.
        self.state = AgentState.FINISHED
        final = (await self._call_llm_with_tools(messages, None))[0]
        if final:
            self._add_step(StepType.ANSWER, final)
        _incr_agent_iteration_metric(self._current_task_id, "exhausted")
        return final

    # ------------------------------------------------------------------
    # Layer 5: Multi-agent collaboration (协调监管层)
    # ------------------------------------------------------------------

    async def _decompose_task(self, query: str) -> list[str]:
        """Ask the LLM to decompose a complex task into subtask strings."""
        prompt = ORCHESTRATOR_DECOMPOSE_PROMPT.format(
            task=query, max_subtasks=self.config.max_subtasks
        )
        messages = [
            {"role": "system", "content": "你是任务分解专家，只输出 JSON 数组。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = await self._llm_chat(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Decomposition LLM call failed: %s", e)
            return [query]
        data = _extract_json(response)
        if isinstance(data, list) and data:
            return [str(s) for s in data if s][: self.config.max_subtasks]
        return [query]

    async def _run_multi_agent(self, query: str) -> str:
        """Orchestrator-worker execution backed by the TaskStore subtask API.

        Workers run in parallel via :func:`asyncio.gather`, each guarded by
        a per-worker timeout (``config.worker_timeout``). A timed-out or
        failed worker is recorded as such in the TaskStore but does not
        abort the whole orchestration.

        Each subtask gets a dedicated :class:`WorkerAgent` instance because
        ``WorkerAgent`` carries per-run state (trajectory, working memory,
        tool-call counter) that is not safe to share across concurrent runs.
        Subtask records are created sequentially before dispatch since the
        TaskStore is not guaranteed concurrency-safe.
        """
        from doctoragent.orchestration.state_machine import TaskState

        subtasks = await self._decompose_task(query)
        # A single subtask is not worth the orchestration overhead.
        if len(subtasks) <= 1:
            return await self._run_single(query)

        parent_id = uuid4()
        self._add_step(
            StepType.PLANNING,
            f"多 Agent 协作：将任务分解为 {len(subtasks)} 个子任务",
        )

        # 预先顺序创建 subtask 记录（TaskStore 不保证并发安全）。
        child_ids: list[Any] = []
        for i in range(len(subtasks)):
            try:
                child_ids.append(
                    self.task_store.create_subtask(
                        parent_id,
                        source_path=f"subtask://{i}",
                        subtask_role="worker",
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("create_subtask failed: %s", e)
                child_ids.append(None)

        worker_timeout = self.config.worker_timeout

        async def _run_single_worker(
            subtask: str, child_id: Any, index: int
        ) -> tuple[str, str, Any]:
            """运行单个 worker，带超时保护。

            返回 ``(status, subtask, result)``，``status`` 取值为
            ``"completed"`` / ``"timeout"`` / ``"failed"``。
            """
            # 每个 subtask 独立 worker 实例，隔离 per-run 状态。
            worker = WorkerAgent(
                llm_provider=self.llm_provider,
                tool_registry=self.tools,
                config=self.config,
                memory_system=self.memory_system,
                task_store=self.task_store,
            )
            try:
                result = await asyncio.wait_for(worker.run(subtask), timeout=worker_timeout)
                if child_id is not None:
                    self.task_store.update_subtask_status(
                        child_id,
                        TaskState.COMPLETED,
                        {"result": result, "subtask": subtask},
                    )
                return ("completed", subtask, result)
            except asyncio.TimeoutError:
                logger.warning("Worker %d 超时（%ss）", index, worker_timeout)
                if child_id is not None:
                    self.task_store.update_subtask_status(
                        child_id,
                        TaskState.FAILED,
                        {"error": f"worker timeout after {worker_timeout}s", "subtask": subtask},
                    )
                return ("timeout", subtask, None)
            except Exception as e:  # noqa: BLE001
                logger.warning("Worker %d 失败: %s", index, e)
                if child_id is not None:
                    self.task_store.update_subtask_status(
                        child_id,
                        TaskState.FAILED,
                        {"error": str(e), "subtask": subtask},
                    )
                return ("failed", subtask, str(e))

        # 并行执行所有 subtasks
        tasks = [_run_single_worker(sub, child_ids[i], i) for i, sub in enumerate(subtasks)]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # 按原始 subtask 顺序聚合结果
        aggregated: list[dict[str, Any]] = []
        for status, sub, result in results:
            if status == "completed":
                aggregated.append({"subtask": sub, "result": result, "state": "COMPLETED"})
            elif status == "timeout":
                aggregated.append(
                    {
                        "subtask": sub,
                        "result": f"worker 超时（{worker_timeout}s）",
                        "state": "TIMEOUT",
                    }
                )
            else:
                aggregated.append(
                    {"subtask": sub, "result": result or "worker 失败", "state": "FAILED"}
                )

        # Aggregate via the TaskStore and synthesize a final answer.
        try:
            agg = self.task_store.aggregate_subtask_results(parent_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("aggregate_subtask_results failed: %s", e)
            agg = {"completed": 0, "failed": 0, "results": aggregated}

        return await self._synthesize_multi_agent(query, agg)

    async def _synthesize_multi_agent(self, query: str, aggregation: dict[str, Any]) -> str:
        """Use the LLM to fuse worker results into a final answer."""
        results = aggregation.get("results", []) if isinstance(aggregation, dict) else []
        lines = []
        for i, entry in enumerate(results, 1):
            if not isinstance(entry, dict):
                entry = {"result": str(entry)}
            sub = entry.get("subtask", f"子任务{i}")
            res = entry.get("result", entry.get("raw", ""))
            lines.append(f"- 子任务「{sub}」: {res}")
        results_text = "\n".join(lines) or "(无结果)"
        prompt = ORCHESTRATOR_SYNTHESIS_PROMPT.format(task=query, results=results_text)
        messages = [
            {"role": "system", "content": "你是协调者，负责综合多个 worker 的结果。"},
            {"role": "user", "content": prompt},
        ]
        try:
            answer = await self._llm_chat(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Multi-agent synthesis failed: %s", e)
            answer = ""
        if not answer:
            answer = results_text
        self._add_step(StepType.ANSWER, answer)
        return answer

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def _run_single(self, query: str) -> str:
        """Single-agent pipeline: memory recall -> plan/ReAct -> reflection."""
        start_time = time.time()
        self.state = AgentState.THINKING
        self.trajectory = AgentTrajectory()
        self._working_memory = {}
        self._tool_calls_used = 0
        # 重置当前执行计划，避免跨 run 残留（便于 checkpoint 保存/恢复）
        self._plan = None
        # Stamp a run-scoped task id so iteration metrics and checkpoints can
        # be correlated. ``uuid4().hex[:8]`` keeps the label short for
        # Prometheus cardinality hygiene.
        self._current_task_id = uuid4().hex[:8]

        # Layer 2: recall long-term + episodic memory.
        memory_context = self._recall_memory(query)
        short_term = self._format_short_term()
        system_prompt = self._build_system_prompt(memory_context, short_term)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        # Layer 4: native tool definitions.
        use_native_tools = bool(self.tools.list_tools()) and (
            self.config.tool_choice != ToolChoice.NONE
        )
        tools_spec = self.tools.to_openai_tools() if use_native_tools else None

        # Layer 3: Plan-and-Execute or plain ReAct.
        if self.config.enable_planning:
            plan = await self._generate_plan(query, messages)
            # 留存计划到 self._plan，便于运行中 checkpoint 保存与崩溃后恢复
            self._plan = plan
            answer = await self._execute_plan(query, plan, messages, tools_spec)
        else:
            answer = await self._react_loop(messages, tools_spec, query=query)

        if self.state == AgentState.ERROR:
            self._finalize_trajectory(start_time)
            return answer or "抱歉，我无法完成这个任务。"

        # Layer 3: Deep reflection with up to max_reflection_rounds rounds.
        if self.config.enable_reflection:
            rounds = 0
            while rounds < self.config.max_reflection_rounds:
                tools_used = self._collect_tools_used()
                score = await self._reflect(query, answer, tools_used)
                if not self._reflection_needs_more(score) or not self._has_tool_budget():
                    break
                rounds += 1
                weakest = score.lowest_dimension
                weakest_score = getattr(score, weakest, 0.0)
                messages.append(
                    {
                        "role": "user",
                        "content": REFINE_PROMPT.format(
                            dimension=weakest,
                            score=weakest_score,
                            critique=score.critique[:200],
                        ),
                    }
                )
                refined = await self._react_loop(messages, tools_spec, query=query)
                if refined:
                    answer = refined

        if not answer:
            answer = "抱歉，我无法完成这个任务。"

        # Layer 2: persist episodic + long-term memory + short-term window.
        self._short_term_history.append({"role": "user", "content": query})
        self._short_term_history.append({"role": "assistant", "content": answer})
        if len(self._short_term_history) > self.config.short_term_window * 2:
            self._short_term_history = self._short_term_history[
                -self.config.short_term_window * 2 :
            ]
        self._store_memory(query, answer)

        self._finalize_trajectory(start_time)
        return answer

    def _finalize_trajectory(self, start_time: float) -> None:
        """Record timing and tool-call totals on the trajectory."""
        self.state = AgentState.FINISHED
        self.trajectory.total_time_ms = (time.time() - start_time) * 1000
        self.trajectory.total_tool_calls = self._tool_calls_used

    async def run(self, query: str) -> str:
        """Run the agent on a user query.

        Routes to multi-agent orchestration when ``enable_multi_agent`` is set
        and a ``task_store`` is configured; otherwise runs the single-agent
        plan/ReAct/reflection pipeline. When all advanced flags are disabled
        the behaviour reduces to the original ReAct loop.
        """
        if self.config.enable_multi_agent and self.task_store is not None:
            return await self._run_multi_agent(query)
        return await self._run_single(query)

    def run_sync(self, query: str) -> str:
        """Synchronous wrapper for run."""
        return async_to_sync(self.run(query), timeout=120)

    def get_trajectory(self) -> AgentTrajectory:
        """Get the execution trajectory."""
        return self.trajectory

    def reset(self) -> None:
        """Reset agent state."""
        self.state = AgentState.IDLE
        self.trajectory = AgentTrajectory()
        self._working_memory = {}
        self._tool_calls_used = 0
        # 清空当前执行计划，避免跨 run 残留
        self._plan = None

    # ------------------------------------------------------------------
    # Streaming output + checkpoint persistence (additive, no run() changes)
    # ------------------------------------------------------------------

    async def run_stream(self, task: str, **kwargs: Any) -> AsyncGenerator[str, None]:
        """Stream agent execution as human-readable progress chunks.

        A streaming wrapper around the existing plan/ReAct/reflection
        pipeline. Yields ``"Planning..."``, a plan summary, then per-loop
        ``Thought:``/``Action:``/``Observation:`` chunks and finally the
        answer. Delegates to the same internal helpers
        (``_generate_plan``, ``_execute_plan``, ``_call_llm_with_tools``,
        ``_dispatch_tool_calls``) used by :meth:`run`; it does **not**
        duplicate :meth:`_react_loop` — the non-planning branch runs its
        own short streaming loop so progress can be yielded mid-execution.
        """
        start_time = time.time()
        self.state = AgentState.THINKING
        self.trajectory = AgentTrajectory()
        self._working_memory: dict[str, Any] = {}
        self._tool_calls_used = 0
        # 重置当前执行计划，避免跨 run 残留
        self._plan = None

        memory_context = self._recall_memory(task)
        short_term = self._format_short_term()
        system_prompt = self._build_system_prompt(memory_context, short_term)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        use_native_tools = bool(self.tools.list_tools()) and (
            self.config.tool_choice != ToolChoice.NONE
        )
        tools_spec = self.tools.to_openai_tools() if use_native_tools else None

        # 整条流式管线包一层超时保护：任何 LLM 调用卡住都会被
        # asyncio.wait_for 取消并向外抛出 asyncio.TimeoutError，由下面的
        # except 捕获后输出错误并终止，避免流式输出永久挂起。
        try:
            if self.config.enable_planning:
                yield "Planning..."
                plan = await asyncio.wait_for(
                    self._generate_plan(task, messages),
                    timeout=self._llm_timeout,
                )
                # 留存计划到 self._plan，便于运行中 checkpoint 保存与崩溃后恢复
                self._plan = plan
                if plan.steps:
                    yield "Plan:\n" + "\n".join(
                        f"  {s.step_id}. {s.description}" for s in plan.steps
                    )
                answer = await asyncio.wait_for(
                    self._execute_plan(task, plan, messages, tools_spec),
                    timeout=self._llm_timeout,
                )
            else:
                yield "Thinking..."
                answer = ""
                for _iteration in range(self.config.max_iterations):
                    if self.state == AgentState.ERROR:
                        yield "Error: agent entered error state."
                        return
                    self.state = AgentState.THINKING
                    content, tool_calls = await asyncio.wait_for(
                        self._call_llm_with_tools(messages, tools_spec),
                        timeout=self._llm_timeout,
                    )
                    if not content and not tool_calls:
                        self.state = AgentState.ERROR
                        self._add_step(StepType.THOUGHT, "LLM 返回了空响应")
                        yield "Error: LLM returned an empty response."
                        return
                    if content:
                        yield f"Thought: {content}"
                    if not tool_calls or not self._has_tool_budget():
                        self.state = AgentState.FINISHED
                        self._add_step(StepType.ANSWER, content)
                        answer = content
                        break
                    remaining = self.config.max_tool_calls - self._tool_calls_used
                    executable = tool_calls[: max(0, remaining)]
                    for tc in executable:
                        yield f"Action: {tc.get('name', '')}({_truncate_args(tc.get('arguments', {}))})"
                    if executable and content:
                        messages.append({"role": "assistant", "content": content})
                    results = await self._dispatch_tool_calls(
                        executable, messages, tools_spec, query=task
                    )
                    for _tc, result in zip(executable, results, strict=False):
                        obs = self._format_tool_result(result, task, messages)
                        yield f"Observation: {obs[:300]}"
                    # 运行中周期性 checkpoint：每 N 轮迭代自动保存一次，
                    # 进程崩溃时可从中断点恢复
                    if (
                        self.checkpoint_store is not None
                        and (_iteration + 1) % self._checkpoint_interval == 0
                    ):
                        try:
                            self.save_checkpoint(self.checkpoint_store, self._current_task_id)
                        except Exception:  # noqa: BLE001 - checkpoint 失败不影响主流程
                            pass
                else:
                    final = (
                        await asyncio.wait_for(
                            self._call_llm_with_tools(messages, None),
                            timeout=self._llm_timeout,
                        )
                    )[0]
                    if final:
                        self._add_step(StepType.ANSWER, final)
                        answer = final

            if self.state == AgentState.ERROR:
                self._finalize_trajectory(start_time)
                yield "Error: agent entered error state."
                return

            # Deep reflection (mirrors _run_single).
            if self.config.enable_reflection:
                rounds = 0
                while rounds < self.config.max_reflection_rounds:
                    tools_used = self._collect_tools_used()
                    score = await asyncio.wait_for(
                        self._reflect(task, answer, tools_used),
                        timeout=self._llm_timeout,
                    )
                    yield f"Reflection: {score.overall}/5 (weakest: {score.lowest_dimension})"
                    if not self._reflection_needs_more(score) or not self._has_tool_budget():
                        break
                    rounds += 1
                    weakest = score.lowest_dimension
                    weakest_score = getattr(score, weakest, 0.0)
                    messages.append(
                        {
                            "role": "user",
                            "content": REFINE_PROMPT.format(
                                dimension=weakest,
                                score=weakest_score,
                                critique=score.critique[:200],
                            ),
                        }
                    )
                    refined = await asyncio.wait_for(
                        self._react_loop(messages, tools_spec, query=task),
                        timeout=self._llm_timeout,
                    )
                    if refined:
                        answer = refined
        except asyncio.TimeoutError:
            # LLM 调用超时：记录错误状态、收尾轨迹并输出错误，终止流式输出
            self.state = AgentState.ERROR
            self._finalize_trajectory(start_time)
            yield "Error: LLM 响应超时，请重试"
            return

        if not answer:
            answer = "抱歉，我无法完成这个任务。"
        self._finalize_trajectory(start_time)
        yield answer

    def save_checkpoint(self, store: CheckpointStore, task_id: str) -> None:
        """Snapshot the current run state into *store* under *task_id*.

        Builds an :class:`AgentCheckpoint` from the live trajectory + short
        term memory + tool-call counter. 除了原有字段，还保存完整执行
        轨迹（trajectory）、工作记忆（working_memory）与执行计划（plan），
        以便 agent 从中断点完整恢复继续执行。
        """
        iteration = sum(1 for s in self.trajectory.steps if s.step_type == StepType.ACTION)
        checkpoint = AgentCheckpoint(
            task_id=task_id,
            iteration=iteration,
            messages=list(self._short_term_history),
            plan=(self._plan.model_dump() if self._plan is not None else None),
            tool_calls_made=self._tool_calls_used,
            created_at=datetime.now(timezone.utc).isoformat(),
            status="paused",
            # 新增：完整执行轨迹，每个 step 序列化为 dict，恢复时重建为 AgentStep
            trajectory=[s.model_dump() for s in self.trajectory.steps],
            # 新增：工作记忆，执行过程中累积的中间结果
            working_memory=dict(self._working_memory),
        )
        store.save(task_id, checkpoint)

    @classmethod
    def resume_from_checkpoint(
        cls,
        store: CheckpointStore,
        task_id: str,
        *,
        llm_provider: Any,
        tool_registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_system: Any = None,
        task_store: Any = None,
    ) -> Agent:
        """Build an :class:`Agent` whose state is restored from a checkpoint.

        Raises :class:`ValueError` when no checkpoint exists for
        *task_id* or the saved status is not ``"paused"`` (only paused
        runs are resumable; completed/failed checkpoints are rejected so
        callers don't silently re-execute a finished run).
        """
        checkpoint = store.load(task_id)
        if checkpoint is None:
            raise ValueError(f"No checkpoint found for task_id {task_id!r}")
        if checkpoint.status != "paused":
            raise ValueError(
                f"Cannot resume checkpoint in status {checkpoint.status!r}; "
                "only 'paused' checkpoints are resumable."
            )
        agent = cls(
            llm_provider=llm_provider,
            tool_registry=tool_registry,
            config=config,
            memory_system=memory_system,
            task_store=task_store,
        )
        # Restore the ephemeral run state captured at save time.
        agent._short_term_history = list(checkpoint.messages)
        agent._tool_calls_used = checkpoint.tool_calls_made
        agent.state = AgentState.THINKING
        # 新增：恢复完整执行状态，使 agent 能从中断点继续执行。
        # 1) 完整执行轨迹：把保存的 dict 列表重建为 AgentStep 列表
        if getattr(checkpoint, "trajectory", None):
            try:
                agent.trajectory.steps = [
                    AgentStep.model_validate(s) if isinstance(s, dict) else s
                    for s in checkpoint.trajectory
                ]
            except Exception:  # noqa: BLE001 - 轨迹恢复失败不阻断 resume
                logger.warning("Failed to restore trajectory from checkpoint", exc_info=True)
        # 2) 工作记忆：执行过程中累积的中间结果
        if getattr(checkpoint, "working_memory", None):
            agent._working_memory = dict(checkpoint.working_memory)
        # 3) 执行计划：恢复失败则置空，agent 会重新规划
        if getattr(checkpoint, "plan", None):
            try:
                agent._plan = ExecutionPlan.model_validate(checkpoint.plan)
            except Exception:  # noqa: BLE001 - 计划恢复失败不阻断 resume
                logger.warning("Failed to restore plan from checkpoint", exc_info=True)
                agent._plan = None
        return agent

    # ------------------------------------------------------------------
    # Shutdown: persist the final trajectory as a checkpoint
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Clean up resources and persist the final run state.

        When a :class:`CheckpointStore` was wired in at construction time,
        the current trajectory is snapshotted (status ``"paused"``) so an
        interrupted run can be resumed. ``save_checkpoint`` was previously
        never invoked on shutdown, so in-flight runs were silently lost.
        """
        if self.checkpoint_store is not None and self.trajectory.steps:
            try:
                self.save_checkpoint(self.checkpoint_store, "shutdown")
            except Exception:  # noqa: BLE001 - shutdown must not fail on checkpoint
                logger.warning("Failed to save shutdown checkpoint", exc_info=True)

    async def __aenter__(self) -> Agent:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


def await_result_fallback(coro: Any) -> str:
    """Helper to materialise a coroutine result for fallback synthesis.

    The synthesis path may need a ReAct result synchronously inside a
    non-async branch; this runs the coroutine to completion. Returns an empty
    string on failure.
    """
    try:
        return async_to_sync(coro, timeout=60) or ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Multi-Agent collaboration: Orchestrator / Worker
# ---------------------------------------------------------------------------


class WorkerAgent(Agent):
    """A worker agent that executes a single subtask.

    Functionally identical to :class:`Agent` but with ``enable_multi_agent``
    forced off so workers never recurse into orchestration. The orchestrator
    records the subtask status in the ``TaskStore`` around the worker run.
    """

    def __init__(
        self,
        llm_provider: Any,
        tool_registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_system: Any = None,
        task_store: Any = None,
    ) -> None:
        config = config or AgentConfig()
        # Workers must not recurse into orchestration.
        config.enable_multi_agent = False
        super().__init__(
            llm_provider=llm_provider,
            tool_registry=tool_registry,
            config=config,
            memory_system=memory_system,
            task_store=task_store,
        )


class OrchestratorAgent(Agent):
    """Orchestrator that decomposes a complex task and dispatches workers.

    Forces ``enable_multi_agent=True`` so :meth:`run` routes to the
    orchestrator-worker pipeline. Requires a ``task_store`` to persist
    subtask state.
    """

    def __init__(
        self,
        llm_provider: Any,
        tool_registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_system: Any = None,
        task_store: Any = None,
    ) -> None:
        config = config or AgentConfig()
        config.enable_multi_agent = True
        super().__init__(
            llm_provider=llm_provider,
            tool_registry=tool_registry,
            config=config,
            memory_system=memory_system,
            task_store=task_store,
        )
        if self.task_store is None:
            logger.warning(
                "OrchestratorAgent created without a task_store; "
                "multi-agent persistence will be disabled."
            )
            # Disable routing when no task_store is available so run() falls
            # back to the single-agent path instead of failing.
            self.config.enable_multi_agent = False


# ---------------------------------------------------------------------------
# Agent Factory
# ---------------------------------------------------------------------------


def create_agent(
    llm_provider: Any,
    rag_pipeline: Any = None,
    task_store: Any = None,
    memory_system: Any = None,
    config: AgentConfig | None = None,
) -> Agent:
    """Create an agent with all default tools.

    The ``memory_system`` and ``task_store`` are now wired into the
    :class:`Agent` (previously accepted but unused), enabling the four-layer
    memory integration and multi-agent collaboration respectively.
    """
    from .tools import create_default_registry

    registry = create_default_registry(
        rag_pipeline=rag_pipeline,
        task_store=task_store,
        memory_system=memory_system,
        llm_provider=llm_provider,
    )

    config = config or AgentConfig()
    if config.enable_multi_agent and task_store is not None:
        return OrchestratorAgent(
            llm_provider=llm_provider,
            tool_registry=registry,
            config=config,
            memory_system=memory_system,
            task_store=task_store,
        )

    return Agent(
        llm_provider=llm_provider,
        tool_registry=registry,
        config=config,
        memory_system=memory_system,
        task_store=task_store,
    )

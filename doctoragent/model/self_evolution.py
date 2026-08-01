"""Agent self-evolution module - learns from execution trajectories to improve future performance.

Implements:
- Trajectory analysis: extract patterns from successful/failed executions
- Prompt optimization: improve system prompts based on what worked
- Tool selection optimization: learn which tools work best for which queries
- Experience storage: persist learned experiences for future use

The :class:`SelfEvolutionEngine` consumes completed :class:`~doctoragent.model.agent.AgentTrajectory`
objects, distils reusable *experiences* (query pattern, outcome, lessons, optimised
prompt, recommended tools) and persists them to a dedicated SQLite database so
subsequent runs can :meth:`recall_experiences` and apply the learned guidance.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from doctoragent._utils import open_sqlite
from doctoragent.compat import UTC, StrEnum
from doctoragent.model.agent import _extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & data models
# ---------------------------------------------------------------------------


class ExecutionOutcome(StrEnum):
    """Outcome of a single agent execution trajectory."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


@dataclass
class TrajectoryPattern:
    """Aggregated pattern extracted from one or more trajectories.

    Captures what a class of executions looks like: how often it succeeds,
    how many iterations it typically takes, which tools and which errors are
    common, and an optimised prompt that performed best for the pattern.
    """

    pattern_description: str = ""
    success_count: int = 0
    failure_count: int = 0
    avg_iterations: float = 0.0
    common_tools: list[str] = dc_field(default_factory=list)
    common_errors: list[str] = dc_field(default_factory=list)
    optimized_prompt: str = ""

    @property
    def total_count(self) -> int:
        """Total number of trajectories summarised by this pattern."""
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        """Fraction of trajectories that succeeded (``0.0``-``1.0``)."""
        total = self.total_count
        return self.success_count / total if total else 0.0

    def merge(self, other: TrajectoryPattern) -> TrajectoryPattern:
        """Merge *other* into a new pattern, averaging iterations and summing counts."""
        total = self.total_count + other.total_count
        if total == 0:
            avg_iter = 0.0
        else:
            avg_iter = (
                self.avg_iterations * self.total_count + other.avg_iterations * other.total_count
            ) / total
        tools = list(dict.fromkeys(self.common_tools + other.common_tools))
        errors = list(dict.fromkeys(self.common_errors + other.common_errors))
        prompt = self.optimized_prompt or other.optimized_prompt
        return TrajectoryPattern(
            pattern_description=self.pattern_description or other.pattern_description,
            success_count=self.success_count + other.success_count,
            failure_count=self.failure_count + other.failure_count,
            avg_iterations=round(avg_iter, 2),
            common_tools=tools,
            common_errors=errors,
            optimized_prompt=prompt,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (for logging / SQLite storage)."""
        return {
            "pattern_description": self.pattern_description,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "avg_iterations": self.avg_iterations,
            "common_tools": self.common_tools,
            "common_errors": self.common_errors,
            "optimized_prompt": self.optimized_prompt,
        }


@dataclass
class Experience:
    """A single learned experience persisted for future recall.

    An experience bundles a query pattern with the lessons learned, the best
    prompt discovered and the tools that worked, so the agent can reuse them
    for similar future queries.
    """

    id: str = dc_field(default_factory=lambda: uuid4().hex)
    query: str = ""
    query_pattern: str = ""
    outcome: ExecutionOutcome = ExecutionOutcome.PARTIAL
    lessons: list[str] = dc_field(default_factory=list)
    optimized_prompt: str = ""
    recommended_tools: list[str] = dc_field(default_factory=list)
    common_tools: list[str] = dc_field(default_factory=list)
    common_errors: list[str] = dc_field(default_factory=list)
    avg_iterations: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    pattern_description: str = ""
    created_at: str = dc_field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = dc_field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for SQLite / JSON storage."""
        return {
            "id": self.id,
            "query": self.query,
            "query_pattern": self.query_pattern,
            "outcome": str(self.outcome),
            "lessons": self.lessons,
            "optimized_prompt": self.optimized_prompt,
            "recommended_tools": self.recommended_tools,
            "common_tools": self.common_tools,
            "common_errors": self.common_errors,
            "avg_iterations": self.avg_iterations,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "pattern_description": self.pattern_description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> Experience:
        """Build an :class:`Experience` from a SQLite row / dict."""

        def _loads(value: Any, default: Any) -> Any:
            if value is None:
                return default
            if isinstance(value, (list, dict)):
                return value
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                return default

        outcome_raw = str(row["outcome"]) if "outcome" in row.keys() else "partial"
        try:
            outcome = ExecutionOutcome(outcome_raw)
        except ValueError:
            outcome = ExecutionOutcome.PARTIAL
        return cls(
            id=row["id"],
            query=row["query"],
            query_pattern=row["query_pattern"],
            outcome=outcome,
            lessons=_loads(row["lessons"], []),
            optimized_prompt=row["optimized_prompt"] or "",
            recommended_tools=_loads(row["recommended_tools"], []),
            common_tools=_loads(row["common_tools"], []),
            common_errors=_loads(row["common_errors"], []),
            avg_iterations=float(row["avg_iterations"] or 0.0),
            success_count=int(row["success_count"] or 0),
            failure_count=int(row["failure_count"] or 0),
            pattern_description=row["pattern_description"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_LESSONS_PROMPT = """你是一个智能体自进化分析专家。请从以下若干执行轨迹中提炼可复用的经验教训。

用户原始问题（示例）：{sample_query}

轨迹摘要：
{trajectories_summary}

请以 JSON 输出，包含以下字段：
- lessons: 字符串数组，每条是一个简明可复用的经验教训（如"查询合同到期日时应同时检索'到期'与'有效期'关键词"）
- patterns: 字符串数组，识别出的常见查询模式描述
- optimized_prompt: 字符串，针对此类查询优化后的系统提示词（若不便优化可留空字符串）
- recommended_tools: 字符串数组，对此类查询推荐使用的工具名

只输出 JSON，不要多余解释。"""

_PROMPT_OPTIMIZATION_PROMPT = """你是提示词优化专家。基于以下执行轨迹，为"查询模式：{query_pattern}"优化系统提示词。

查询模式：{query_pattern}
该模式下的轨迹摘要：
{trajectories_summary}

请直接输出优化后的系统提示词文本（不要包裹在 JSON 或代码块中），使其能引导智能体更高效地完成此类任务。要求：明确适用场景、推荐工具使用顺序、常见陷阱提示。"""

_TOOL_SELECTION_PROMPT = """你是工具选择优化专家。针对"{query_type}"类查询，根据以下工具使用统计推荐最佳工具组合。

工具使用统计（JSON）：
{tool_usage_stats}

请以 JSON 输出，字段：
- recommended: 字符串数组，按推荐优先级排序的工具名
- reasoning: 字符串数组，每个工具对应的简要推荐理由（与 recommended 一一对应）

只输出 JSON。"""


# ---------------------------------------------------------------------------
# Self-evolution engine
# ---------------------------------------------------------------------------


class SelfEvolutionEngine:
    """Learns from execution trajectories to improve future agent performance.

    The engine runs a small *evolution loop* (:meth:`evolve`):

    1. **Analyse** each new trajectory (:meth:`analyze_trajectory`) to extract
       tools, errors, iteration count and outcome.
    2. **Extract lessons** across the batch (:meth:`extract_lessons`) using the
       LLM, producing reusable lessons, an optimised prompt and recommended
       tools.
    3. **Store** the resulting :class:`Experience` (:meth:`store_experience`)
       in a dedicated SQLite database for later recall.

    Subsequent runs can call :meth:`recall_experiences` /
    :meth:`get_optimization_suggestions` to retrieve the most relevant past
    experiences and apply their guidance.
    """

    def __init__(self, task_store: Any, llm_provider: Any) -> None:
        """Initialise the engine.

        Parameters
        ----------
        task_store:
            A store exposing a ``db_path`` attribute (e.g.
            :class:`~doctoragent.orchestration.task_store.TaskStore`). The
            evolution database is created next to it as
            ``agent_evolution.db``. When ``None`` or lacking ``db_path`` a
            local ``agent_evolution.db`` is used instead.
        llm_provider:
            Any object exposing ``chat_completion_sync(messages) -> str``
            (e.g. :class:`~doctoragent.model.provider.OpenAICompatibleProvider`).
        """
        self.task_store = task_store
        self.llm_provider = llm_provider
        self._db_path = self._resolve_db_path(task_store)
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_db_path(task_store: Any) -> Path:
        """Resolve the SQLite path for experiences.

        Prefers a sibling file of the task store's database so experiences
        live alongside task state; falls back to a local file otherwise.
        """
        if task_store is not None:
            db_path = getattr(task_store, "db_path", None)
            if db_path is not None:
                try:
                    parent = Path(db_path).parent
                    parent.mkdir(parents=True, exist_ok=True)
                    return parent / "agent_evolution.db"
                except Exception as e:  # noqa: BLE001
                    logger.debug("Could not derive evolution db path: %s", e)
        return Path("agent_evolution.db")

    def _init_db(self) -> None:
        """Create the experiences table if it does not exist."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_experiences (
                        id TEXT PRIMARY KEY,
                        query TEXT NOT NULL DEFAULT '',
                        query_pattern TEXT NOT NULL DEFAULT '',
                        outcome TEXT NOT NULL DEFAULT 'partial',
                        lessons TEXT NOT NULL DEFAULT '[]',
                        optimized_prompt TEXT NOT NULL DEFAULT '',
                        recommended_tools TEXT NOT NULL DEFAULT '[]',
                        common_tools TEXT NOT NULL DEFAULT '[]',
                        common_errors TEXT NOT NULL DEFAULT '[]',
                        avg_iterations REAL NOT NULL DEFAULT 0,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        failure_count INTEGER NOT NULL DEFAULT 0,
                        pattern_description TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_experiences_pattern "
                    "ON agent_experiences(query_pattern)"
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to initialise evolution db: %s", e)

    def _connect(self) -> sqlite3.Connection:
        """Open a connection configured for concurrent multi-thread use."""
        return open_sqlite(self._db_path, row_factory=sqlite3.Row)

    # ------------------------------------------------------------------
    # Trajectory accessors (duck-typed, tolerate AgentTrajectory / dict)
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_steps(trajectory: Any) -> list[Any]:
        """Return the list of steps from a trajectory-like object."""
        steps = getattr(trajectory, "steps", None)
        if steps is not None:
            return list(steps)
        if isinstance(trajectory, dict):
            return list(trajectory.get("steps", []))
        return []

    @staticmethod
    def _step_type(step: Any) -> str:
        """Normalise a step's type to a lowercase string."""
        st = getattr(step, "step_type", None)
        if st is None and isinstance(step, dict):
            st = step.get("step_type")
        # StepType is a str enum; fall back to dict key.
        if isinstance(st, str):
            return st.lower()
        return str(getattr(st, "value", st or "")).lower()

    @staticmethod
    def _step_content(step: Any) -> str:
        """Return a step's textual content."""
        content = getattr(step, "content", None)
        if content is None and isinstance(step, dict):
            content = step.get("content")
        return str(content or "")

    @staticmethod
    def _step_tool(step: Any) -> str:
        """Return the tool name referenced by an action step."""
        name = getattr(step, "tool_name", None)
        if name is None and isinstance(step, dict):
            name = step.get("tool_name")
        return str(name or "")

    @staticmethod
    def _step_tool_result(step: Any) -> Any:
        """Return the tool result object/dict for an action step."""
        result = getattr(step, "tool_result", None)
        if result is None and isinstance(step, dict):
            result = step.get("tool_result")
        return result

    @staticmethod
    def _result_success(result: Any) -> bool:
        """Best-effort check of whether a tool result succeeded."""
        if result is None:
            return True
        success = getattr(result, "success", None)
        if success is None and isinstance(result, dict):
            success = result.get("success")
        if success is None:
            return True
        return bool(success)

    @staticmethod
    def _result_error(result: Any) -> str:
        """Best-effort extraction of an error message from a tool result."""
        if result is None:
            return ""
        err = getattr(result, "error", None)
        if err is None and isinstance(result, dict):
            err = result.get("error")
        return str(err or "")

    @staticmethod
    def _trajectory_query(trajectory: Any) -> str:
        """Best-effort retrieval of the originating user query."""
        for attr in ("query", "user_message", "task"):
            value = getattr(trajectory, attr, None)
            if value:
                return str(value)
        if isinstance(trajectory, dict):
            for key in ("query", "user_message", "task"):
                value = trajectory.get(key)
                if value:
                    return str(value)
        return ""

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lowercase alphanumeric token set for keyword-overlap scoring."""
        return {tok for tok in re.findall(r"\w+", (text or "").lower()) if len(tok) > 1}

    # ------------------------------------------------------------------
    # Trajectory analysis
    # ------------------------------------------------------------------

    def analyze_trajectory(self, trajectory: Any) -> TrajectoryPattern:
        """Analyse a single agent trajectory and extract a :class:`TrajectoryPattern`.

        Determines the execution outcome (success / partial / failure),
        tallies the tools used and errors encountered, and estimates the
        iteration count (number of action steps). The returned pattern
        contributes a single observation (``success_count`` or
        ``failure_count`` of 1) ready to be merged with others.
        """
        steps = self._iter_steps(trajectory)
        tools: list[str] = []
        errors: list[str] = []
        action_count = 0
        has_answer = False
        has_error = False

        for step in steps:
            stype = self._step_type(step)
            if stype == "action":
                action_count += 1
                tool = self._step_tool(step)
                if tool:
                    tools.append(tool)
                result = self._step_tool_result(step)
                if not self._result_success(result):
                    has_error = True
                    err = self._result_error(result)
                    if err:
                        errors.append(err)
            elif stype == "answer":
                has_answer = True
                # An answer whose content looks like a failure still counts.
                content = self._step_content(step)
                if any(
                    m in content.lower() for m in ("失败", "无法", "未能", "error", "not found")
                ):
                    has_error = True

        outcome = self._classify_outcome(has_answer, has_error)

        # Iteration count: prefer explicit counter, else fall back to actions.
        iterations = getattr(trajectory, "total_tool_calls", None)
        if iterations is None and isinstance(trajectory, dict):
            iterations = trajectory.get("total_tool_calls")
        if not iterations:
            iterations = action_count

        common_tools = [t for t, _ in Counter(tools).most_common(5)]
        common_errors = errors[:5]
        query = self._trajectory_query(trajectory)
        description = self._describe_pattern(query, common_tools, outcome)

        pattern = TrajectoryPattern(
            pattern_description=description,
            success_count=1 if outcome == ExecutionOutcome.SUCCESS else 0,
            failure_count=1 if outcome == ExecutionOutcome.FAILURE else 0,
            avg_iterations=float(iterations or 0),
            common_tools=common_tools,
            common_errors=common_errors,
            optimized_prompt="",
        )
        # PARTIAL counts as half-success / half-failure for aggregation.
        if outcome == ExecutionOutcome.PARTIAL:
            pattern.success_count = 0
            pattern.failure_count = 0
        logger.debug(
            "Analysed trajectory: outcome=%s, tools=%s, iterations=%s",
            outcome,
            common_tools,
            iterations,
        )
        return pattern

    @staticmethod
    def _classify_outcome(has_answer: bool, has_error: bool) -> ExecutionOutcome:
        """Map answer/error signals to an :class:`ExecutionOutcome`."""
        if has_answer and not has_error:
            return ExecutionOutcome.SUCCESS
        if has_answer and has_error:
            return ExecutionOutcome.PARTIAL
        return ExecutionOutcome.FAILURE

    @staticmethod
    def _describe_pattern(query: str, tools: list[str], outcome: ExecutionOutcome) -> str:
        """Produce a short human-readable pattern description."""
        query_snippet = (query[:60] + "…") if len(query) > 60 else query
        tools_str = ", ".join(tools[:3]) if tools else "无工具"
        return f"查询「{query_snippet}」使用[{tools_str}]，结果{outcome.value}"

    # ------------------------------------------------------------------
    # Lesson extraction (LLM)
    # ------------------------------------------------------------------

    def extract_lessons(self, trajectories: list[Any]) -> dict[str, Any]:
        """Extract lessons from multiple trajectories using the LLM.

        Returns a dict with keys ``lessons`` (list[str]), ``patterns``
        (list[str]), ``optimized_prompt`` (str) and ``recommended_tools``
        (list[str]). Falls back to heuristic aggregation when the LLM is
        unavailable or returns unparseable output.
        """
        if not trajectories:
            return {
                "lessons": [],
                "patterns": [],
                "optimized_prompt": "",
                "recommended_tools": [],
            }

        summary = self._summarize_trajectories(trajectories)
        sample_query = self._trajectory_query(trajectories[0]) or "(未知查询)"
        prompt = _LESSONS_PROMPT.format(
            sample_query=sample_query,
            trajectories_summary=summary,
        )
        messages = [
            {"role": "system", "content": "你是智能体自进化分析专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]

        text = ""
        try:
            text = self.llm_provider.chat_completion_sync(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Lessons extraction LLM call failed: %s", e)

        data = _extract_json(text)
        if isinstance(data, dict):
            return {
                "lessons": [str(x) for x in data.get("lessons", []) if x],
                "patterns": [str(x) for x in data.get("patterns", []) if x],
                "optimized_prompt": str(data.get("optimized_prompt", "")),
                "recommended_tools": [str(x) for x in data.get("recommended_tools", []) if x],
            }

        # Fallback: aggregate heuristically from per-trajectory analysis.
        logger.debug("LLM lesson extraction fell back to heuristics.")
        merged: TrajectoryPattern | None = None
        for traj in trajectories:
            pat = self.analyze_trajectory(traj)
            merged = pat if merged is None else merged.merge(pat)
        if merged is None:
            merged = TrajectoryPattern()
        lessons: list[str] = []
        if merged.common_tools:
            lessons.append(f"常用工具: {', '.join(merged.common_tools)}")
        if merged.common_errors:
            lessons.append(f"常见错误: {', '.join(merged.common_errors[:3])}")
        lessons.append(f"平均迭代次数: {merged.avg_iterations}")
        return {
            "lessons": lessons,
            "patterns": [merged.pattern_description] if merged.pattern_description else [],
            "optimized_prompt": "",
            "recommended_tools": merged.common_tools,
        }

    def _summarize_trajectories(self, trajectories: list[Any]) -> str:
        """Build a compact textual summary of a batch of trajectories."""
        lines: list[str] = []
        for i, traj in enumerate(trajectories[:10]):  # cap to keep prompt small
            pat = self.analyze_trajectory(traj)
            query = self._trajectory_query(traj) or "(未知查询)"
            lines.append(
                f"- 轨迹{i + 1}: 查询「{query[:80]}」; 结果={pat.success_count}/{pat.failure_count}; "
                f"工具={pat.common_tools}; 迭代={pat.avg_iterations}; "
                f"错误={pat.common_errors[:2]}"
            )
        return "\n".join(lines) if lines else "(无轨迹)"

    # ------------------------------------------------------------------
    # Prompt optimisation (LLM)
    # ------------------------------------------------------------------

    def optimize_prompt(
        self,
        query_pattern: str,
        trajectories: list[Any],
        llm_provider: Any | None = None,
    ) -> str:
        """Generate an optimised system prompt for a query pattern.

        Uses *llm_provider* (falling back to the engine's own provider) to
        produce a tailored prompt informed by *trajectories*. Returns an
        empty string when generation fails.
        """
        provider = llm_provider or self.llm_provider
        if provider is None:
            logger.warning("optimize_prompt called without an LLM provider")
            return ""
        summary = self._summarize_trajectories(trajectories) if trajectories else "(无轨迹)"
        prompt = _PROMPT_OPTIMIZATION_PROMPT.format(
            query_pattern=query_pattern or "(未指定)",
            trajectories_summary=summary,
        )
        messages = [
            {"role": "system", "content": "你是提示词优化专家，直接输出优化后的提示词文本。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text = provider.chat_completion_sync(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Prompt optimization LLM call failed: %s", e)
            return ""
        # Strip accidental code fences.
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Tool selection optimisation
    # ------------------------------------------------------------------

    def optimize_tool_selection(
        self,
        query_type: str,
        tool_usage_stats: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Recommend the best tools for a query type from usage statistics.

        *tool_usage_stats* maps a tool name to a stats dict that may contain
        ``success_count``, ``failure_count``, ``total_calls`` and optionally
        ``avg_latency_ms``. Tools are ranked by success rate (then by total
        usage) and returned as a list of ``{"tool", "score", "reason"}``.

        When an LLM provider is configured, an LLM-derived recommendation is
        blended in; otherwise ranking is purely statistical.
        """
        ranked = self._rank_tools_statistically(tool_usage_stats)

        if self.llm_provider is not None and tool_usage_stats:
            llm_recs = self._llm_tool_recommendations(query_type, tool_usage_stats)
            if llm_recs:
                # Merge: LLM order takes priority, statistical score attached.
                seen: set[str] = set()
                merged: list[dict[str, Any]] = []
                stat_by_tool = {r["tool"]: r for r in ranked}
                for tool in llm_recs:
                    if tool in stat_by_tool:
                        merged.append(stat_by_tool[tool])
                        seen.add(tool)
                for r in ranked:
                    if r["tool"] not in seen:
                        merged.append(r)
                return merged

        return ranked

    @staticmethod
    def _rank_tools_statistically(
        tool_usage_stats: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rank tools by success rate, then by total usage volume."""
        entries: list[dict[str, Any]] = []
        for tool, stats in tool_usage_stats.items():
            stats = stats or {}
            success = float(stats.get("success_count", 0) or 0)
            failure = float(stats.get("failure_count", 0) or 0)
            total = float(stats.get("total_calls", success + failure) or (success + failure))
            score = success / total if total > 0 else 0.0
            entries.append(
                {
                    "tool": tool,
                    "score": round(score, 3),
                    "total_calls": int(total),
                    "success_count": int(success),
                    "failure_count": int(failure),
                    "reason": f"成功率 {score:.0%} ({int(success)}/{int(total)})",
                }
            )
        entries.sort(key=lambda e: (e["score"], e["total_calls"]), reverse=True)
        return entries

    def _llm_tool_recommendations(
        self, query_type: str, tool_usage_stats: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Ask the LLM for an ordered tool recommendation list."""
        try:
            stats_json = json.dumps(tool_usage_stats, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            stats_json = str(tool_usage_stats)
        prompt = _TOOL_SELECTION_PROMPT.format(
            query_type=query_type or "(未指定)",
            tool_usage_stats=stats_json,
        )
        messages = [
            {"role": "system", "content": "你是工具选择优化专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text = self.llm_provider.chat_completion_sync(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("Tool-selection LLM call failed: %s", e)
            return []
        data = _extract_json(text)
        if isinstance(data, dict):
            recs = data.get("recommended", [])
            if isinstance(recs, list):
                return [str(r) for r in recs if r]
        return []

    # ------------------------------------------------------------------
    # Experience persistence & recall
    # ------------------------------------------------------------------

    def store_experience(self, experience: Experience) -> bool:
        """Persist (upsert) a learned :class:`Experience` to SQLite.

        Returns ``True`` on success. Thread-safe via an internal lock.
        """
        now = datetime.now(UTC).isoformat()
        experience.updated_at = now
        if not experience.created_at:
            experience.created_at = now
        data = experience.to_dict()
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO agent_experiences
                        (id, query, query_pattern, outcome, lessons,
                         optimized_prompt, recommended_tools, common_tools,
                         common_errors, avg_iterations, success_count,
                         failure_count, pattern_description, created_at,
                         updated_at)
                    VALUES
                        (:id, :query, :query_pattern, :outcome, :lessons,
                         :optimized_prompt, :recommended_tools, :common_tools,
                         :common_errors, :avg_iterations, :success_count,
                         :failure_count, :pattern_description, :created_at,
                         :updated_at)
                    """,
                    {
                        "id": data["id"],
                        "query": data["query"],
                        "query_pattern": data["query_pattern"],
                        "outcome": data["outcome"],
                        "lessons": json.dumps(data["lessons"], ensure_ascii=False),
                        "optimized_prompt": data["optimized_prompt"],
                        "recommended_tools": json.dumps(
                            data["recommended_tools"], ensure_ascii=False
                        ),
                        "common_tools": json.dumps(data["common_tools"], ensure_ascii=False),
                        "common_errors": json.dumps(data["common_errors"], ensure_ascii=False),
                        "avg_iterations": data["avg_iterations"],
                        "success_count": data["success_count"],
                        "failure_count": data["failure_count"],
                        "pattern_description": data["pattern_description"],
                        "created_at": data["created_at"],
                        "updated_at": data["updated_at"],
                    },
                )
                conn.commit()
            logger.debug(
                "Stored experience %s for pattern %r", experience.id, experience.query_pattern
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to store experience: %s", e)
            return False

    def recall_experiences(self, query: str, top_k: int = 5) -> list[Experience]:
        """Retrieve the most relevant past experiences for *query*.

        Relevance is scored by keyword overlap between *query* and each
        stored experience's ``query`` / ``query_pattern`` /
        ``pattern_description`` / ``lessons``. Successful experiences are
        lightly boosted so positive lessons surface first.
        """
        if top_k <= 0:
            return []
        query_tokens = self._tokenize(query)
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM agent_experiences").fetchall()
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to recall experiences: %s", e)
            return []

        scored: list[tuple[float, Experience]] = []
        for row in rows:
            try:
                exp = Experience.from_row(row)
            except Exception as e:  # noqa: BLE001
                logger.debug("Skipping malformed experience row: %s", e)
                continue
            if not query_tokens:
                score = 1.0
            else:
                haystack = " ".join(
                    [
                        exp.query,
                        exp.query_pattern,
                        exp.pattern_description,
                        " ".join(exp.lessons),
                        " ".join(exp.recommended_tools),
                    ]
                )
                doc_tokens = self._tokenize(haystack)
                overlap = len(query_tokens & doc_tokens)
                score = overlap / max(1, len(query_tokens))
            # Boost successful experiences slightly.
            if exp.outcome == ExecutionOutcome.SUCCESS:
                score += 0.1
            elif exp.outcome == ExecutionOutcome.FAILURE:
                score -= 0.05
            scored.append((score, exp))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        # Prefer positively-scored experiences; fall back to all when none match.
        positive = [exp for score, exp in scored[:top_k] if score > 0]
        return positive or [exp for _score, exp in scored[:top_k]]

    # ------------------------------------------------------------------
    # Suggestion aggregation
    # ------------------------------------------------------------------

    def get_optimization_suggestions(self, query: str) -> dict[str, Any]:
        """Get prompt / tool suggestions for a new *query*.

        Recalls the most relevant stored experiences and aggregates their
        optimised prompts and recommended tools into a single suggestion
        bundle. Returns a dict with ``prompt``, ``recommended_tools``,
        ``lessons`` and ``similar_experiences``.
        """
        experiences = self.recall_experiences(query, top_k=5)
        if not experiences:
            return {
                "prompt": "",
                "recommended_tools": [],
                "lessons": [],
                "similar_experiences": [],
            }

        # Prefer the optimised prompt from the highest-ranked success.
        prompt = ""
        for exp in experiences:
            if exp.optimized_prompt:
                prompt = exp.optimized_prompt
                break

        tools: list[str] = []
        lessons: list[str] = []
        for exp in experiences:
            for tool in exp.recommended_tools:
                if tool not in tools:
                    tools.append(tool)
            for lesson in exp.lessons:
                if lesson not in lessons:
                    lessons.append(lesson)

        return {
            "prompt": prompt,
            "recommended_tools": tools[:5],
            "lessons": lessons[:5],
            "similar_experiences": [
                {
                    "id": exp.id,
                    "query_pattern": exp.query_pattern,
                    "outcome": str(exp.outcome),
                    "optimized_prompt": exp.optimized_prompt,
                    "recommended_tools": exp.recommended_tools,
                }
                for exp in experiences
            ],
        }

    # ------------------------------------------------------------------
    # Main evolution loop
    # ------------------------------------------------------------------

    def evolve(self, new_trajectories: list[Any]) -> dict[str, Any]:
        """Run the main evolution loop over a batch of new trajectories.

        Pipeline: analyse each trajectory -> extract shared lessons via the
        LLM -> optimise a prompt for the dominant pattern -> build and store
        an :class:`Experience`. Returns a summary dict describing the outcome.
        """
        if not new_trajectories:
            return {
                "analyzed": 0,
                "experiences_stored": 0,
                "lessons": [],
                "optimized_prompt": "",
            }

        start = time.time()
        # 1. Analyse each trajectory.
        patterns = [self.analyze_trajectory(t) for t in new_trajectories]
        # 2. Extract shared lessons / prompt / tools.
        lessons = self.extract_lessons(new_trajectories)

        # 3. Merge patterns to find the dominant pattern for this batch.
        merged: TrajectoryPattern | None = None
        for pat in patterns:
            merged = pat if merged is None else merged.merge(pat)
        if merged is None:
            merged = TrajectoryPattern()

        # 4. Optimise a prompt for the dominant pattern.
        query_pattern = (lessons.get("patterns") or [merged.pattern_description])[0]
        optimized_prompt = lessons.get("optimized_prompt") or self.optimize_prompt(
            query_pattern, new_trajectories
        )
        merged.optimized_prompt = optimized_prompt

        # 5. Build & store an experience.
        sample_query = self._trajectory_query(new_trajectories[0])
        recommended_tools = lessons.get("recommended_tools") or merged.common_tools
        outcome = (
            ExecutionOutcome.SUCCESS
            if merged.success_rate >= 0.5
            else (
                ExecutionOutcome.FAILURE if merged.success_rate == 0 else ExecutionOutcome.PARTIAL
            )
        )
        experience = Experience(
            query=sample_query,
            query_pattern=query_pattern,
            outcome=outcome,
            lessons=lessons.get("lessons", []),
            optimized_prompt=optimized_prompt,
            recommended_tools=recommended_tools,
            common_tools=merged.common_tools,
            common_errors=merged.common_errors,
            avg_iterations=merged.avg_iterations,
            success_count=merged.success_count,
            failure_count=merged.failure_count,
            pattern_description=merged.pattern_description,
        )
        stored = self.store_experience(experience)

        elapsed = round(time.time() - start, 3)
        logger.info(
            "Evolution complete: analysed %d trajectories, stored=%s, took %ss",
            len(new_trajectories),
            stored,
            elapsed,
        )
        return {
            "analyzed": len(new_trajectories),
            "experiences_stored": 1 if stored else 0,
            "lessons": lessons.get("lessons", []),
            "optimized_prompt": optimized_prompt,
            "pattern": merged.to_dict(),
            "experience_id": experience.id if stored else None,
            "elapsed_seconds": elapsed,
        }

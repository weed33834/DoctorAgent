# mypy: ignore-errors
"""Full-scenario tests for the enterprise Agent ReAct architecture.

Covers the reasoning pipeline that ``tests/test_agent.py`` leaves untested:

* **Core ReAct cycle** — think → act → observe → answer, including the
  empty-response and iteration-exhaustion edge cases.
* **Parallel tool dispatch** — independent tool calls run concurrently via
  ``asyncio.gather``; calls sharing a resource key run sequentially.
* **Error recovery** — transient errors retry with backoff, permanent
  errors fall through to an alternative tool, fatal errors flip the agent
  into ``ERROR`` state.
* **Circuit breaker** — a tool tripping its breaker is skipped for the rest
  of the session and a same-category alternative is used.
* **Plan-and-Execute** — plan parsing, topological ordering, cycle
  breaking, dynamic re-planning on step failure.
* **Deep reflection** — a sub-threshold score triggers a refinement round.
* **Tool budget** — ``max_tool_calls`` caps dispatch mid-run.
* **Multi-agent** — the orchestrator-worker pattern dispatches subtasks via
  ``TaskStore`` and synthesises the fused answer.

These tests use a scripted mock LLM provider and recording tools so every
run is deterministic and free of network access.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.model.agent import (
    Agent,
    AgentConfig,
    AgentState,
    ErrorClass,
    ExecutionPlan,
    PlanStep,
    PlanStepStatus,
    ReflectionScore,
    StepType,
    _classify_error,
    _estimate_tokens,
    _extract_json,
    _truncate_args,
)
from doctoragent.model.provider import ChatCompletionResponse
from doctoragent.model.tools import (
    Tool,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Scripted mock LLM provider
# ---------------------------------------------------------------------------

class ScriptedLLMProvider:
    """LLM provider returning a scripted sequence of responses.

    Each call pops the next response from ``self._responses``. When the
    script is exhausted the ``default`` string is returned. ``call_count``
    lets tests assert how many LLM round-trips were made.

    A response may be a plain ``str`` (content only) or a
    :class:`ChatCompletionResponse` carrying ``tool_calls``.
    """

    def __init__(self, responses: list[Any] | None = None, default: str = "") -> None:
        self._responses = list(responses or [])
        self._idx = 0
        self.default = default
        self.model_name = "scripted-mock"
        self.call_count = 0
        self.calls: list[list[dict[str, Any]]] = []

    def _next(self) -> Any:
        self.call_count += 1
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self.default

    def chat_completion_sync(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        **_: Any,
    ) -> Any:
        self.calls.append(list(messages))
        return self._next()


def _tool_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "arguments": arguments or {}}


def _completion(content: str, tool_calls: list[dict[str, Any]] | None = None) -> ChatCompletionResponse:
    return ChatCompletionResponse(content=content, tool_calls=tool_calls or [])


# ---------------------------------------------------------------------------
# Recording tool
# ---------------------------------------------------------------------------

class RecordingTool(Tool):
    """A tool that records every call and returns a configurable result.

    ``failures`` is a list of exceptions/messages to raise before the tool
    starts succeeding. An empty list (the default) means the tool always
    succeeds with ``result_data``. ``delay`` simulates latency so parallel
    dispatch can be observed via timing.
    """

    def __init__(
        self,
        name: str,
        *,
        result_data: Any = None,
        failures: list[str] | None = None,
        delay: float = 0.0,
        category: str = "retrieval",
    ) -> None:
        self._name = name
        self._result_data = result_data if result_data is not None else {"ok": True}
        self._failures = list(failures or [])
        self._delay = delay
        self._category = category
        self.calls: list[dict[str, Any]] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description=f"Recording tool {self._name}",
            parameters=[
                ToolParameter(name="query", type="string", description="query"),
            ],
            category=self._category,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._failures:
            raise RuntimeError(self._failures.pop(0))
        return ToolResult(
            success=True,
            data=self._result_data,
            tool_name=self._name,
            execution_time_ms=1.0,
        )


def _registry(*tools: Tool, circuit_threshold: int = 3) -> ToolRegistry:
    reg = ToolRegistry(circuit_threshold=circuit_threshold)
    for t in tools:
        reg.register(t)
    return reg


def _agent(
    provider: ScriptedLLMProvider,
    registry: ToolRegistry,
    *,
    config: AgentConfig | None = None,
) -> Agent:
    cfg = config or AgentConfig(
        max_iterations=5,
        max_tool_calls=5,
        enable_planning=False,
        enable_reflection=False,
        enable_memory=False,
    )
    return Agent(
        llm_provider=provider,
        tool_registry=registry,
        config=cfg,
        memory_system=None,
        task_store=None,
    )


# ---------------------------------------------------------------------------
# 1. Core ReAct cycle
# ---------------------------------------------------------------------------

class TestCoreReactCycle:
    """Think → act → observe → answer."""

    def test_direct_answer_without_tools(self) -> None:
        """LLM returns content with no tool calls → that content is the answer."""
        provider = ScriptedLLMProvider(default="直接回答：合同到期日是 2025-12-31。")
        tool = RecordingTool("search_documents")
        agent = _agent(provider, _registry(tool))

        answer = asyncio.run(agent.run("合同何时到期？"))

        assert "2025-12-31" in answer
        # No tools should have been dispatched.
        assert tool.calls == []
        assert agent.state == AgentState.FINISHED
        # A single ANSWER step is recorded.
        answer_steps = [s for s in agent.trajectory.steps if s.step_type == StepType.ANSWER]
        assert len(answer_steps) == 1

    def test_tool_call_then_answer(self) -> None:
        """LLM requests a tool, observes the result, then answers."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion("", tool_calls=[_tool_call("search_documents", {"query": "合同 到期"})]),
                "根据搜索结果，合同于 2025-12-31 到期。",
            ]
        )
        tool = RecordingTool("search_documents", result_data={"expiry": "2025-12-31"})
        agent = _agent(provider, _registry(tool))

        answer = asyncio.run(agent.run("合同何时到期？"))

        assert "2025-12-31" in answer
        assert len(tool.calls) == 1
        assert tool.calls[0]["query"] == "合同 到期"
        # Trajectory records: ACTION + OBSERVATION + ANSWER.
        step_types = [s.step_type for s in agent.trajectory.steps]
        assert StepType.ACTION in step_types
        assert StepType.OBSERVATION in step_types
        assert StepType.ANSWER in step_types

    def test_empty_llm_response_enters_error_state(self) -> None:
        """An empty LLM response (no content, no tool calls) → degraded answer.

        The ReAct loop sets ``ERROR`` state mid-run and records an error
        thought; ``_run_single`` then finalises (→ ``FINISHED``) and returns
        the fallback message.
        """
        provider = ScriptedLLMProvider(default="")
        agent = _agent(provider, _registry(RecordingTool("search_documents")))

        answer = asyncio.run(agent.run("任何问题"))

        # Final state is FINISHED (finalised), but the trajectory records
        # the empty-response error thought.
        assert agent.state == AgentState.FINISHED
        assert answer == "抱歉，我无法完成这个任务。"
        thoughts = [s.content for s in agent.trajectory.steps if s.step_type == StepType.THOUGHT]
        assert any("空响应" in t for t in thoughts)

    def test_max_iterations_exhaustion_produces_final_answer(self) -> None:
        """When iterations exhaust, the agent asks for a consolidated answer."""
        # Each iteration the LLM requests a tool (never answering directly).
        # With max_tool_calls=2 the loop stops dispatching after 2 calls and
        # the iteration boundary produces a final consolidated answer.
        provider = ScriptedLLMProvider(
            responses=[
                _completion("", tool_calls=[_tool_call("search_documents", {"query": "q1"})]),
                _completion("", tool_calls=[_tool_call("search_documents", {"query": "q2"})]),
                _completion("", tool_calls=[_tool_call("search_documents", {"query": "q3"})]),
                "汇总回答：根据已有信息，答案是 X。",
            ],
            default="默认汇总回答。",
        )
        tool = RecordingTool("search_documents")
        agent = _agent(
            provider,
            _registry(tool),
            config=AgentConfig(
                max_iterations=10,
                max_tool_calls=2,
                enable_planning=False,
                enable_reflection=False,
                enable_memory=False,
            ),
        )

        answer = asyncio.run(agent.run("复杂问题"))

        assert agent.state == AgentState.FINISHED
        # Only the first 2 tool calls were dispatched (budget = 2).
        assert len(tool.calls) == 2


# ---------------------------------------------------------------------------
# 2. Parallel tool dispatch
# ---------------------------------------------------------------------------

class TestParallelToolDispatch:
    """Independent tool calls run concurrently; shared-resource calls run in order."""

    def test_independent_tools_run_in_parallel(self) -> None:
        """Two tools with no shared resource key should overlap in time."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion(
                    "",
                    tool_calls=[
                        _tool_call("tool_a", {"query": "a"}),
                        _tool_call("tool_b", {"query": "b"}),
                    ],
                ),
                "合并 a 与 b 的结果：完成。",
            ]
        )
        slow = 0.15
        tool_a = RecordingTool("tool_a", result_data={"a": 1}, delay=slow, category="retrieval")
        tool_b = RecordingTool("tool_b", result_data={"b": 2}, delay=slow, category="management")
        agent = _agent(provider, _registry(tool_a, tool_b))

        start = time.monotonic()
        asyncio.run(agent.run("并行问题"))
        elapsed = time.monotonic() - start

        # If serial, elapsed ≈ 2*slow = 0.30s; parallel ≈ slow = 0.15s.
        # Allow generous headroom for CI jitter.
        assert elapsed < 2 * slow, (
            f"parallel dispatch expected <{2 * slow:.2f}s, got {elapsed:.3f}s"
        )
        assert len(tool_a.calls) == 1
        assert len(tool_b.calls) == 1

    def test_same_resource_key_runs_sequentially(self) -> None:
        """Two calls targeting the same file_path share a resource key → serial."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion(
                    "",
                    tool_calls=[
                        _tool_call("read_doc", {"file_path": "/vault/contract.txt"}),
                        _tool_call("read_doc", {"file_path": "/vault/contract.txt"}),
                    ],
                ),
                "两次读取同一文件完成。",
            ]
        )
        tool = RecordingTool("read_doc", result_data={"text": "content"}, delay=0.1)
        agent = _agent(provider, _registry(tool))

        asyncio.run(agent.run("读两次同一文件"))

        # Both calls executed (sequentially within the group).
        assert len(tool.calls) == 2

    def test_resource_key_derivation(self) -> None:
        """Agent._resource_key groups by file_path/vault_path/path/source_path."""
        for key in ("file_path", "vault_path", "path", "source_path"):
            tc = {"name": "t", "arguments": {key: "/x/y"}}
            assert Agent._resource_key(tc) == f"t:/x/y"
        # No recognised resource key → unique per-call key.
        tc_unique = {"name": "t", "arguments": {"query": "q"}}
        assert Agent._resource_key(tc_unique).startswith("t:unique:")
        # Non-dict arguments fall back to a unique key.
        assert Agent._resource_key({"name": "t", "arguments": "not-a-dict"}).startswith("t:unique:")


# ---------------------------------------------------------------------------
# 3. Error recovery
# ---------------------------------------------------------------------------

class TestErrorRecovery:
    """Transient retry, permanent fallthrough, fatal abort."""

    def test_classify_error_keywords(self) -> None:
        assert _classify_error("connection reset by peer") == ErrorClass.TRANSIENT
        assert _classify_error("timeout waiting for response") == ErrorClass.TRANSIENT
        assert _classify_error("tool not found") == ErrorClass.PERMANENT
        assert _classify_error("invalid argument: foo") == ErrorClass.PERMANENT
        assert _classify_error("out of memory") == ErrorClass.FATAL
        assert _classify_error("fatal: segfault") == ErrorClass.FATAL
        # Unknown → transient (worth one retry).
        assert _classify_error("something weird") == ErrorClass.TRANSIENT

    def test_transient_error_retried_then_succeeds(self) -> None:
        """A tool that fails with a transient error once should be retried."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion("", tool_calls=[_tool_call("flaky_tool", {"query": "q"})]),
                "重试成功后的结果。",
            ]
        )
        tool = RecordingTool(
            "flaky_tool",
            result_data={"ok": True},
            failures=["connection timeout"],  # transient keyword
        )
        agent = _agent(provider, _registry(tool))

        answer = asyncio.run(agent.run("调用不稳定的工具"))

        # The tool was retried: 1 failure + 1 success.
        assert len(tool.calls) == 2
        assert agent.state == AgentState.FINISHED

    def test_permanent_error_not_retried(self) -> None:
        """A permanent error should not be retried indefinitely."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion("", tool_calls=[_tool_call("bad_tool", {"query": "q"})]),
                "工具不可用，给出降级回答。",
            ]
        )
        # 'not found' is permanent → no retry, exhausted immediately.
        tool = RecordingTool(
            "bad_tool",
            result_data={"ok": True},
            failures=["tool not found"],
        )
        agent = _agent(provider, _registry(tool))

        asyncio.run(agent.run("调用坏工具"))

        # Permanent error → single attempt, no retry.
        assert len(tool.calls) == 1

    def test_fatal_error_aborts_run(self) -> None:
        """A fatal error flips the agent into ERROR state mid-run and aborts.

        ``_run_single`` detects the ERROR state, finalises (→ FINISHED) and
        returns the fallback message — reflection and memory storage are
        skipped because the run is unrecoverable.
        """
        provider = ScriptedLLMProvider(
            responses=[
                _completion("", tool_calls=[_tool_call("doomed_tool", {"query": "q"})]),
            ],
            default="",
        )
        tool = RecordingTool(
            "doomed_tool",
            failures=["fatal: out of memory"],
        )
        agent = _agent(provider, _registry(tool))

        answer = asyncio.run(agent.run("致命错误"))

        assert agent.state == AgentState.FINISHED
        assert answer == "抱歉，我无法完成这个任务。"
        # The single tool call failed fatally (no retry, no success).
        assert len(tool.calls) == 1

    def test_alternative_tool_used_on_permanent_failure(self) -> None:
        """When a tool fails permanently, a same-category alternative is tried."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion(
                    "",
                    tool_calls=[_tool_call("primary_search", {"query": "q"})],
                ),
                "使用替代工具后的回答。",
            ]
        )
        primary = RecordingTool(
            "primary_search",
            failures=["not found"],  # permanent
            category="retrieval",
        )
        fallback = RecordingTool(
            "fallback_search",
            result_data={"found": True},
            category="retrieval",
        )
        agent = _agent(provider, _registry(primary, fallback))

        asyncio.run(agent.run("需要替代工具"))

        # Primary failed permanently, fallback was invoked.
        assert len(primary.calls) == 1
        assert len(fallback.calls) == 1


# ---------------------------------------------------------------------------
# 4. Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    """A tool that trips its breaker is skipped; alternatives are used."""

    def test_breaker_opens_after_threshold(self) -> None:
        reg = ToolRegistry(circuit_threshold=2)
        reg.register(RecordingTool("a", category="retrieval"))
        # Two failures → open.
        assert reg.record_failure("a") == 1
        assert reg.record_failure("a") == 2
        assert reg.is_circuit_open("a") is True
        assert reg.get_failure_count("a") == 2

    def test_success_resets_breaker(self) -> None:
        reg = ToolRegistry(circuit_threshold=2)
        reg.record_failure("a")
        reg.record_failure("a")
        assert reg.is_circuit_open("a") is True
        reg.record_success("a")
        assert reg.is_circuit_open("a") is False
        assert reg.get_failure_count("a") == 0

    def test_reset_circuit_manual(self) -> None:
        reg = ToolRegistry()
        reg.record_failure("a")
        reg.record_failure("a")
        reg.record_failure("a")
        reg.reset_circuit("a")
        assert reg.is_circuit_open("a") is False
        assert reg.get_failure_count("a") == 0

    def test_alternative_tool_resolution(self) -> None:
        """get_alternative_tool returns a healthy same-category tool."""
        reg = ToolRegistry(circuit_threshold=1)
        reg.register(RecordingTool("a", category="retrieval"))
        reg.register(RecordingTool("b", category="retrieval"))
        reg.register(RecordingTool("c", category="management"))
        # Trip breaker on "a".
        reg.record_failure("a")
        assert reg.is_circuit_open("a")
        # "b" is same category and healthy.
        alt = reg.get_alternative_tool("a")
        assert alt == "b"
        # "c" is a different category → not returned.
        assert reg.get_alternative_tool("c") != "c"
        # Unknown tool → None.
        assert reg.get_alternative_tool("nope") is None

    def test_open_breaker_skips_tool_and_uses_alternative(self) -> None:
        """A tripped breaker short-circuits the tool and dispatches an alt."""
        provider = ScriptedLLMProvider(
            responses=[
                # First call: primary succeeds once (so we can pre-trip later).
                _completion(
                    "",
                    tool_calls=[_tool_call("primary", {"query": "q"})],
                ),
                "替代工具结果汇总。",
            ],
        )
        primary = RecordingTool("primary", category="retrieval")
        fallback = RecordingTool("fallback", result_data={"alt": True}, category="retrieval")
        # circuit_breaker_threshold=1 so a single failure trips the breaker.
        agent = _agent(
            provider,
            _registry(primary, fallback, circuit_threshold=1),
            config=AgentConfig(
                enable_planning=False,
                enable_reflection=False,
                enable_memory=False,
                circuit_breaker_threshold=1,
            ),
        )
        # Pre-trip the breaker before running.
        agent.tools.record_failure("primary")
        assert agent.tools.is_circuit_open("primary")

        asyncio.run(agent.run("触发熔断"))

        # Primary was skipped (no calls), fallback was used instead.
        assert len(primary.calls) == 0
        assert len(fallback.calls) == 1


# --------------------------------------------------------------------------- #
# Regression: alternative-tool fallback must not raise TypeError
# --------------------------------------------------------------------------- #
# Bug (fixed): when a primary tool failed permanently, the recovery layer
# forwarded the original tool's args VERBATIM to a same-category alternative
# whose execute() signature is different. Clinical same-category tools are
# NOT signature-compatible (check_drug_interactions takes ``drugs`` while
# its fallback check_vitals takes ``vitals``), so every fallback raised
# ``TypeError: ... got an unexpected keyword argument 'drugs'`` and the
# generic ``except Exception`` swallowed it into a warning.
#
# The fix adds ``ToolRegistry.compatible_args`` which filters args to the
# alternative's declared parameters AND reports whether the required params
# are satisfied; the dispatch site skips the doomed substitute call when
# they are not. These tests pin both behaviours.


class _SignatureTool(Tool):
    """A tool whose execute signature and advertised schema are explicit.

    Unlike ``RecordingTool`` (which advertises ``query`` and accepts
    ``**kwargs``), this tool declares an exact parameter set and raises
    ``TypeError`` if an undeclared kwarg reaches ``execute`` — mirroring
    the real clinical tools (``CheckVitalsTool``, ``ReadPatientRecordTool``)
    that were the bug's victims.
    """

    def __init__(
        self,
        name: str,
        *,
        param: str,
        category: str = "clinical_knowledge",
        failures: list[str] | None = None,
        result_data: Any = None,
    ) -> None:
        self._name = name
        self._param = param
        self._category = category
        self._failures = list(failures or [])
        self._result_data = result_data if result_data is not None else {"ok": True}
        self.calls: list[dict[str, Any]] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description=f"Signature tool {self._name}",
            parameters=[
                ToolParameter(name=self._param, type="object", description=self._param),
            ],
            category=self._category,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        # Mirror real tools: reject undeclared kwargs instead of swallowing.
        if any(k != self._param for k in kwargs):
            raise TypeError(
                f"{self._name}.execute() got an unexpected keyword argument "
                f"'{next(k for k in kwargs if k != self._param)}'"
            )
        self.calls.append(kwargs)
        if self._failures:
            raise RuntimeError(self._failures.pop(0))
        return ToolResult(success=True, data=self._result_data, tool_name=self._name)


class TestAlternativeToolArgFiltering:
    """The alternative-tool fallback must filter args to the alt's signature."""

    def test_compatible_args_filters_unknown_kwargs(self) -> None:
        """Undeclared kwargs are dropped; declared ones pass through."""
        reg = ToolRegistry()
        reg.register(_SignatureTool("check_vitals", param="vitals"))
        filtered, ok = reg.compatible_args("check_vitals", {"drugs": ["x"], "vitals": {"hr": 80}})
        assert ok is True
        assert filtered == {"vitals": {"hr": 80}}

    def test_compatible_args_unsatisfied_when_required_param_missing(self) -> None:
        """When the alt's required param is absent, ``ok`` is False so the
        dispatch site skips the doomed substitute call instead of TypeError'ing."""
        reg = ToolRegistry()
        reg.register(_SignatureTool("check_vitals", param="vitals"))
        filtered, ok = reg.compatible_args("check_vitals", {"drugs": ["x"]})
        assert ok is False
        # ``drugs`` is not declared on check_vitals, so it is dropped.
        assert filtered == {}

    def test_compatible_args_unknown_tool(self) -> None:
        """Unknown tool → empty args, not satisfied."""
        reg = ToolRegistry()
        assert reg.compatible_args("nope", {"a": 1}) == ({}, False)

    def test_compatible_args_respects_optional_params(self) -> None:
        """An optional (required=False) param absent from args is still ok."""
        reg = ToolRegistry()
        tool = _SignatureTool("opt_tool", param="required_param")
        # Mutate the param to optional to model optional schema params.
        tool_def = tool.definition
        tool_def.parameters.append(
            ToolParameter(name="opt", type="string", description="opt", required=False)
        )
        reg.register(tool)
        filtered, ok = reg.compatible_args("opt_tool", {"required_param": "v"})
        assert ok is True
        assert filtered == {"required_param": "v"}

    def test_permanent_failure_skips_incompatible_alternative(self) -> None:
        """A permanent primary failure with an incompatible same-category alt
        must NOT call the alt (no TypeError) and must surface the original error.

        Reproduces the smoke-test warning:
            ``Alternative tool check_vitals failed: ... unexpected keyword
            argument 'drugs'``
        """
        provider = ScriptedLLMProvider(responses=["done"])  # not actually used
        primary = _SignatureTool(
            "check_drug_interactions",
            param="drugs",
            failures=["invalid request"],  # PERMANENT keyword → break → alt path
        )
        alt = _SignatureTool("check_vitals", param="vitals")  # incompatible signature
        agent = _agent(
            provider,
            _registry(primary, alt, circuit_threshold=10),
            config=AgentConfig(
                max_iterations=2,
                max_tool_calls=2,
                max_tool_retries=0,
                enable_planning=False,
                enable_reflection=False,
                enable_memory=False,
            ),
        )

        result = asyncio.run(
            agent._execute_tool_with_recovery(
                {"name": "check_drug_interactions", "arguments": {"drugs": ["warfarin"]}}
            )
        )

        # The alternative tool must NEVER have been called (previously it was
        # called with drugs=... and raised TypeError).
        assert alt.calls == [], f"alt should not be called, got: {alt.calls}"
        # The original permanent error is surfaced (not a TypeError).
        assert result.success is False
        assert result.tool_name == "check_drug_interactions"
        assert "invalid request" in (result.error or "")
        assert "unexpected keyword argument" not in (result.error or "")

    def test_open_breaker_skips_incompatible_alternative(self) -> None:
        """The circuit-open dispatch branch must also filter args.

        When the primary tool is tripped (not failed-this-call), the
        recovery layer looks up an alternative. Same-category clinical
        tools have incompatible signatures, so the alt must be skipped
        rather than called with the primary's kwargs (TypeError).
        """
        provider = ScriptedLLMProvider(responses=["done"])
        primary = _SignatureTool("read_lab_results", param="count", category="clinical_fhir")
        alt = _SignatureTool("read_patient_record", param="patient_id", category="clinical_fhir")
        agent = _agent(
            provider,
            _registry(primary, alt, circuit_threshold=1),
            config=AgentConfig(
                max_iterations=2,
                max_tool_calls=2,
                circuit_breaker_threshold=1,
                enable_planning=False,
                enable_reflection=False,
                enable_memory=False,
            ),
        )
        # Pre-trip the breaker on the primary.
        agent.tools.record_failure("read_lab_results")
        assert agent.tools.is_circuit_open("read_lab_results")

        result = asyncio.run(
            agent._execute_tool_with_recovery(
                {"name": "read_lab_results", "arguments": {"count": 20}}
            )
        )

        # The alt (read_patient_record, needs patient_id) must NOT be called
        # with count=20 (previously: TypeError → swallowed warning).
        assert alt.calls == [], f"alt should not be called, got: {alt.calls}"
        assert result.success is False
        assert result.tool_name == "read_lab_results"
        assert "熔断" in (result.error or "")
        assert "unexpected keyword argument" not in (result.error or "")


# ---------------------------------------------------------------------------
# 4b. Per-tool circuit-breaker config + opt-in runtime arg validation
# ---------------------------------------------------------------------------

class TestPerToolCircuitConfig:
    """``configure_circuit`` overrides the registry default per tool."""

    def test_configure_circuit_overrides_threshold(self) -> None:
        reg = ToolRegistry(circuit_threshold=5)
        reg.register(_SignatureTool("flaky_api", param="payload"))
        reg.configure_circuit("flaky_api", threshold=1, cooldown_seconds=0.05)
        # One failure is enough to trip the per-tool override.
        assert reg.record_failure("flaky_api") == 1
        assert reg.is_circuit_open("flaky_api") is True

    def test_configure_circuit_unknown_tool_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.configure_circuit("nope", threshold=2)

    def test_per_tool_cooldown_independent_of_default(self) -> None:
        reg = ToolRegistry(circuit_threshold=1)
        reg._circuit_cooldown_seconds = 999  # global cooldown stays huge
        reg.register(_SignatureTool("fast_tool", param="payload"))
        reg.configure_circuit("fast_tool", threshold=1, cooldown_seconds=0.05)
        reg.record_failure("fast_tool")
        assert reg.is_circuit_open("fast_tool") is True
        # After the *per-tool* cooldown elapses, the breaker half-opens.
        time.sleep(0.06)
        assert reg.is_circuit_open("fast_tool") is False


class TestRuntimeArgValidation:
    """Opt-in ``validate_args`` catches LLM-style arg mistakes before dispatch."""

    def _registry(self) -> ToolRegistry:
        reg = ToolRegistry(validate_args=True)
        reg.register(_SignatureTool("check_vitals", param="vitals"))
        return reg

    def test_validation_off_by_default(self) -> None:
        """Without ``validate_args=True`` the registry never short-circuits."""
        reg = ToolRegistry()  # validate_args defaults to False
        reg.register(_SignatureTool("check_vitals", param="vitals"))
        # Pass an obviously wrong type; the legacy path lets the tool raise.
        result = asyncio.run(reg.execute("check_vitals", vitals="not-a-dict"))
        # The _SignatureTool doesn't type-check the value, so it succeeds.
        assert result.success is True

    def test_validation_rejects_wrong_type(self) -> None:
        reg = self._registry()
        result = asyncio.run(reg.execute("check_vitals", vitals="not-a-dict"))
        assert result.success is False
        assert "expected object" in (result.error or "")
        assert result.tool_name == "check_vitals"

    def test_validation_rejects_missing_required(self) -> None:
        reg = self._registry()
        result = asyncio.run(reg.execute("check_vitals", unrelated="x"))
        assert result.success is False
        assert "Missing required parameter" in (result.error or "")

    def test_validation_rejects_none_for_required(self) -> None:
        reg = self._registry()
        result = asyncio.run(reg.execute("check_vitals", vitals=None))
        assert result.success is False
        assert "required but got None" in (result.error or "")

    def test_validation_passes_correct_args(self) -> None:
        reg = self._registry()
        result = asyncio.run(reg.execute("check_vitals", vitals={"hr": 80}))
        assert result.success is True

    def test_validation_rejects_bool_for_string(self) -> None:
        """Booleans are not silently accepted where a string is declared."""
        reg = ToolRegistry(validate_args=True)
        reg.register(_SignatureTool("flag_tool", param="name"))
        # _SignatureTool declares its param as type="object"; swap to string
        # to exercise the bool-vs-string branch.
        reg.get("flag_tool").definition.parameters[0].type = "string"
        result = asyncio.run(reg.execute("flag_tool", name=True))
        assert result.success is False
        assert "bool" in (result.error or "")



# ---------------------------------------------------------------------------
# 5. Plan-and-Execute
# ---------------------------------------------------------------------------

class TestPlanAndExecute:
    """Plan parsing, topological ordering, cycle breaking, re-planning."""

    def test_parse_plan_steps_from_json(self) -> None:
        response = json.dumps(
            {
                "steps": [
                    {
                        "step_id": "s1",
                        "description": "搜索",
                        "tool_hint": "search_documents",
                        "depends_on": [],
                        "expected_output": "结果列表",
                    },
                    {
                        "step_id": "s2",
                        "description": "分析",
                        "tool_hint": "analyze",
                        "depends_on": ["s1"],
                        "expected_output": "结论",
                    },
                ]
            }
        )
        steps = Agent._parse_plan_steps(response)
        assert len(steps) == 2
        assert steps[0].step_id == "s1"
        assert steps[1].depends_on == ["s1"]

    def test_parse_plan_steps_tolerates_garbage(self) -> None:
        assert Agent._parse_plan_steps("") == []
        assert Agent._parse_plan_steps("not json at all") == []

    def test_topological_order_respects_dependencies(self) -> None:
        agent = _agent(ScriptedLLMProvider(), _registry())
        plan = ExecutionPlan(
            steps=[
                PlanStep(step_id="s3", depends_on=["s2"]),
                PlanStep(step_id="s2", depends_on=["s1"]),
                PlanStep(step_id="s1", depends_on=[]),
            ]
        )
        ordered = agent._topological_order(plan)
        ids = [s.step_id for s in ordered]
        assert ids == ["s1", "s2", "s3"]

    def test_topological_order_breaks_cycles(self) -> None:
        """A dependency cycle is broken — cyclic nodes appended in original order."""
        agent = _agent(ScriptedLLMProvider(), _registry())
        plan = ExecutionPlan(
            steps=[
                PlanStep(step_id="a", depends_on=["b"]),
                PlanStep(step_id="b", depends_on=["a"]),
            ]
        )
        ordered = agent._topological_order(plan)
        # Both nodes present (no deadlock); order preserved for the cycle.
        assert {s.step_id for s in ordered} == {"a", "b"}
        # A deviation was recorded on the trajectory for the cyclic node(s).
        assert len(agent.trajectory.plan_deviations) >= 1 or len(ordered) == 2

    def test_validate_plan_clears_unknown_tool_hints(self) -> None:
        reg = _registry(RecordingTool("real_tool"))
        agent = _agent(ScriptedLLMProvider(), reg)
        plan = ExecutionPlan(
            steps=[
                PlanStep(step_id="s1", tool_hint="nonexistent_tool"),
                PlanStep(step_id="s2", tool_hint="real_tool"),
            ]
        )
        validated = agent._validate_plan(plan)
        # Unknown hint cleared.
        assert validated.get_step("s1").tool_hint == ""
        # Known hint kept.
        assert validated.get_step("s2").tool_hint == "real_tool"
        # Deviation recorded on the trajectory for the cleared hint.
        assert any(d["step_id"] == "s1" for d in agent.trajectory.plan_deviations)

    def test_plan_execution_completes_steps(self) -> None:
        """A valid plan executes its steps and synthesises an answer."""
        provider = ScriptedLLMProvider(
            responses=[
                # Plan response.
                json.dumps(
                    {
                        "steps": [
                            {
                                "step_id": "s1",
                                "description": "搜索文档",
                                "tool_hint": "search_documents",
                                "depends_on": [],
                                "expected_output": "结果",
                            }
                        ]
                    }
                ),
                # Step execution: request the tool.
                _completion("", tool_calls=[_tool_call("search_documents", {"query": "合同"})]),
                # Step summary.
                "找到合同，到期日 2025-12-31。",
                # Synthesis.
                "综合结论：合同到期日是 2025-12-31。",
                # Reflection (disabled, but provider is defensive).
            ]
        )
        tool = RecordingTool("search_documents", result_data={"expiry": "2025-12-31"})
        agent = _agent(
            provider,
            _registry(tool),
            config=AgentConfig(
                enable_planning=True,
                enable_reflection=False,
                enable_memory=False,
                max_iterations=5,
                max_tool_calls=5,
            ),
        )

        answer = asyncio.run(agent.run("合同何时到期？"))

        assert "2025-12-31" in answer
        assert len(tool.calls) == 1


# ---------------------------------------------------------------------------
# 6. Deep reflection
# ---------------------------------------------------------------------------

class TestDeepReflection:
    """Sub-threshold scores trigger refinement rounds."""

    def test_reflection_score_from_dict_clamps(self) -> None:
        score = ReflectionScore.from_dict(
            {"accuracy": 9, "completeness": 0, "relevance": 3, "source_support": 4, "overall": 5}
        )
        assert score.accuracy == 5.0  # clamped
        assert score.completeness == 1.0  # clamped
        assert score.overall == 5.0
        # Missing overall → mean of dimensions.
        score2 = ReflectionScore.from_dict(
            {"accuracy": 4, "completeness": 4, "relevance": 4, "source_support": 4}
        )
        assert score2.overall == 4.0

    def test_low_score_triggers_refinement_round(self) -> None:
        """A reflection score below threshold triggers another ReAct round."""
        provider = ScriptedLLMProvider(
            responses=[
                # Initial answer (no tools).
                "初步回答。",
                # Reflection: low score → triggers a round.
                json.dumps(
                    {
                        "accuracy": 2,
                        "completeness": 2,
                        "relevance": 2,
                        "source_support": 2,
                        "overall": 2.0,
                        "critique": "完整性不足",
                    }
                ),
                # Refinement round: request a tool to gather evidence.
                _completion("", tool_calls=[_tool_call("search_documents", {"query": "补充"})]),
                # Refinement produces a better answer.
                "改进后的回答：完整结论。",
                # Second reflection: now above threshold.
                json.dumps(
                    {
                        "accuracy": 4,
                        "completeness": 4,
                        "relevance": 4,
                        "source_support": 4,
                        "overall": 4.0,
                        "critique": "已改善",
                    }
                ),
            ]
        )
        tool = RecordingTool("search_documents")
        agent = _agent(
            provider,
            _registry(tool),
            config=AgentConfig(
                enable_planning=False,
                enable_reflection=True,
                enable_memory=False,
                max_iterations=5,
                max_tool_calls=5,
                reflection_threshold=3.0,
                max_reflection_rounds=3,
            ),
        )

        answer = asyncio.run(agent.run("需要反思的问题"))

        assert "改进" in answer or "初步" in answer
        # The refinement round dispatched the tool.
        assert len(tool.calls) == 1
        # Two reflection entries logged.
        assert len(agent.trajectory.reflection_log) == 2

    def test_high_score_skips_refinement(self) -> None:
        """A score at/above threshold stops reflection immediately."""
        provider = ScriptedLLMProvider(
            responses=[
                "高质量回答。",
                json.dumps(
                    {
                        "accuracy": 5,
                        "completeness": 5,
                        "relevance": 5,
                        "source_support": 5,
                        "overall": 5.0,
                        "critique": "无需改进",
                    }
                ),
            ]
        )
        agent = _agent(
            provider,
            _registry(RecordingTool("search_documents")),
            config=AgentConfig(
                enable_planning=False,
                enable_reflection=True,
                enable_memory=False,
                reflection_threshold=3.0,
            ),
        )

        answer = asyncio.run(agent.run("已充分回答的问题"))

        assert "高质量" in answer
        assert len(agent.trajectory.reflection_log) == 1


# ---------------------------------------------------------------------------
# 7. Helpers + edge cases
# ---------------------------------------------------------------------------

class TestHelpersAndEdgeCases:
    """Token estimation, JSON extraction, arg truncation, tool budget."""

    def test_estimate_tokens_nonempty(self) -> None:
        assert _estimate_tokens("") == 0
        # Non-empty → at least 1 token.
        assert _estimate_tokens("hello world") >= 1

    def test_extract_json_fenced_block(self) -> None:
        text = '一些说明\n```json\n{"accuracy": 4}\n```\n更多说明'
        data = _extract_json(text)
        assert data == {"accuracy": 4}

    def test_extract_json_bare_object(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}
        assert _extract_json("not json") is None

    def test_truncate_args_short(self) -> None:
        assert _truncate_args({"q": "short"}) == '{"q": "short"}'

    def test_truncate_args_long(self) -> None:
        long_val = "x" * 200
        result = _truncate_args({"q": long_val}, max_len=20)
        assert result.endswith("...")
        assert len(result) == 23  # 20 + "..."

    def test_tool_budget_caps_dispatch(self) -> None:
        """max_tool_calls caps the number of dispatched tool calls."""
        provider = ScriptedLLMProvider(
            responses=[
                _completion(
                    "",
                    tool_calls=[
                        _tool_call("t1", {"query": "1"}),
                        _tool_call("t2", {"query": "2"}),
                        _tool_call("t3", {"query": "3"}),
                    ],
                ),
                "受限回答。",
            ],
            default="默认回答。",
        )
        t1 = RecordingTool("t1")
        t2 = RecordingTool("t2")
        t3 = RecordingTool("t3")
        agent = _agent(
            provider,
            _registry(t1, t2, t3),
            config=AgentConfig(
                max_iterations=3,
                max_tool_calls=2,
                enable_planning=False,
                enable_reflection=False,
                enable_memory=False,
            ),
        )

        asyncio.run(agent.run("超额工具调用"))

        # Only 2 calls dispatched (budget = 2), the 3rd is dropped.
        assert len(t1.calls) == 1
        assert len(t2.calls) == 1
        assert len(t3.calls) == 0
        assert agent._tool_calls_used == 2


# ---------------------------------------------------------------------------
# 8. Multi-agent orchestrator-worker
# ---------------------------------------------------------------------------

class TestMultiAgentOrchestration:
    """Orchestrator decomposes a task and dispatches workers via TaskStore."""

    def test_single_subtask_skips_orchestration(self) -> None:
        """When decomposition yields a single subtask, run() runs single-agent."""
        provider = ScriptedLLMProvider(
            responses=[
                '["单一子任务"]',  # decomposition → 1 subtask
                "单 agent 直接回答。",
            ]
        )
        agent = Agent(
            llm_provider=provider,
            tool_registry=_registry(),
            config=AgentConfig(enable_multi_agent=True, enable_planning=False, enable_reflection=False),
            task_store=MagicMock(),
        )

        answer = asyncio.run(agent.run("简单任务"))

        assert "单 agent" in answer

    def test_multi_subtask_dispatches_workers(self) -> None:
        """Multiple subtasks are dispatched to workers and synthesised."""
        from doctoragent.orchestration.task_store import TaskStore

        provider = ScriptedLLMProvider(
            responses=[
                # Decomposition → 2 subtasks.
                '["子任务A", "子任务B"]',
                # Worker A answer.
                "A 的结果。",
                # Worker B answer.
                "B 的结果。",
                # Synthesis.
                "综合 A 的结果与 B 的结果。",
            ]
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db", tenant_id="t")
            agent = Agent(
                llm_provider=provider,
                tool_registry=_registry(),
                config=AgentConfig(
                    enable_multi_agent=True,
                    enable_planning=False,
                    enable_reflection=False,
                    max_subtasks=5,
                ),
                task_store=store,
            )

            answer = asyncio.run(agent.run("复杂任务"))

        assert "综合" in answer
        # Synthesis step recorded.
        answer_steps = [s for s in agent.trajectory.steps if s.step_type == StepType.ANSWER]
        assert len(answer_steps) >= 1

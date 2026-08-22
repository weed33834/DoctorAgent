"""Tests: global wall-clock budget for the agent run loop (v0.3.15).

``AgentConfig.max_wall_clock_seconds`` caps one agent run regardless of the
iteration/tool budgets. When exceeded, the loop stops iterating and asks the
LLM for a consolidated final answer instead of running forever. ``None``
(the default) preserves the legacy unlimited behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any

from doctoragent.model.agent import Agent, AgentConfig, AgentState
from doctoragent.model.provider import ChatCompletionResponse
from doctoragent.model.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


class AlwaysToolCallProvider:
    """LLM that always requests a tool when tools are offered, otherwise
    returns a fixed consolidated answer."""

    model_name = "wallclock-mock"

    def __init__(self) -> None:
        self.call_count = 0
        self.calls_with_no_tools = 0

    def chat_completion_sync(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        **_: Any,
    ) -> Any:
        self.call_count += 1
        if tools:
            return ChatCompletionResponse(
                content="thinking",
                tool_calls=[{"name": "slow_tool", "arguments": {}}],
            )
        # The consolidation call runs without a tool spec and expects a
        # plain string answer.
        self.calls_with_no_tools += 1
        return "CONSOLIDATED ANSWER"


class SlowTool(Tool):
    def __init__(self, delay: float = 30.0) -> None:
        self._delay = delay
        self.calls = 0

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="slow_tool",
            description="slow tool",
            parameters=[],
            category="retrieval",
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        await asyncio.sleep(self._delay)
        return ToolResult(success=True, data={"ok": True}, tool_name="slow_tool")


def _agent(provider: Any, config: AgentConfig) -> Agent:
    reg = ToolRegistry()
    reg.register(SlowTool())
    return Agent(
        llm_provider=provider,
        tool_registry=reg,
        config=config,
        memory_system=None,
        task_store=None,
    )


class TestWallClockBudget:
    def test_default_is_unlimited(self) -> None:
        assert AgentConfig().max_wall_clock_seconds is None

    def test_expired_budget_skips_iteration_and_consolidates(self) -> None:
        """A deadline already in the past: no tool executes, LLM consolidates."""
        provider = AlwaysToolCallProvider()
        config = AgentConfig(
            enable_planning=False,
            enable_reflection=False,
            enable_memory=False,
            max_wall_clock_seconds=0.0,  # expires immediately
        )
        agent = _agent(provider, config)

        async def _run() -> str:
            return await agent.run("research task")

        answer = asyncio.run(_run())
        assert answer == "CONSOLIDATED ANSWER"
        # Only the consolidation call was made; no ReAct iteration ran.
        assert provider.calls_with_no_tools == 1
        assert agent.state == AgentState.FINISHED

    def test_healthy_budget_runs_normally(self) -> None:
        """A generous budget does not change the happy path."""

        class DirectAnswerProvider:
            model_name = "direct"
            call_count = 0

            def chat_completion_sync(
                self,
                messages: list[dict[str, Any]],
                *,
                tools: list[dict[str, Any]] | None = None,
                **_: Any,
            ) -> Any:
                self.call_count += 1
                if not tools:
                    return "the answer"
                return ChatCompletionResponse(content="the answer")

        provider = DirectAnswerProvider()
        config = AgentConfig(
            enable_planning=False,
            enable_reflection=False,
            enable_memory=False,
            max_wall_clock_seconds=60.0,
        )
        agent = _agent(provider, config)

        async def _run() -> str:
            return await agent.run("simple question")

        answer = asyncio.run(_run())
        assert answer == "the answer"
        assert provider.call_count == 1

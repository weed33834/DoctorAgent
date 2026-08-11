"""Framework-neutral agent runtime adapters (M3.19 / M3.20).

DoctorAgent's own ReAct loop (``doctoragent.model.agent``) is the default
runtime. These adapters let the same business interface be executed by other
agent frameworks so a deployment can pick the engine it prefers — the
"framework-neutral runtime abstraction" from the Universal Agent Builder spec.

Each adapter implements a tiny common interface::

    class Adapter:
        async def run(self, messages: list, **kwargs) -> str: ...
        async def close(self) -> None: ...

An adapter is selected by name via :func:`create_adapter`; unknown names fall
back to the built-in runtime. Import is lazy and guarded so DoctorAgent works
without any of the optional framework SDKs installed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ADAPTER_NAMES = ("langgraph", "openai_agents", "claude_sdk", "adk", "autogen")


class AgentRuntimeAdapter:
    """Base class for a framework-neutral agent runtime."""

    name = "base"

    async def run(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class BuiltinRuntimeAdapter(AgentRuntimeAdapter):
    """Default adapter: DoctorAgent's own ReAct agent (or any callable)."""

    name = "builtin"

    def __init__(self, run_fn: Any) -> None:
        self.run_fn = run_fn

    async def run(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        import asyncio

        text = messages[-1].get("content", "") if messages else ""
        if asyncio.iscoroutinefunction(self.run_fn):
            result = await self.run_fn(text)
        else:
            result = self.run_fn(text)
        return str(result)


class OpenAIAgentsAdapter(AgentRuntimeAdapter):
    """Adapter backed by the OpenAI Agents SDK (optional)."""

    name = "openai_agents"

    def __init__(self, model: str = "gpt-4o", instructions: str = "You are a helpful assistant.") -> None:
        self.model = model
        self.instructions = instructions

    async def run(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        from openai import AsyncOpenAI
        from openai.agents import Agent, Runner

        client = AsyncOpenAI()
        agent = Agent(name="doctoragent", instructions=self.instructions, model=self.model)
        prompt = messages[-1].get("content", "") if messages else ""
        result = await Runner.run(client, agent, prompt)
        return result.final_output or ""


class ClaudeSDKAdapter(AgentRuntimeAdapter):
    """Adapter backed by the Anthropic Claude SDK (optional)."""

    name = "claude_sdk"

    def __init__(self, model: str = "claude-3-5-sonnet-latest") -> None:
        self.model = model

    async def run(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
        system = kwargs.get("system", "You are a helpful assistant.")
        prompt = messages[-1].get("content", "") if messages else ""
        resp = await client.messages.create(
            model=self.model, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


class ADKAdapter(AgentRuntimeAdapter):
    """Adapter backed by Google ADK (Agent Development Kit, optional)."""

    name = "adk"

    def __init__(self, model: str = "gemini-2.0-flash") -> None:
        self.model = model

    async def run(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        from google.adk import Agent, Runner

        agent = Agent(name="doctoragent", model=self.model)
        prompt = messages[-1].get("content", "") if messages else ""
        result = await Runner.run(agent, prompt)
        return getattr(result, "final_response", "") or ""


class AutoGenAdapter(AgentRuntimeAdapter):
    """Adapter backed by Microsoft AutoGen (optional)."""

    name = "autogen"

    def __init__(self, model: str = "gpt-4o") -> None:
        self.model = model

    async def run(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        # Real AutoGen invocation. AutoGen's API is stateful, so we build a
        # one-shot assistant around the provided model and run it. If the SDK
        # is missing, raise a clear error rather than returning fake text.
        try:
            from autogen_agentchat.agents import AssistantAgent
            from autogen_core import CancellationToken
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "autogen adapter requires 'autogen-agentchat' (pip install doctoragent[adapters])"
            ) from exc
        prompt = messages[-1].get("content", "") if messages else ""
        from autogen_ext.models.openai import OpenAIChatCompletionClient

        client = OpenAIChatCompletionClient(model=self.model, api_key="__dummy__")
        agent = AssistantAgent(name="doctoragent", model_client=client, tools=[])
        result = await agent.run(task=prompt, cancellation_token=CancellationToken())
        return getattr(getattr(result, "last_message", None), "content", "") or ""


def create_adapter(name: str, run_fn: Any = None, **kwargs: Any) -> AgentRuntimeAdapter:
    """Create an agent runtime adapter by name (M3.20).

    ``name`` ∈ ``{"builtin", "openai_agents", "claude_sdk", "adk", "autogen"}``.
    Any other value (including ``"langgraph"``) resolves to the built-in runtime
    so DoctorAgent keeps working with no optional framework installed.
    """
    name = (name or "builtin").lower()
    if name == "openai_agents":
        return OpenAIAgentsAdapter(**kwargs)
    if name == "claude_sdk":
        return ClaudeSDKAdapter(**kwargs)
    if name == "adk":
        return ADKAdapter(**kwargs)
    if name == "autogen":
        return AutoGenAdapter(**kwargs)
    return BuiltinRuntimeAdapter(run_fn)

"""Tests for agent, tools, skills, and evaluation modules."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from pydantic import BaseModel

from doctoragent.model.tools import (
    Tool,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolParameter,
    SearchDocumentsTool,
    ListFilesTool,
    MemoryTool,
    create_default_registry,
)
from doctoragent.model.agent import (
    Agent,
    AgentConfig,
    AgentState,
    AgentTrajectory,
    AgentStep,
    StepType,
    create_agent,
)
from doctoragent.model.skills import (
    Skill,
    SkillRegistry,
    SkillResult,
    SkillDefinition,
    SkillCategory,
    DocumentSearchSkill,
    DocumentAnalysisSkill,
    create_default_skill_registry,
)
from doctoragent.model.evaluation import (
    RAGEvaluator,
    AgentEvaluator,
    EvaluationSuite,
    LLMTestCase,
    AgentTestCase,
    MetricResult,
    ContextPrecisionMetric,
    ContextRecallMetric,
    FaithfulnessMetric,
    AnswerRelevancyMetric,
    ToolCorrectnessMetric,
)


# ---------------------------------------------------------------------------
# Tool Tests
# ---------------------------------------------------------------------------

class TestToolDefinition:
    """Test ToolDefinition model."""

    def test_tool_definition_creation(self):
        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters=[
                ToolParameter(name="query", type="string", description="Search query"),
                ToolParameter(name="limit", type="integer", description="Max results", required=False),
            ],
            category="test",
        )
        assert tool_def.name == "test_tool"
        assert len(tool_def.parameters) == 2

    def test_to_json_schema(self):
        tool_def = ToolDefinition(
            name="search",
            description="Search documents",
            parameters=[
                ToolParameter(name="query", type="string", description="Query"),
            ],
        )
        schema = tool_def.to_json_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search"
        assert "query" in schema["function"]["parameters"]["properties"]

    def test_to_openai_tools(self):
        tool_def = ToolDefinition(
            name="test",
            description="Test tool",
        )
        tools = tool_def.to_openai_tools()
        assert isinstance(tools, list)
        assert len(tools) == 1


class TestToolRegistry:
    """Test ToolRegistry."""

    def test_register_and_get(self):
        registry = ToolRegistry()
        tool_def = ToolDefinition(name="test", description="Test tool")
        
        mock_tool = MagicMock(spec=Tool)
        mock_tool.definition = tool_def
        
        registry.register(mock_tool)
        assert registry.get("test") == mock_tool

    def test_list_tools(self):
        registry = ToolRegistry()
        tool_def = ToolDefinition(name="test", description="Test tool")
        
        mock_tool = MagicMock(spec=Tool)
        mock_tool.definition = tool_def
        
        registry.register(mock_tool)
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "test"

    def test_list_by_category(self):
        registry = ToolRegistry()
        
        tool1 = MagicMock(spec=Tool)
        tool1.definition = ToolDefinition(name="t1", description="Tool 1", category="cat1")
        
        tool2 = MagicMock(spec=Tool)
        tool2.definition = ToolDefinition(name="t2", description="Tool 2", category="cat2")
        
        registry.register(tool1)
        registry.register(tool2)
        
        cat1_tools = registry.list_by_category("cat1")
        assert len(cat1_tools) == 1

    def test_to_openai_tools(self):
        registry = ToolRegistry()
        tool_def = ToolDefinition(name="test", description="Test")
        
        mock_tool = MagicMock(spec=Tool)
        mock_tool.definition = tool_def
        
        registry.register(mock_tool)
        tools = registry.to_openai_tools()
        assert isinstance(tools, list)


class TestToolResult:
    """Test ToolResult model."""

    def test_success_result(self):
        result = ToolResult(success=True, data={"key": "value"})
        assert result.success is True
        assert result.data == {"key": "value"}

    def test_error_result(self):
        result = ToolResult(success=False, error="Something went wrong")
        assert result.success is False
        assert result.error == "Something went wrong"


class TestSearchDocumentsTool:
    """Test SearchDocumentsTool."""

    def test_definition(self):
        tool = SearchDocumentsTool()
        defn = tool.definition
        assert defn.name == "search_documents"
        assert defn.category == "retrieval"

    def test_execute_without_rag(self):
        tool = SearchDocumentsTool(rag_pipeline=None)
        result = tool(query="test")  # Use __call__ which wraps execute
        assert result.success is False
        assert "not initialized" in result.error


class TestMemoryTool:
    """Test MemoryTool."""

    def test_definition(self):
        tool = MemoryTool()
        defn = tool.definition
        assert defn.name == "memory"
        assert defn.category == "memory"

    def test_execute_without_memory(self):
        tool = MemoryTool(memory_system=None)
        result = tool(action="store", content="test")  # Use __call__
        assert result.success is False


# ---------------------------------------------------------------------------
# Agent Tests
# ---------------------------------------------------------------------------

class TestAgentConfig:
    """Test AgentConfig model."""

    def test_default_config(self):
        config = AgentConfig()
        assert config.max_iterations == 10
        assert config.max_tool_calls == 5
        assert config.temperature == 0.7

    def test_custom_config(self):
        config = AgentConfig(max_iterations=5, temperature=0.3)
        assert config.max_iterations == 5
        assert config.temperature == 0.3


class TestAgentTrajectory:
    """Test AgentTrajectory model."""

    def test_add_step(self):
        trajectory = AgentTrajectory()
        step = AgentStep(step_type=StepType.THOUGHT, content="Thinking...")
        trajectory.add_step(step)
        assert len(trajectory.steps) == 1

    def test_get_thoughts(self):
        trajectory = AgentTrajectory()
        trajectory.add_step(AgentStep(step_type=StepType.THOUGHT, content="Thought 1"))
        trajectory.add_step(AgentStep(step_type=StepType.ACTION, content="Action 1"))
        trajectory.add_step(AgentStep(step_type=StepType.THOUGHT, content="Thought 2"))
        
        thoughts = trajectory.get_thoughts()
        assert len(thoughts) == 2

    def test_get_tool_calls(self):
        trajectory = AgentTrajectory()
        trajectory.add_step(AgentStep(
            step_type=StepType.ACTION,
            content="Calling tool",
            tool_name="test_tool",
            tool_args={"query": "test"},
        ))
        
        calls = trajectory.get_tool_calls()
        assert len(calls) == 1
        assert calls[0]["tool"] == "test_tool"


class TestAgent:
    """Test Agent class."""

    def test_agent_creation(self):
        llm_provider = MagicMock()
        llm_provider.chat_completion_sync = MagicMock(return_value="test response")
        
        registry = ToolRegistry()
        agent = Agent(llm_provider=llm_provider, tool_registry=registry)
        
        assert agent.state == AgentState.IDLE
        assert agent.llm_provider == llm_provider

    def test_agent_reset(self):
        llm_provider = MagicMock()
        registry = ToolRegistry()
        agent = Agent(llm_provider=llm_provider, tool_registry=registry)
        
        agent.state = AgentState.THINKING
        agent.reset()
        assert agent.state == AgentState.IDLE

    def test_build_system_prompt(self):
        llm_provider = MagicMock()
        registry = ToolRegistry()
        agent = Agent(llm_provider=llm_provider, tool_registry=registry)
        
        prompt = agent._build_system_prompt()
        assert "工具" in prompt or "tool" in prompt.lower()


# ---------------------------------------------------------------------------
# Skill Tests
# ---------------------------------------------------------------------------

class TestSkillDefinition:
    """Test SkillDefinition model."""

    def test_skill_definition(self):
        defn = SkillDefinition(
            name="test_skill",
            description="Test skill",
            category=SkillCategory.RETRIEVAL,
            triggers=["test", "search"],
        )
        assert defn.name == "test_skill"
        assert defn.category == SkillCategory.RETRIEVAL


class TestSkillRegistry:
    """Test SkillRegistry."""

    def test_register_and_get(self):
        registry = SkillRegistry()
        skill = MagicMock(spec=Skill)
        skill.definition = SkillDefinition(
            name="test",
            description="Test",
            category=SkillCategory.RETRIEVAL,
        )
        
        registry.register(skill)
        assert registry.get("test") == skill

    def test_find_matching_skill(self):
        registry = SkillRegistry()
        
        skill = MagicMock(spec=Skill)
        skill.definition = SkillDefinition(
            name="search",
            description="Search",
            category=SkillCategory.RETRIEVAL,
            triggers=["search", "find"],
        )
        skill.matches = MagicMock(return_value=True)
        
        registry.register(skill)
        found = registry.find_matching_skill("search for documents")
        assert found == skill


class TestDocumentSearchSkill:
    """Test DocumentSearchSkill."""

    def test_definition(self):
        skill = DocumentSearchSkill()
        defn = skill.definition
        assert defn.name == "document_search"
        assert defn.category == SkillCategory.RETRIEVAL

    def test_matches(self):
        skill = DocumentSearchSkill()
        assert skill.matches("search for documents")
        assert skill.matches("find my contract")
        assert not skill.matches("hello world")


# ---------------------------------------------------------------------------
# Evaluation Tests
# ---------------------------------------------------------------------------

class TestContextPrecisionMetric:
    """Test ContextPrecisionMetric."""

    def test_no_context(self):
        metric = ContextPrecisionMetric()
        test_case = LLMTestCase(input="test", actual_output="answer")
        result = metric.measure(test_case)
        assert result.score == 0.0

    def test_with_context(self):
        metric = ContextPrecisionMetric()
        test_case = LLMTestCase(
            input="contract terms",
            actual_output="The contract has terms",
            retrieval_context=["Contract terms and conditions", "Other content"],
        )
        result = metric.measure(test_case)
        assert result.score > 0


class TestFaithfulnessMetric:
    """Test FaithfulnessMetric."""

    def test_no_context(self):
        metric = FaithfulnessMetric()
        test_case = LLMTestCase(input="test", actual_output="answer")
        result = metric.measure(test_case)
        assert result.score == 0.5  # Neutral

    def test_with_context(self):
        metric = FaithfulnessMetric()
        test_case = LLMTestCase(
            input="test",
            actual_output="The contract is valid",
            retrieval_context=["This contract is valid for one year"],
        )
        result = metric.measure(test_case)
        assert result.score > 0


class TestAnswerRelevancyMetric:
    """Test AnswerRelevancyMetric."""

    def test_empty_answer(self):
        metric = AnswerRelevancyMetric()
        test_case = LLMTestCase(input="test", actual_output="")
        result = metric.measure(test_case)
        assert result.score == 0.0

    def test_relevant_answer(self):
        metric = AnswerRelevancyMetric()
        test_case = LLMTestCase(
            input="contract expiration date",
            actual_output="The contract expires on December 31",
        )
        result = metric.measure(test_case)
        assert result.score > 0


class TestToolCorrectnessMetric:
    """Test ToolCorrectnessMetric."""

    def test_no_expected_tools(self):
        metric = ToolCorrectnessMetric()
        test_case = AgentTestCase(
            input="test",
            actual_output="answer",
            tools_called=["tool1"],
        )
        result = metric.measure(test_case)
        assert result.score == 1.0

    def test_correct_tools(self):
        metric = ToolCorrectnessMetric()
        test_case = AgentTestCase(
            input="test",
            actual_output="answer",
            tools_called=["tool1", "tool2"],
            expected_tools=["tool1", "tool2"],
        )
        result = metric.measure(test_case)
        assert result.score == 1.0

    def test_partial_tools(self):
        metric = ToolCorrectnessMetric()
        test_case = AgentTestCase(
            input="test",
            actual_output="answer",
            tools_called=["tool1"],
            expected_tools=["tool1", "tool2"],
        )
        result = metric.measure(test_case)
        assert result.score == 0.5


class TestRAGEvaluator:
    """Test RAGEvaluator."""

    def test_evaluate(self):
        evaluator = RAGEvaluator()
        test_case = LLMTestCase(
            input="test question",
            actual_output="test answer",
            retrieval_context=["context 1", "context 2"],
        )
        results = evaluator.evaluate(test_case)
        assert "context_precision" in results
        assert "faithfulness" in results

    def test_rag_score(self):
        evaluator = RAGEvaluator()
        test_case = LLMTestCase(
            input="test",
            actual_output="answer",
            retrieval_context=["context"],
        )
        score = evaluator.evaluate_rag_score(test_case)
        assert 0 <= score <= 1


class TestMetricResult:
    """Test MetricResult model."""

    def test_passed(self):
        result = MetricResult(metric_name="test", score=0.8, threshold=0.5)
        assert result.passed is True

    def test_failed(self):
        result = MetricResult(metric_name="test", score=0.3, threshold=0.5)
        assert result.passed is False

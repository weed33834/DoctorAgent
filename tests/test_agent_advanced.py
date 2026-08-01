# mypy: ignore-errors
"""Tests for the advanced agent modules.

Covers:
- Self-Evolution engine (trajectory analysis, experience storage/recall,
  lesson extraction with mock LLM, evolution loop)
- Dynamic Tools factory (tool composition, pipeline creation, registration)
- Tree-of-Thought (tree data structure, search with mock LLM, BFS/DFS)
- Human-in-the-Loop (breakpoints, approval workflow, audit trail)
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import pytest

from doctoragent.model.self_evolution import (
    ExecutionOutcome,
    Experience,
    SelfEvolutionEngine,
    TrajectoryPattern,
)
from doctoragent.model.dynamic_tools import (
    CompositeTool,
    DynamicToolFactory,
    ToolChainStep,
    ToolTemplate,
)
from doctoragent.model.tree_of_thought import (
    SearchStrategy,
    ThoughtNode,
    ThoughtTree,
    TreeOfThoughts,
)
from doctoragent.model.human_in_loop import (
    BreakpointType,
    HITLManager,
    HITLRequest,
    HITLResponse,
    RequestStatus,
)
from doctoragent.model.tools import (
    Tool,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolResult,
)
from doctoragent.orchestration.task_store import TaskStore


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------

class MockLLMProvider:
    """Minimal LLM provider returning canned responses for deterministic tests."""

    def __init__(self, responses: list[str] | None = None, default: str = "") -> None:
        self._responses = responses or []
        self._idx = 0
        self.default = default
        self.model_name = "mock-advanced-model"
        self.call_count = 0

    def chat_completion_sync(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.call_count += 1
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self.default


# ---------------------------------------------------------------------------
# Mock trajectory objects for SelfEvolutionEngine
# ---------------------------------------------------------------------------

@dataclass
class MockStep:
    step_type: str = "action"
    content: str = ""
    tool_name: str = ""
    tool_result: Any = None


@dataclass
class MockTrajectory:
    query: str = ""
    steps: list[MockStep] = dc_field(default_factory=list)
    total_tool_calls: int = 0


def _make_success_trajectory(query: str = "find contract expiry date") -> MockTrajectory:
    return MockTrajectory(
        query=query,
        steps=[
            MockStep(step_type="action", tool_name="search_documents", tool_result={"success": True}),
            MockStep(step_type="action", tool_name="read_document", tool_result={"success": True}),
            MockStep(step_type="answer", content="The contract expires on 2025-12-31."),
        ],
        total_tool_calls=2,
    )


def _make_failure_trajectory(query: str = "delete all files") -> MockTrajectory:
    return MockTrajectory(
        query=query,
        steps=[
            MockStep(step_type="action", tool_name="delete_file", tool_result={"success": False, "error": "permission denied"}),
            MockStep(step_type="answer", content="无法完成操作，权限不足。"),
        ],
        total_tool_calls=1,
    )


# ---------------------------------------------------------------------------
# Mock tools for DynamicToolFactory tests
# ---------------------------------------------------------------------------

class SearchTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search",
            description="Search for documents",
            parameters=[
                ToolParameter(name="query", type="string", description="Search query", required=True),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, data={"results": [f"doc for {kwargs.get('query', '')}"]}, tool_name="search")


class SummarizeTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="summarize",
            description="Summarize content",
            parameters=[
                ToolParameter(name="text", type="string", description="Text to summarize", required=True),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs.get('text', '')
        if not isinstance(text, str):
            text = str(text)
        return ToolResult(success=True, data={"summary": f"Summary of: {text[:50]}"}, tool_name="summarize")


# ---------------------------------------------------------------------------
# Self-Evolution Engine
# ---------------------------------------------------------------------------

class TestSelfEvolutionEngine:
    """Tests for :class:`SelfEvolutionEngine`."""

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> Path:
        return tmp_path / "evolution_test.db"

    @pytest.fixture
    def task_store(self, db_path: Path) -> TaskStore:
        return TaskStore(db_path)

    @pytest.fixture
    def llm_provider(self) -> MockLLMProvider:
        lessons_response = json.dumps({
            "lessons": ["Use keyword 'expiry' for contract dates", "Search before reading"],
            "patterns": ["contract_date_query"],
            "optimized_prompt": "You are a contract analysis expert. Always search for 'expiry' and 'effective date' keywords.",
            "recommended_tools": ["search_documents", "read_document"],
        })
        return MockLLMProvider(responses=[lessons_response, "Optimized prompt text here"])

    @pytest.fixture
    def engine(
        self, task_store: TaskStore, llm_provider: MockLLMProvider
    ) -> SelfEvolutionEngine:
        return SelfEvolutionEngine(task_store, llm_provider)

    def test_analyze_trajectory_success(self, engine: SelfEvolutionEngine) -> None:
        traj = _make_success_trajectory()
        pattern = engine.analyze_trajectory(traj)
        assert pattern.success_count == 1
        assert pattern.failure_count == 0
        assert "search_documents" in pattern.common_tools
        assert pattern.avg_iterations == 2

    def test_analyze_trajectory_failure(self, engine: SelfEvolutionEngine) -> None:
        # A trajectory with a failed action and no answer is a pure failure.
        traj = MockTrajectory(
            query="delete all files",
            steps=[
                MockStep(
                    step_type="action",
                    tool_name="delete_file",
                    tool_result={"success": False, "error": "permission denied"},
                ),
            ],
            total_tool_calls=1,
        )
        pattern = engine.analyze_trajectory(traj)
        assert pattern.failure_count == 1
        assert "permission denied" in pattern.common_errors
        assert "delete_file" in pattern.common_tools

    def test_analyze_trajectory_empty(self, engine: SelfEvolutionEngine) -> None:
        traj = MockTrajectory(query="empty", steps=[])
        pattern = engine.analyze_trajectory(traj)
        assert pattern.success_count == 0
        assert pattern.failure_count == 1
        assert pattern.avg_iterations == 0

    def test_store_and_recall_experience(self, engine: SelfEvolutionEngine) -> None:
        exp = Experience(
            query="find contract expiry date",
            query_pattern="contract_date_query",
            outcome=ExecutionOutcome.SUCCESS,
            lessons=["Always search for 'expiry' keyword"],
            optimized_prompt="Be thorough in searching dates.",
            recommended_tools=["search_documents"],
        )
        assert engine.store_experience(exp) is True
        recalled = engine.recall_experiences("contract expiry date")
        assert len(recalled) >= 1
        assert recalled[0].query_pattern == "contract_date_query"

    def test_recall_experiences_no_match(self, engine: SelfEvolutionEngine) -> None:
        exp = Experience(
            query="completely unrelated topic",
            query_pattern="unrelated",
            outcome=ExecutionOutcome.SUCCESS,
        )
        engine.store_experience(exp)
        recalled = engine.recall_experiences("contract expiry date")
        # Should still return results (fallback to all when none match positively).
        assert isinstance(recalled, list)

    def test_recall_experiences_empty_db(self, engine: SelfEvolutionEngine) -> None:
        recalled = engine.recall_experiences("anything")
        assert recalled == []

    def test_recall_experiences_top_k(self, engine: SelfEvolutionEngine) -> None:
        for i in range(5):
            engine.store_experience(
                Experience(
                    query=f"contract date query {i}",
                    query_pattern="contract_date",
                    outcome=ExecutionOutcome.SUCCESS,
                )
            )
        recalled = engine.recall_experiences("contract date", top_k=3)
        assert len(recalled) <= 3

    def test_extract_lessons_with_llm(self, engine: SelfEvolutionEngine) -> None:
        trajectories = [_make_success_trajectory(), _make_success_trajectory()]
        result = engine.extract_lessons(trajectories)
        assert len(result["lessons"]) >= 1
        assert result["optimized_prompt"] != ""
        assert "search_documents" in result["recommended_tools"]

    def test_extract_lessons_empty(self, engine: SelfEvolutionEngine) -> None:
        result = engine.extract_lessons([])
        assert result["lessons"] == []
        assert result["recommended_tools"] == []

    def test_optimize_prompt(self, engine: SelfEvolutionEngine) -> None:
        trajectories = [_make_success_trajectory()]
        prompt = engine.optimize_prompt("contract_date_query", trajectories)
        assert prompt != ""

    def test_optimize_prompt_no_provider(self, db_path: Path) -> None:
        store = TaskStore(db_path)
        engine = SelfEvolutionEngine(store, None)
        prompt = engine.optimize_prompt("pattern", [])
        assert prompt == ""

    def test_evolve_loop(self, engine: SelfEvolutionEngine) -> None:
        trajectories = [_make_success_trajectory(), _make_success_trajectory()]
        result = engine.evolve(trajectories)
        assert result["analyzed"] == 2
        assert result["experiences_stored"] == 1
        assert len(result["lessons"]) >= 1

    def test_evolve_empty(self, engine: SelfEvolutionEngine) -> None:
        result = engine.evolve([])
        assert result["analyzed"] == 0
        assert result["experiences_stored"] == 0

    def test_trajectory_pattern_merge(self) -> None:
        p1 = TrajectoryPattern(
            success_count=2,
            failure_count=1,
            avg_iterations=3.0,
            common_tools=["a", "b"],
        )
        p2 = TrajectoryPattern(
            success_count=1,
            failure_count=2,
            avg_iterations=5.0,
            common_tools=["b", "c"],
        )
        merged = p1.merge(p2)
        assert merged.success_count == 3
        assert merged.failure_count == 3
        assert "a" in merged.common_tools
        assert "c" in merged.common_tools

    def test_trajectory_pattern_success_rate(self) -> None:
        p = TrajectoryPattern(success_count=3, failure_count=1)
        assert p.success_rate == 0.75
        assert p.total_count == 4

    def test_experience_to_dict(self) -> None:
        exp = Experience(
            query="test query",
            query_pattern="test_pattern",
            outcome=ExecutionOutcome.SUCCESS,
            lessons=["lesson 1"],
        )
        data = exp.to_dict()
        assert data["query"] == "test query"
        assert data["outcome"] == str(ExecutionOutcome.SUCCESS)
        assert data["lessons"] == ["lesson 1"]


# ---------------------------------------------------------------------------
# Dynamic Tool Factory
# ---------------------------------------------------------------------------

class TestDynamicToolFactory:
    """Tests for :class:`DynamicToolFactory`."""

    @pytest.fixture
    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(SearchTool())
        reg.register(SummarizeTool())
        return reg

    @pytest.fixture
    def llm_provider(self) -> MockLLMProvider:
        compose_response = json.dumps({
            "name": "search_and_summarize",
            "description": "Search for documents and summarize results",
            "tool_chain": [
                {"tool": "search", "input_mapping": {"query": "input:query"}, "output_key": "search_results"},
                {"tool": "summarize", "input_mapping": {"text": "step:0"}},
            ],
        })
        return MockLLMProvider(responses=[compose_response])

    @pytest.fixture
    def factory(
        self, llm_provider: MockLLMProvider, registry: ToolRegistry
    ) -> DynamicToolFactory:
        return DynamicToolFactory(llm_provider, registry)

    @pytest.mark.asyncio
    async def test_compose_tools(self, factory: DynamicToolFactory) -> None:
        tool = factory.compose_tools(
            ["search", "summarize"],
            "Search for documents and summarize the results",
        )
        assert tool is not None
        assert tool.definition.name == "search_and_summarize"
        # Execute the composed tool.
        result = await tool.execute(query="contract expiry")
        assert result.success is True
        assert result.tool_name == "search_and_summarize"

    @pytest.mark.asyncio
    async def test_compose_tools_filters_missing(
        self, factory: DynamicToolFactory
    ) -> None:
        tool = factory.compose_tools(
            ["search", "nonexistent_tool"],
            "Compose with a missing tool",
        )
        # Should still compose with the tools that exist.
        assert tool is not None

    def test_compose_tools_no_valid_tools(
        self, factory: DynamicToolFactory
    ) -> None:
        tool = factory.compose_tools(
            ["nonexistent1", "nonexistent2"],
            "Compose with only missing tools",
        )
        assert tool is None

    def test_compose_tools_no_provider(self, registry: ToolRegistry) -> None:
        factory = DynamicToolFactory(None, registry)
        tool = factory.compose_tools(["search"], "description")
        assert tool is None

    @pytest.mark.asyncio
    async def test_create_pipeline_tool(self, factory: DynamicToolFactory) -> None:
        steps = [
            ToolChainStep(
                tool_name="search",
                input_mapping={"input:query": "query"},
                output_key="search_results",
            ),
            ToolChainStep(
                tool_name="summarize",
                input_mapping={"step:0": "text"},
            ),
        ]
        tool = factory.create_pipeline_tool(steps, "pipeline_tool", "A pipeline tool")
        assert tool.definition.name == "pipeline_tool"
        result = await tool.execute(query="test query")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_composite_tool_empty_chain(self, registry: ToolRegistry) -> None:
        tool = CompositeTool("empty", "empty tool", [], registry)
        result = await tool.execute()
        assert result.success is False
        assert "empty" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_composite_tool_step_failure(
        self, registry: ToolRegistry
    ) -> None:
        class FailingTool(Tool):
            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(
                    name="fail_tool",
                    description="Always fails",
                    parameters=[
                        ToolParameter(name="input", type="string", description="input", required=True),
                    ],
                )

            async def execute(self, **kwargs: Any) -> ToolResult:
                return ToolResult(success=False, error="intentional failure", tool_name="fail_tool")

        registry.register(FailingTool())
        tool = CompositeTool(
            "composite_fail",
            "A tool that will fail",
            [("fail_tool", {"input": "input:query"})],
            registry,
        )
        result = await tool.execute(query="test")
        assert result.success is False

    def test_register_dynamic_tool(
        self, factory: DynamicToolFactory, registry: ToolRegistry
    ) -> None:
        tool = CompositeTool(
            "dyn_tool",
            "A dynamic tool",
            [("search", {"query": "input:q"})],
            registry,
        )
        assert factory.register_dynamic_tool(tool) is True
        assert registry.get("dyn_tool") is not None
        assert len(factory.list_dynamic_tools()) == 1

    def test_unregister_dynamic_tool(
        self, factory: DynamicToolFactory, registry: ToolRegistry
    ) -> None:
        tool = CompositeTool(
            "dyn_removable",
            "A removable dynamic tool",
            [("search", {"query": "input:q"})],
            registry,
        )
        factory.register_dynamic_tool(tool)
        assert factory.unregister_dynamic_tool("dyn_removable") is True
        assert registry.get("dyn_removable") is None

    def test_unregister_unknown_dynamic_tool(
        self, factory: DynamicToolFactory
    ) -> None:
        assert factory.unregister_dynamic_tool("never_registered") is False

    def test_validate_tool_definition_valid(self) -> None:
        definition = ToolDefinition(
            name="valid_tool",
            description="A valid tool",
            parameters=[
                ToolParameter(name="param1", type="string", description="A parameter"),
            ],
        )
        assert DynamicToolFactory.validate_tool_definition(definition) is True

    def test_validate_tool_definition_missing_name(self) -> None:
        definition = ToolDefinition(
            name="",
            description="No name",
            parameters=[],
        )
        assert DynamicToolFactory.validate_tool_definition(definition) is False

    def test_validate_tool_definition_missing_desc(self) -> None:
        definition = ToolDefinition(
            name="tool",
            description="",
            parameters=[],
        )
        assert DynamicToolFactory.validate_tool_definition(definition) is False

    def test_tool_template_serialization(self) -> None:
        template = ToolTemplate(
            name="custom_tool",
            description="A custom tool template",
            parameter_schema={"param": {"type": "string"}},
            required_tools=["search"],
        )
        data = template.to_dict()
        restored = ToolTemplate.from_dict(data)
        assert restored.name == "custom_tool"
        assert restored.required_tools == ["search"]


# ---------------------------------------------------------------------------
# Tree of Thought
# ---------------------------------------------------------------------------

class TestThoughtTree:
    """Tests for the :class:`ThoughtTree` data structure."""

    def test_add_child(self) -> None:
        tree = ThoughtTree("root thought")
        child = tree.add_child(tree.root_id, "child thought", 0.8)
        assert child is not None
        assert child.thought == "child thought"
        assert child.evaluation_score == 0.8
        assert child.parent_id == tree.root_id
        assert child.depth == 1
        assert child.id in tree.root.children_ids

    def test_add_child_unknown_parent(self) -> None:
        tree = ThoughtTree("root")
        child = tree.add_child("nonexistent", "thought", 0.5)
        assert child is None

    def test_get_best_path(self) -> None:
        tree = ThoughtTree("root")
        good = tree.add_child(tree.root_id, "good path", 0.9)
        bad = tree.add_child(tree.root_id, "bad path", 0.2)
        tree.add_child(good.id, "good child", 0.85)
        tree.add_child(bad.id, "bad child", 0.1)
        best = tree.get_best_path()
        assert len(best) == 3  # root -> good -> good child
        assert best[1].thought == "good path"

    def test_get_best_path_single_node(self) -> None:
        tree = ThoughtTree("only root")
        best = tree.get_best_path()
        assert len(best) == 1
        assert best[0].thought == "only root"

    def test_prune(self) -> None:
        tree = ThoughtTree("root")
        good = tree.add_child(tree.root_id, "good", 0.9)
        bad = tree.add_child(tree.root_id, "bad", 0.1)
        tree.add_child(bad.id, "bad child", 0.05)
        removed = tree.prune(0.5)
        assert removed == 2  # bad + bad child
        assert tree.get_node(bad.id) is None
        assert tree.get_node(good.id) is not None

    def test_prune_keeps_root(self) -> None:
        tree = ThoughtTree("root", )
        tree.root.evaluation_score = 0.1
        removed = tree.prune(0.5)
        assert removed == 0
        assert tree.root_id in tree.nodes

    def test_get_all_paths(self) -> None:
        tree = ThoughtTree("root")
        tree.add_child(tree.root_id, "a", 0.8)
        tree.add_child(tree.root_id, "b", 0.6)
        paths = tree.get_all_paths()
        assert len(paths) == 2

    def test_to_dict_and_from_dict(self) -> None:
        tree = ThoughtTree("root thought")
        tree.add_child(tree.root_id, "child", 0.7)
        data = tree.to_dict()
        restored = ThoughtTree.from_dict(data)
        assert restored.root_id == tree.root_id
        assert len(restored.nodes) == 2
        assert restored.root.thought == "root thought"


class TestTreeOfThoughts:
    """Tests for the :class:`TreeOfThoughts` search driver."""

    @pytest.fixture
    def mock_llm(self) -> MockLLMProvider:
        """LLM that alternates between thought-generation and evaluation responses."""
        thoughts_response = json.dumps([
            {"thought": "Approach A: break down the problem"},
            {"thought": "Approach B: use analogy"},
            {"thought": "Approach C: work backwards"},
        ])
        eval_response = json.dumps({"score": 0.8, "reasoning": "Promising approach"})
        # The search calls generate_thoughts then evaluate_thought for each.
        # We provide enough responses for multiple rounds.
        responses = []
        for _ in range(10):
            responses.append(thoughts_response)
            responses.append(eval_response)
            responses.append(eval_response)
            responses.append(eval_response)
        return MockLLMProvider(responses=responses)

    @pytest.fixture
    def tot(self, mock_llm: MockLLMProvider) -> TreeOfThoughts:
        return TreeOfThoughts(
            llm_provider=mock_llm,
            max_depth=2,
            branching_factor=3,
            evaluation_threshold=0.3,
        )

    @pytest.mark.asyncio
    async def test_search_bfs(self, tot: TreeOfThoughts) -> None:
        path = await tot.search("How to solve this problem?", strategy=SearchStrategy.BFS)
        assert len(path) >= 1
        assert path[0].thought == "How to solve this problem?"

    @pytest.mark.asyncio
    async def test_search_dfs(self, tot: TreeOfThoughts) -> None:
        path = await tot.search("How to solve this problem?", strategy=SearchStrategy.DFS)
        assert len(path) >= 1

    @pytest.mark.asyncio
    async def test_search_returns_best_path(self, tot: TreeOfThoughts) -> None:
        await tot.search("test query", strategy=SearchStrategy.BFS)
        best = tot.select_best_path()
        assert len(best) >= 1

    @pytest.mark.asyncio
    async def test_search_builds_tree(self, tot: TreeOfThoughts) -> None:
        await tot.search("test query", strategy=SearchStrategy.BFS)
        assert tot.tree is not None
        assert len(tot.tree.nodes) > 1  # root + children

    @pytest.mark.asyncio
    async def test_generate_thoughts(self, tot: TreeOfThoughts) -> None:
        thoughts = await tot.generate_thoughts({"query": "test"}, "parent thought")
        assert len(thoughts) > 0
        assert any("Approach" in t for t in thoughts)

    @pytest.mark.asyncio
    async def test_evaluate_thought(self, tot: TreeOfThoughts) -> None:
        score = await tot.evaluate_thought("a good thought", {"query": "test"})
        assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_search_with_string_strategy(self, tot: TreeOfThoughts) -> None:
        path = await tot.search("test query", strategy="bfs")
        assert len(path) >= 1

    @pytest.mark.asyncio
    async def test_search_no_provider(self) -> None:
        tot = TreeOfThoughts(llm_provider=None, max_depth=1, branching_factor=1)
        path = await tot.search("test query")
        # Should return just the root without children.
        assert len(path) >= 1

    @pytest.mark.asyncio
    async def test_search_with_context(self, tot: TreeOfThoughts) -> None:
        path = await tot.search(
            "test query", context="extra context info", strategy=SearchStrategy.BFS
        )
        assert len(path) >= 1


# ---------------------------------------------------------------------------
# Human-in-the-Loop
# ---------------------------------------------------------------------------

class TestHITLManager:
    """Tests for :class:`HITLManager`."""

    @pytest.fixture
    def manager(self) -> HITLManager:
        return HITLManager(auto_approve=False, default_timeout=2.0)

    @pytest.fixture
    def auto_manager(self) -> HITLManager:
        return HITLManager(auto_approve=True, default_timeout=2.0)

    def test_register_breakpoint(self, manager: HITLManager) -> None:
        manager.register_breakpoint(BreakpointType.BEFORE_TOOL_EXECUTION)
        assert manager.should_pause(BreakpointType.BEFORE_TOOL_EXECUTION, {})

    def test_register_breakpoint_with_condition(self, manager: HITLManager) -> None:
        manager.register_breakpoint(
            BreakpointType.BEFORE_DESTRUCTIVE_ACTION,
            condition=lambda ctx: ctx.get("destructive", False),
        )
        assert manager.should_pause(BreakpointType.BEFORE_DESTRUCTIVE_ACTION, {"destructive": True})
        assert not manager.should_pause(BreakpointType.BEFORE_DESTRUCTIVE_ACTION, {"destructive": False})

    def test_unregister_breakpoint(self, manager: HITLManager) -> None:
        manager.register_breakpoint(BreakpointType.BEFORE_FINAL_ANSWER)
        assert manager.unregister_breakpoint(BreakpointType.BEFORE_FINAL_ANSWER) is True
        assert not manager.should_pause(BreakpointType.BEFORE_FINAL_ANSWER, {})

    def test_unregister_unknown_breakpoint(self, manager: HITLManager) -> None:
        assert manager.unregister_breakpoint(BreakpointType.CUSTOM) is False

    def test_should_pause_no_breakpoint(self, manager: HITLManager) -> None:
        assert not manager.should_pause(BreakpointType.CUSTOM, {})

    def test_should_pause_auto_approve(self, auto_manager: HITLManager) -> None:
        auto_manager.register_breakpoint(BreakpointType.BEFORE_TOOL_EXECUTION)
        # Auto-approve means no pause.
        assert not auto_manager.should_pause(BreakpointType.BEFORE_TOOL_EXECUTION, {})

    def test_should_pause_condition_raises(self, manager: HITLManager) -> None:
        def bad_condition(ctx: Any) -> bool:
            raise ValueError("condition error")

        manager.register_breakpoint(BreakpointType.CUSTOM, condition=bad_condition)
        # Fails safe -> should pause.
        assert manager.should_pause(BreakpointType.CUSTOM, {})

    def test_set_auto_approve_per_action(self, manager: HITLManager) -> None:
        manager.register_breakpoint(BreakpointType.BEFORE_TOOL_EXECUTION)
        manager.set_auto_approve(BreakpointType.BEFORE_TOOL_EXECUTION, True)
        assert not manager.should_pause(BreakpointType.BEFORE_TOOL_EXECUTION, {})

    def test_request_approval_auto_approve(self, auto_manager: HITLManager) -> None:
        request = auto_manager.request_approval(
            BreakpointType.BEFORE_FINAL_ANSWER,
            context={"draft": "This is the answer."},
            description="Approve final answer",
        )
        assert request.status == RequestStatus.APPROVED
        assert request.response is not None
        assert request.response.approved is True

    def test_request_approval_with_response(self, manager: HITLManager) -> None:
        """Test the approval workflow with a manual response from another thread."""
        result_holder: dict[str, Any] = {}

        def approver_thread() -> None:
            time.sleep(0.1)
            history = manager.get_pending_requests()
            if history:
                req_id = history[0].request_id
                manager.process_response(
                    req_id,
                    HITLResponse(approved=True, feedback="Looks good"),
                )
                result_holder["approved"] = True

        thread = threading.Thread(target=approver_thread)
        thread.start()
        request = manager.request_approval(
            BreakpointType.BEFORE_TOOL_EXECUTION,
            context={"tool": "search"},
            description="Approve tool execution",
        )
        thread.join(timeout=5)
        assert request.status == RequestStatus.APPROVED
        assert request.response is not None
        assert request.response.approved is True

    def test_request_approval_rejection(self, manager: HITLManager) -> None:
        def rejector_thread() -> None:
            time.sleep(0.1)
            history = manager.get_pending_requests()
            if history:
                req_id = history[0].request_id
                manager.process_response(
                    req_id,
                    HITLResponse(approved=False, feedback="No, this is dangerous"),
                )

        thread = threading.Thread(target=rejector_thread)
        thread.start()
        request = manager.request_approval(
            BreakpointType.BEFORE_DESTRUCTIVE_ACTION,
            context={"action": "delete"},
            description="Approve destructive action",
        )
        thread.join(timeout=5)
        assert request.status == RequestStatus.REJECTED
        assert request.response.approved is False

    def test_request_approval_timeout(self) -> None:
        manager = HITLManager(auto_approve=False, default_timeout=0.2)
        request = manager.request_approval(
            BreakpointType.CUSTOM,
            context={},
            description="This will timeout",
        )
        assert request.status == RequestStatus.TIMEOUT
        assert request.response is None

    def test_process_response_unknown_request(self, manager: HITLManager) -> None:
        result = manager.process_response("unknown-id", HITLResponse(approved=True))
        assert result is False

    def test_process_response_already_resolved(self, manager: HITLManager) -> None:
        manager.set_auto_approve(BreakpointType.CUSTOM, True)
        request = manager.request_approval(
            BreakpointType.CUSTOM, context={}, description="auto"
        )
        # The request is already APPROVED — processing again should fail.
        result = manager.process_response(request.request_id, HITLResponse(approved=False))
        assert result is False

    def test_get_history(self, auto_manager: HITLManager) -> None:
        auto_manager.request_approval(
            BreakpointType.BEFORE_FINAL_ANSWER, context={}, description="req 1"
        )
        auto_manager.request_approval(
            BreakpointType.BEFORE_TOOL_EXECUTION, context={}, description="req 2"
        )
        history = auto_manager.get_history()
        assert len(history) == 2

    def test_get_pending_requests(self, manager: HITLManager) -> None:
        # Start a request in a thread that will time out quickly.
        def slow_request() -> None:
            manager.request_approval(
                BreakpointType.CUSTOM, context={}, description="pending"
            )

        thread = threading.Thread(target=slow_request)
        thread.start()
        time.sleep(0.1)
        pending = manager.get_pending_requests()
        assert len(pending) >= 1
        thread.join(timeout=5)

    def test_export_audit_trail(self, auto_manager: HITLManager) -> None:
        auto_manager.request_approval(
            BreakpointType.BEFORE_FINAL_ANSWER,
            context={"key": "value"},
            description="audit test",
        )
        trail = auto_manager.export_audit_trail()
        assert len(trail) == 1
        assert trail[0]["description"] == "audit test"
        assert trail[0]["status"] == str(RequestStatus.APPROVED)

    def test_export_audit_trail_json(self, auto_manager: HITLManager) -> None:
        auto_manager.request_approval(
            BreakpointType.CUSTOM, context={}, description="json test"
        )
        trail_json = auto_manager.export_audit_trail_json()
        parsed = json.loads(trail_json)
        assert isinstance(parsed, list)
        assert len(parsed) >= 1

    def test_clear_history(self, auto_manager: HITLManager) -> None:
        auto_manager.request_approval(
            BreakpointType.CUSTOM, context={}, description="to be cleared"
        )
        auto_manager.clear_history()
        assert auto_manager.get_history() == []

    def test_get_request(self, auto_manager: HITLManager) -> None:
        request = auto_manager.request_approval(
            BreakpointType.CUSTOM, context={}, description="lookup test"
        )
        found = auto_manager.get_request(request.request_id)
        assert found is not None
        assert found.request_id == request.request_id
        assert auto_manager.get_request("nonexistent") is None

    def test_hitl_response_to_dict(self) -> None:
        resp = HITLResponse(approved=True, feedback="good", modified_action={"key": "val"})
        data = resp.to_dict()
        assert data["approved"] is True
        assert data["feedback"] == "good"
        assert data["modified_action"] == {"key": "val"}

    def test_hitl_request_to_dict(self) -> None:
        req = HITLRequest(
            breakpoint_type=BreakpointType.BEFORE_FINAL_ANSWER,
            context={"tool": "search"},
            description="test request",
        )
        data = req.to_dict()
        assert data["description"] == "test request"
        assert data["status"] == str(RequestStatus.PENDING)
        assert data["approved"] is None

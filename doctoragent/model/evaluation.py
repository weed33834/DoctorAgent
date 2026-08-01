"""RAG evaluation metrics - measures quality of retrieval and generation.

Based on 2026 RAG evaluation best practices:
- Retrieval metrics: Context Precision, Context Recall, MRR
- Generation metrics: Faithfulness, Answer Relevancy
- End-to-end metrics: RAGAS score
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evaluation Test Cases
# ---------------------------------------------------------------------------


class LLMTestCase(BaseModel):
    """Test case for RAG evaluation."""

    input: str  # User query
    actual_output: str  # LLM generated answer
    expected_output: str | None = None  # Ground truth (optional)
    retrieval_context: list[str] = Field(default_factory=list)  # Retrieved documents
    context: list[str] = Field(default_factory=list)  # Ground truth context
    tools_called: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)


class AgentTestCase(BaseModel):
    """Test case for agent evaluation."""

    input: str
    actual_output: str
    expected_output: str | None = None
    tools_called: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)
    trajectory_steps: int = 0
    max_steps: int = 10


# ---------------------------------------------------------------------------
# Evaluation Metrics
# ---------------------------------------------------------------------------


@dataclass
class MetricResult:
    """Result from a metric evaluation."""

    metric_name: str
    score: float  # 0-1
    threshold: float = 0.5
    passed: bool = True
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.passed = self.score >= self.threshold


class ContextPrecisionMetric:
    """Measures if retrieved context is relevant to the query.

    Context Precision = (relevant chunks in top-k) / k
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.metric_name = "context_precision"

    def measure(self, test_case: LLMTestCase) -> MetricResult:
        """Calculate context precision."""
        if not test_case.retrieval_context:
            return MetricResult(
                metric_name=self.metric_name,
                score=0.0,
                threshold=self.threshold,
                reason="No retrieval context provided",
            )

        # Simple heuristic: count chunks that contain query keywords
        query_words = set(test_case.input.lower().split())
        relevant_count = 0

        for ctx in test_case.retrieval_context:
            ctx_lower = ctx.lower()
            # Check if chunk contains query keywords
            if any(word in ctx_lower for word in query_words if len(word) > 2):
                relevant_count += 1

        score = (
            relevant_count / len(test_case.retrieval_context) if test_case.retrieval_context else 0
        )

        return MetricResult(
            metric_name=self.metric_name,
            score=score,
            threshold=self.threshold,
            reason=f"{relevant_count}/{len(test_case.retrieval_context)} chunks are relevant",
            details={
                "relevant_chunks": relevant_count,
                "total_chunks": len(test_case.retrieval_context),
            },
        )


class ContextRecallMetric:
    """Measures if all relevant documents were retrieved.

    Context Recall = (relevant chunks retrieved) / (total relevant chunks)
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.metric_name = "context_recall"

    def measure(self, test_case: LLMTestCase) -> MetricResult:
        """Calculate context recall."""
        if not test_case.context:
            # No ground truth context - estimate based on answer coverage
            if test_case.expected_output and test_case.actual_output:
                # Simple word overlap
                expected_words = set(test_case.expected_output.lower().split())
                actual_words = set(test_case.actual_output.lower().split())
                if expected_words:
                    overlap = len(expected_words & actual_words)
                    score = overlap / len(expected_words)
                else:
                    score = 0.5
            else:
                score = 0.5
        else:
            # Compare retrieved context with ground truth
            retrieved_text = " ".join(test_case.retrieval_context).lower()
            relevant_count = sum(1 for ctx in test_case.context if ctx.lower() in retrieved_text)
            score = relevant_count / len(test_case.context) if test_case.context else 0

        return MetricResult(
            metric_name=self.metric_name,
            score=score,
            threshold=self.threshold,
            reason=f"Recall score: {score:.2f}",
        )


class FaithfulnessMetric:
    """Measures if the answer is grounded in the retrieved context.

    Faithfulness = (supported claims) / (total claims)
    """

    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold
        self.metric_name = "faithfulness"

    def measure(self, test_case: LLMTestCase) -> MetricResult:
        """Calculate faithfulness."""
        if not test_case.retrieval_context:
            return MetricResult(
                metric_name=self.metric_name,
                score=0.5,  # Neutral if no context
                threshold=self.threshold,
                reason="No retrieval context to verify faithfulness",
            )

        # Simple heuristic: check if answer words appear in context
        answer_words = set(test_case.actual_output.lower().split())
        context_text = " ".join(test_case.retrieval_context).lower()

        supported = sum(1 for word in answer_words if word in context_text and len(word) > 3)
        total = len([w for w in answer_words if len(w) > 3])

        score = supported / total if total > 0 else 0.5

        return MetricResult(
            metric_name=self.metric_name,
            score=score,
            threshold=self.threshold,
            reason=f"{supported}/{total} content words found in context",
            details={"supported_words": supported, "total_words": total},
        )


class AnswerRelevancyMetric:
    """Measures if the answer addresses the user's question.

    Uses LLM-as-judge approach (simplified version).
    """

    def __init__(self, threshold: float = 0.6):
        self.threshold = threshold
        self.metric_name = "answer_relevancy"

    def measure(self, test_case: LLMTestCase) -> MetricResult:
        """Calculate answer relevancy."""
        if not test_case.actual_output:
            return MetricResult(
                metric_name=self.metric_name,
                score=0.0,
                threshold=self.threshold,
                reason="Empty answer",
            )

        # Simple heuristic: word overlap between question and answer
        question_words = set(test_case.input.lower().split())
        answer_words = set(test_case.actual_output.lower().split())

        # Remove common stop words
        stop_words = {
            "的",
            "是",
            "在",
            "了",
            "有",
            "和",
            "与",
            "或",
            "这",
            "那",
            "我",
            "你",
            "他",
            "a",
            "the",
            "is",
            "are",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
        }
        question_words -= stop_words
        answer_words -= stop_words

        if question_words:
            overlap = len(question_words & answer_words)
            score = min(1.0, overlap / len(question_words) * 1.5)  # Boost for good coverage
        else:
            score = 0.5

        return MetricResult(
            metric_name=self.metric_name,
            score=score,
            threshold=self.threshold,
            reason=f"Relevancy score: {score:.2f}",
        )


class ToolCorrectnessMetric:
    """Measures if the correct tools were called."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.metric_name = "tool_correctness"

    def measure(self, test_case: AgentTestCase) -> MetricResult:
        """Calculate tool correctness."""
        if not test_case.expected_tools:
            return MetricResult(
                metric_name=self.metric_name,
                score=1.0,
                threshold=self.threshold,
                reason="No expected tools specified",
            )

        called = set(test_case.tools_called)
        expected = set(test_case.expected_tools)

        if not expected:
            score = 1.0
        else:
            correct = len(called & expected)
            score = correct / len(expected)

        return MetricResult(
            metric_name=self.metric_name,
            score=score,
            threshold=self.threshold,
            reason=f"{len(called & expected)}/{len(expected)} expected tools were called",
            details={"called": list(called), "expected": list(expected)},
        )


class StepEfficiencyMetric:
    """Measures if the agent completed the task efficiently."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.metric_name = "step_efficiency"

    def measure(self, test_case: AgentTestCase) -> MetricResult:
        """Calculate step efficiency."""
        if test_case.trajectory_steps == 0:
            score = 1.0
        else:
            # Efficiency = 1 - (actual_steps / max_steps)
            efficiency = 1 - (test_case.trajectory_steps / test_case.max_steps)
            score = max(0, efficiency)

        return MetricResult(
            metric_name=self.metric_name,
            score=score,
            threshold=self.threshold,
            reason=f"Used {test_case.trajectory_steps}/{test_case.max_steps} steps",
        )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class RAGEvaluator:
    """Main evaluator for RAG systems."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.metrics = {
            "context_precision": ContextPrecisionMetric(threshold),
            "context_recall": ContextRecallMetric(threshold),
            "faithfulness": FaithfulnessMetric(threshold),
            "answer_relevancy": AnswerRelevancyMetric(threshold),
        }

    def evaluate(self, test_case: LLMTestCase) -> dict[str, MetricResult]:
        """Evaluate a test case across all metrics."""
        results = {}
        for name, metric in self.metrics.items():
            results[name] = metric.measure(test_case)
        return results

    def evaluate_rag_score(self, test_case: LLMTestCase) -> float:
        """Calculate overall RAG score (RAGAS-like)."""
        results = self.evaluate(test_case)

        # Weighted average
        weights = {
            "context_precision": 0.25,
            "context_recall": 0.25,
            "faithfulness": 0.3,
            "answer_relevancy": 0.2,
        }

        total_score = 0
        for name, weight in weights.items():
            if name in results:
                total_score += results[name].score * weight

        return total_score


class AgentEvaluator:
    """Evaluator for agent systems."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.metrics = {
            "tool_correctness": ToolCorrectnessMetric(threshold),
            "step_efficiency": StepEfficiencyMetric(threshold),
        }

    def evaluate(self, test_case: AgentTestCase) -> dict[str, MetricResult]:
        """Evaluate an agent test case."""
        results = {}
        for name, metric in self.metrics.items():
            results[name] = metric.measure(test_case)
        return results


# ---------------------------------------------------------------------------
# Evaluation Runner
# ---------------------------------------------------------------------------


class EvaluationSuite:
    """Run evaluations on a RAG system."""

    def __init__(self, rag_pipeline: Any = None, agent: Any = None):
        self.rag = rag_pipeline
        self.agent = agent
        self.rag_evaluator = RAGEvaluator()
        self.agent_evaluator = AgentEvaluator()

    def run_rag_evaluation(self, test_cases: list[LLMTestCase]) -> dict[str, Any]:
        """Run evaluation on multiple test cases."""
        all_results = []

        for i, test_case in enumerate(test_cases):
            # Run RAG if pipeline available
            if self.rag and not test_case.actual_output:
                response = self.rag.ask(
                    question=test_case.input,
                    use_memory=False,
                    use_query_expansion=False,
                )
                test_case.actual_output = response.answer
                test_case.retrieval_context = [r.chunk.get("text", "") for r in response.sources]

            # Evaluate
            results = self.rag_evaluator.evaluate(test_case)
            rag_score = self.rag_evaluator.evaluate_rag_score(test_case)

            all_results.append(
                {
                    "test_case": i,
                    "input": test_case.input,
                    "rag_score": rag_score,
                    "metrics": {name: r.score for name, r in results.items()},
                }
            )

        # Aggregate
        avg_rag_score = (
            sum(r["rag_score"] for r in all_results) / len(all_results) if all_results else 0
        )

        return {
            "total_test_cases": len(test_cases),
            "average_rag_score": avg_rag_score,
            "results": all_results,
        }

    def run_agent_evaluation(self, test_cases: list[AgentTestCase]) -> dict[str, Any]:
        """Run evaluation on agent test cases."""
        all_results = []

        for i, test_case in enumerate(test_cases):
            # Run agent if available
            if self.agent and not test_case.actual_output:
                response = self.agent.run_sync(test_case.input)
                test_case.actual_output = response
                test_case.trajectory_steps = len(self.agent.get_trajectory().steps)
                test_case.tools_called = [
                    s.tool_name for s in self.agent.get_trajectory().steps if s.tool_name
                ]

            # Evaluate
            results = self.agent_evaluator.evaluate(test_case)

            all_results.append(
                {
                    "test_case": i,
                    "input": test_case.input,
                    "metrics": {name: r.score for name, r in results.items()},
                }
            )

        return {
            "total_test_cases": len(test_cases),
            "results": all_results,
        }

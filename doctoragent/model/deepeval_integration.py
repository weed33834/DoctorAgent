"""DeepEval-backed RAG evaluation (replaces hand-rolled RAGAS-style metrics).

This module adapts the :mod:`doctoragent.model.evaluation` metric surface to
the industry-standard **DeepEval** library so we stop hand-rolling
Faithfulness / Answer Relevancy / Context Precision / Context Recall and
instead reuse a maintained, peer-reviewed implementation.

Design notes
------------
* DeepEval's metrics require an LLM judge. When the caller does not supply
  one (no ``OPENAI_API_KEY`` / no provider) every metric degrades to the
  **deterministic fallback** already implemented in
  :mod:`doctoragent.model.evaluation` — the platform stays fully runnable
  offline. This mirrors the clinical workflow's no-LLM degradation pattern.
* The :class:`RAGEvaluator` returns the same :class:`MetricResult` shape the
  hand-rolled metrics did, so :mod:`doctoragent.api.advanced_routes` and the
  console keep working unchanged.
* DeepEval is an optional dependency (``observability``-adjacent). When it
  is not installed, :func:`deepeval_available` returns ``False`` and the
  evaluator transparently falls back to the hand-rolled metrics — same
  pattern as the LangGraph / hand-rolled orchestrator split.
"""

from __future__ import annotations

import logging
from typing import Any

from doctoragent.model.evaluation import (
    LLMTestCase,
    MetricResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RAGEvaluationReport",
    "RAGEvaluator",
    "deepeval_available",
]


def deepeval_available() -> bool:
    """Return ``True`` when the ``deepeval`` package is importable."""
    try:
        import deepeval  # noqa: F401 — import probe
    except ImportError:
        return False
    return True


class RAGEvaluationReport:
    """Aggregated result of running the four RAGAS-equivalent metrics.

    Mirrors the shape the console ``/evaluate`` endpoint renders, so the UI
    is identical whether DeepEval or the fallback ran.
    """

    def __init__(self, results: list[MetricResult], engine: str) -> None:
        self.results = results
        self.engine = engine

    def to_dict(self) -> dict[str, Any]:
        passed = sum(1 for r in self.results if r.passed)
        return {
            "engine": self.engine,
            "metrics": [r.__dict__ if hasattr(r, "__dict__") else r for r in self.results],
            "summary": {
                "total": len(self.results),
                "passed": passed,
                "failed": len(self.results) - passed,
                "average_score": (
                    sum(r.score for r in self.results) / len(self.results) if self.results else 0.0
                ),
            },
        }


class RAGEvaluator:
    """Run Faithfulness / Answer Relevancy / Context Precision / Recall.

    Uses DeepEval when available; otherwise falls back to the hand-rolled
    metrics in :mod:`doctoragent.model.evaluation` so the evaluator is
    always callable (important for the ``/evaluate`` console endpoint and
    CI runs without an LLM judge key).
    """

    def __init__(
        self,
        threshold: float = 0.5,
        judge_model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.threshold = threshold
        self.judge_model = judge_model
        self.api_key = api_key
        self.base_url = base_url

    async def evaluate(self, test_case: LLMTestCase) -> RAGEvaluationReport:
        """Run the four-metric RAG suite against *test_case*.

        Returns a :class:`RAGEvaluationReport`. Never raises — metric
        failures degrade to score 0 with the failure reason captured.
        """
        if deepeval_available():
            try:
                return await self._evaluate_deepeval(test_case)
            except Exception as exc:  # noqa: BLE001 — never break eval on judge err
                logger.warning(
                    "DeepEval evaluation failed (%s); falling back to deterministic metrics",
                    exc,
                    exc_info=True,
                )
        return self._evaluate_fallback(test_case)

    # ------------------------------------------------------------------
    # DeepEval backend
    # ------------------------------------------------------------------

    async def _evaluate_deepeval(self, test_case: LLMTestCase) -> RAGEvaluationReport:
        from deepeval.metrics import (
            AnswerRelevancyMetric,
            ContextualPrecisionMetric,
            ContextualRecallMetric,
            FaithfulnessMetric,
        )
        from deepeval.test_case import LLMTestCase as DeepEvalTestCase

        # Build the judge model kwargs. DeepEval's OpenAI-backed GPTModel
        # accepts api_key + base_url; when neither is set it reads the env
        # (OPENAI_API_KEY). When no key is configured at all the metric
        # raises DeepEvalError at measure() time — caught by the caller's
        # fallback wrapper.
        model_kwargs: dict[str, Any] = {"model": self.judge_model}
        if self.api_key:
            model_kwargs["api_key"] = self.api_key
        if self.base_url:
            model_kwargs["base_url"] = self.base_url

        dtc = DeepEvalTestCase(
            input=test_case.input,
            actual_output=test_case.actual_output,
            expected_output=test_case.expected_output or None,
            retrieval_context=test_case.retrieval_context or None,
            context=test_case.context or None,
        )

        metrics = [
            FaithfulnessMetric(threshold=self.threshold, **model_kwargs),
            AnswerRelevancyMetric(threshold=self.threshold, **model_kwargs),
            ContextualPrecisionMetric(threshold=self.threshold, **model_kwargs),
            ContextualRecallMetric(threshold=self.threshold, **model_kwargs),
        ]

        results: list[MetricResult] = []
        for metric in metrics:
            # DeepEval's measure() is synchronous (it may call the LLM).
            # Run it off the event loop so the async API stays non-blocking.
            import asyncio

            try:
                await asyncio.to_thread(metric.measure, dtc)
                results.append(
                    MetricResult(
                        metric_name=metric.__name__
                        if hasattr(metric, "__name__")
                        else type(metric).__name__.replace("Metric", "").lower(),
                        score=float(metric.score),
                        threshold=float(self.threshold),
                        passed=bool(metric.is_successful()),
                        reason=str(getattr(metric, "reason", "") or ""),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — per-metric isolation
                results.append(
                    MetricResult(
                        metric_name=type(metric).__name__,
                        score=0.0,
                        threshold=self.threshold,
                        passed=False,
                        reason=f"metric failed: {exc}",
                    )
                )

        return RAGEvaluationReport(results, engine="deepeval")

    # ------------------------------------------------------------------
    # Deterministic fallback (no LLM judge required)
    # ------------------------------------------------------------------

    def _evaluate_fallback(self, test_case: LLMTestCase) -> RAGEvaluationReport:
        from doctoragent.model.evaluation import (
            AnswerRelevancyMetric,
            ContextPrecisionMetric,
            ContextRecallMetric,
            FaithfulnessMetric,
        )

        metrics = [
            ContextPrecisionMetric(threshold=self.threshold),
            ContextRecallMetric(threshold=self.threshold),
            FaithfulnessMetric(threshold=self.threshold),
            AnswerRelevancyMetric(threshold=self.threshold),
        ]
        results: list[MetricResult] = []
        for metric in metrics:
            try:
                results.append(metric.measure(test_case))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    MetricResult(
                        metric_name=getattr(metric, "metric_name", type(metric).__name__),
                        score=0.0,
                        threshold=self.threshold,
                        passed=False,
                        reason=f"metric failed: {exc}",
                    )
                )
        return RAGEvaluationReport(results, engine="deterministic-fallback")

"""Clinical benchmark evaluation framework (design doc §4.5 / Phase-C3).

Measures the model-under-test (MUT) against standard clinical QA datasets
(MedQA / PubMedQA / MedMCQA) and reports a multi-dimensional score card:

* **accuracy** — exact-match / letter-match for MCQ, macro-F1 for
  yes/no/maybe classification,
* **calibration** — Expected Calibration Error (ECE) + Brier score from
  the model's self-reported confidence,
* **safety** — fraction of answers that survive the deterministic
  :class:`~doctoragent.clinical.safety.ClinicalGuardrails` (block rate,
  correct-refusal rate on unsafe queries),
* **citation** — fraction of answers carrying a verifiable citation,
* **latency** — p50 / p95 wall-clock per case.

A free-text rationale is judged by an **LLM-as-judge** that MUST belong to
a different model family than the MUT (cross-family judging) — see
:class:`LLMJudge`.

The framework is dependency-light: only ``pydantic`` (core dep) and the
existing clinical stack are required. HuggingFace ``datasets`` is loaded
opportunistically for real MedQA/PubMedQA; when absent the loader falls
back to a local JSONL file of the same shape.
"""

from __future__ import annotations

from doctoragent.clinical.evaluation.benchmark import (
    BenchmarkCase,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkSuite,
    CaseOutcome,
    MetricBundle,
    Predictor,
    PredictorKind,
    SamplePredictor,
    format_report_markdown,
)
from doctoragent.clinical.evaluation.datasets import (
    DATASET_SHAPES,
    DatasetShape,
    load_dataset,
)
from doctoragent.clinical.evaluation.judge import (
    JUDGE_FAMILY,
    JudgeFamily,
    JudgeVerdict,
    LLMJudge,
    detect_model_family,
)

__all__ = [
    "BenchmarkCase",
    "BenchmarkReport",
    "BenchmarkRunner",
    "BenchmarkSuite",
    "CaseOutcome",
    "DATASET_SHAPES",
    "DatasetShape",
    "JUDGE_FAMILY",
    "JudgeFamily",
    "JudgeVerdict",
    "LLMJudge",
    "MetricBundle",
    "Predictor",
    "PredictorKind",
    "SamplePredictor",
    "detect_model_family",
    "format_report_markdown",
    "load_dataset",
]

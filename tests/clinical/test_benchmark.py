"""Tests for the Phase-C3 clinical benchmark evaluation framework.

Covers:
* Dataset loading (sample fallback, local JSONL, unknown dataset rejection).
* Record normalisation (MedQA letter↔index, PubMedQA ``final_decision``,
  nested ``context`` flattening).
* Answer parsing (MCQ letter extraction with markers, PubMedQA labels,
  citation detection).
* Scorers (accuracy, macro-F1 + per-class, ECE, Brier, percentiles).
* Predictor protocol (SamplePredictor, custom predictor, error capture).
* LLM-as-judge cross-family enforcement, JSON parsing repair, fallback
  scorer.
* End-to-end runner report + markdown formatting.
* Guardrail post-hoc action for raw-LLM predictors.
* Unsafe-case correct-refusal semantics.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import pytest

from doctoragent.clinical.evaluation import (
    BenchmarkCase,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkSuite,
    JudgeFamily,
    LLMJudge,
    Predictor,
    PredictorKind,
    SamplePredictor,
    format_report_markdown,
    load_dataset,
)
from doctoragent.clinical.evaluation.benchmark import (
    _brier,
    _correct_for_case,
    _ece,
    _looks_like_refusal,
    _macro_f1,
    _percentile,
    answer_is_citation,
    parse_label_answer,
    parse_mcq_answer,
)
from doctoragent.clinical.evaluation.datasets import (
    DATASET_SHAPES,
    _normalise_record,
)
from doctoragent.clinical.evaluation.judge import detect_model_family

# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class TestDatasetLoading:
    def test_unknown_dataset_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown dataset"):
            load_dataset("does-not-exist")

    def test_medqa_sample_fallback(self) -> None:
        # No HF + no path → built-in sample. Force path=None and rely on
        # the absence of the ``datasets`` extra in the test env.
        records = load_dataset("medqa", limit=2)
        assert len(records) == 2
        assert records[0]["source"] == "medqa"
        assert records[0]["answer"] in {"A", "B", "C", "D"}
        assert 0 <= records[0]["answer_idx"] <= 3

    def test_pubmedqa_sample_fallback(self) -> None:
        records = load_dataset("pubmedqa", limit=3)
        assert len(records) == 3
        assert records[0]["source"] == "pubmedqa"
        assert records[0]["answer"] in {"yes", "no", "maybe"}

    def test_local_jsonl_file_wins(self, tmp_path: Path) -> None:
        # A local JSONL file overrides HF / sample fallback.
        records_out = [
            {"question": "q1", "options": {"A": "x", "B": "y"}, "answer_idx": "B"},
            {"question": "q2", "options": {"A": "p", "B": "q"}, "answer_idx": "A"},
        ]
        f = tmp_path / "medqa.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in records_out), encoding="utf-8")
        loaded = load_dataset("medqa", path=f)
        assert len(loaded) == 2
        assert loaded[0]["question"] == "q1"
        assert loaded[0]["answer"] == "B"
        assert loaded[0]["answer_idx"] == 1

    def test_local_json_array_file(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text(json.dumps([{"question": "q", "answer": "yes"}]), encoding="utf-8")
        loaded = load_dataset("pubmedqa", path=f)
        assert loaded[0]["answer"] == "yes"

    def test_limit_truncates(self) -> None:
        assert len(load_dataset("medqa", limit=2)) == 2

    def test_case_id_assigned_when_missing(self, tmp_path: Path) -> None:
        f = tmp_path / "m.jsonl"
        f.write_text(json.dumps({"question": "q", "answer": "A"}), encoding="utf-8")
        loaded = load_dataset("medqa", path=f)
        assert loaded[0]["case_id"] == "medqa-0000"


# ---------------------------------------------------------------------------
# Record normalisation
# ---------------------------------------------------------------------------


class TestNormalisation:
    def test_medqa_dict_options_sorted_and_letter_answer(self) -> None:
        rec = _normalise_record(
            {
                "options": {"A": "alpha", "B": "beta"},
                "answer_idx": "B",
            },
            DATASET_SHAPES["medqa"],
        )
        assert rec["options"] == ["alpha", "beta"]
        assert rec["answer"] == "B"
        assert rec["answer_idx"] == 1

    def test_pubmedqa_final_decision_string(self) -> None:
        rec = _normalise_record(
            {"question": "q", "final_decision": "maybe", "long_answer": "because"},
            DATASET_SHAPES["pubmedqa"],
        )
        assert rec["answer"] == "maybe"
        assert rec["rationale"] == "because"

    def test_pubmedqa_nested_context_flattened(self) -> None:
        rec = _normalise_record(
            {
                "question": "q",
                "final_decision": "yes",
                "context": {
                    "contexts": ["sentence one", "sentence two"],
                    "labels": ["BACKGROUND", "RESULT"],
                },
            },
            DATASET_SHAPES["pubmedqa"],
        )
        assert "sentence one" in rec["context"]
        assert "sentence two" in rec["context"]

    def test_medqa_integer_answer_converted_to_letter(self) -> None:
        rec = _normalise_record(
            {"options": {"A": "x", "B": "y", "C": "z"}, "answer": 2},
            DATASET_SHAPES["medqa"],
        )
        assert rec["answer"] == "C"
        assert rec["answer_idx"] == 2

    def test_meta_preserves_extra_keys(self) -> None:
        rec = _normalise_record(
            {"question": "q", "answer": "yes", "pubid": 42, "year": 2020},
            DATASET_SHAPES["pubmedqa"],
        )
        assert rec["meta"].get("pubid") == 42 or "pubid" not in rec["meta"]
        # ``pubid`` is excluded from meta (it's used as case_id fallback).
        assert rec["meta"].get("year") == 2020


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------


class TestAnswerParsing:
    def test_mcq_explicit_answer_marker(self) -> None:
        letter, idx = parse_mcq_answer("The answer is: C", 4)
        assert letter == "C"
        assert idx == 2

    def test_mcq_chinese_marker(self) -> None:
        letter, idx = parse_mcq_answer("答案：B", 4)
        assert letter == "B"
        assert idx == 1

    def test_mcq_leading_letter(self) -> None:
        letter, idx = parse_mcq_answer("A. The first option is correct.", 4)
        assert letter == "A"
        assert idx == 0

    def test_mcq_first_standalone_letter(self) -> None:
        letter, idx = parse_mcq_answer("I think it is D because...", 4)
        assert letter == "D"

    def test_mcq_empty(self) -> None:
        assert parse_mcq_answer("", 4) == ("", -1)

    def test_mcq_out_of_range_ignored(self) -> None:
        # E is out of range for 4 options.
        letter, idx = parse_mcq_answer("answer: E", 4)
        assert letter == ""
        assert idx == -1

    def test_label_explicit_marker(self) -> None:
        assert parse_label_answer("answer: no", ("yes", "no", "maybe")) == "no"

    def test_label_first_occurrence(self) -> None:
        assert parse_label_answer("Yes — it works", ("yes", "no", "maybe")) == "yes"

    def test_label_empty(self) -> None:
        assert parse_label_answer("", ("yes", "no", "maybe")) == ""

    def test_citation_detection(self) -> None:
        assert answer_is_citation("see PMID: 12345")
        assert answer_is_citation("doi: 10.1234/foo")
        assert answer_is_citation("per WHO guidance")
        assert answer_is_citation("指南：GINA")
        assert not answer_is_citation("no source here")


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------


class TestScorers:
    def test_correct_mcq_by_index(self) -> None:
        case = BenchmarkCase(
            case_id="x", question="q", options=["a", "b"], answer="B", answer_idx=1
        )
        assert _correct_for_case(case, 1, "B") is True
        assert _correct_for_case(case, 0, "A") is False

    def test_correct_mcq_by_letter_when_idx_missing(self) -> None:
        case = BenchmarkCase(case_id="x", question="q", answer="B", answer_idx=-1)
        assert _correct_for_case(case, -1, "B") is True

    def test_correct_classification(self) -> None:
        case = BenchmarkCase(
            case_id="x", source="pubmedqa", kind="classification", question="q",
            answer="yes",
        )
        assert _correct_for_case(case, -1, "yes") is True
        assert _correct_for_case(case, -1, "no") is False
        assert _correct_for_case(case, -1, "") is False

    def test_macro_f1_perfect(self) -> None:
        f1, pc = _macro_f1(["yes", "no", "yes"], ["yes", "no", "yes"])
        assert f1 == 1.0
        assert pc["yes"]["f1"] == 1.0
        assert pc["no"]["f1"] == 1.0

    def test_macro_f1_partial(self) -> None:
        f1, pc = _macro_f1(["yes", "no"], ["yes", "yes"])
        # yes: tp=1,fp=1,fn=0 → prec=0.5, rec=1.0, f1=0.667
        # no: tp=0,fp=0,fn=1 → 0
        assert math.isclose(f1, (2 / 3 + 0.0) / 2)
        assert pc["yes"]["fp"] == 1.0

    def test_macro_f1_empty(self) -> None:
        assert _macro_f1([], [])[0] == 0.0

    def test_ece_zero_when_perfectly_calibrated(self) -> None:
        # confidence 1.0 always correct → ECE 0.
        assert _ece([1.0, 1.0, 1.0], [True, True, True]) == 0.0

    def test_ece_high_when_overconfident(self) -> None:
        # confidence 1.0 always wrong → large ECE.
        ece = _ece([1.0, 1.0], [False, False])
        assert ece > 0.5

    def test_ece_empty(self) -> None:
        assert _ece([], []) == 0.0

    def test_brier_perfect(self) -> None:
        assert _brier([1.0, 0.0], [True, False]) == 0.0

    def test_brier_worst(self) -> None:
        assert _brier([1.0, 1.0], [False, False]) == 1.0

    def test_percentile(self) -> None:
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == pytest.approx(4.8)
        assert _percentile([], 0.5) == 0.0


# ---------------------------------------------------------------------------
# Refusal heuristic
# ---------------------------------------------------------------------------


class TestRefusalHeuristic:
    @pytest.mark.parametrize(
        "text",
        [
            "我无法回答该问题",
            "需医生人工评估",
            "I cannot answer this question",
            "consult a physician",
        ],
    )
    def test_refusal_detected(self, text: str) -> None:
        assert _looks_like_refusal(text) is True

    @pytest.mark.parametrize("text", ["the answer is B", "metformin 500mg"])
    def test_non_refusal(self, text: str) -> None:
        assert _looks_like_refusal(text) is False


# ---------------------------------------------------------------------------
# Model family detection + cross-family judge
# ---------------------------------------------------------------------------


class TestModelFamily:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("gpt-4o", JudgeFamily.OPENAI),
            ("o1-preview", JudgeFamily.OPENAI),
            ("claude-3-opus", JudgeFamily.ANTHROPIC),
            ("llama3:8b", JudgeFamily.META),
            ("meta-llama/Llama-3-70B", JudgeFamily.META),
            ("gemini-1.5-pro", JudgeFamily.GOOGLE),
            ("mistral-large", JudgeFamily.MISTRAL),
            ("qwen3:8b", JudgeFamily.QWEN),
            ("deepseek-r1", JudgeFamily.DEEPSEEK),
            ("command-r-plus", JudgeFamily.COHERE),
            ("unknown-model", JudgeFamily.UNKNOWN),
            ("", JudgeFamily.UNKNOWN),
        ],
    )
    def test_detect(self, name: str, expected: JudgeFamily) -> None:
        assert detect_model_family(name) == expected

    def test_ollama_prefix_uses_underlying_model(self) -> None:
        # ``ollama/qwen3:8b`` → family read from qwen, not the host alias.
        assert detect_model_family("ollama/qwen3:8b") == JudgeFamily.QWEN

    def test_cross_family_same_family_raises(self) -> None:
        judge = LLMJudge(judge_family=JudgeFamily.OPENAI, model_name="gpt-4o")
        with pytest.raises(ValueError, match="cross-family"):
            judge.assert_cross_family("gpt-4o-mini")

    def test_cross_family_unknown_mut_allowed(self) -> None:
        # An UNKNOWN-family MUT never triggers the guard (we can't prove
        # a collision), so judging is allowed.
        judge = LLMJudge(judge_family=JudgeFamily.OPENAI)
        judge.assert_cross_family("unknown-model")

    def test_cross_family_disabled(self) -> None:
        judge = LLMJudge(
            judge_family=JudgeFamily.OPENAI, enforce_cross_family=False
        )
        # No raise even though families match.
        judge.assert_cross_family("gpt-4o")


# ---------------------------------------------------------------------------
# LLMJudge
# ---------------------------------------------------------------------------


class TestLLMJudge:
    def _case(self) -> dict[str, Any]:
        return {
            "question": "Does metformin reduce mortality?",
            "options": "",
            "context": "",
            "answer": "yes",
            "rationale": "cohort studies show benefit",
        }

    def test_parse_valid_json(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        verdict = judge._parse_judge_response(
            '{"score": 0.8, "correct": true, "citation_present": true, '
            '"safety_concern": false, "reasoning": "ok"}'
        )
        assert verdict.score == 0.8
        assert verdict.correct is True
        assert verdict.citation_present is True
        assert verdict.fallback is False

    def test_parse_json_in_prose(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        verdict = judge._parse_judge_response(
            'Here is my verdict: {"score": 0.5, "correct": false, "reasoning": "weak"}'
        )
        assert verdict.score == 0.5
        assert verdict.correct is False

    def test_parse_invalid_returns_fallback(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        verdict = judge._parse_judge_response("not json at all")
        assert verdict.fallback is True
        assert verdict.score == 0.0

    def test_parse_empty(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        verdict = judge._parse_judge_response("")
        assert verdict.fallback is True

    def test_parse_score_clamped(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        verdict = judge._parse_judge_response('{"score": 5.0, "correct": true}')
        assert verdict.score == 1.0
        verdict2 = judge._parse_judge_response('{"score": -1.0, "correct": false}')
        assert verdict2.score == 0.0

    def test_sync_chat_called(self) -> None:
        calls: list[list[dict]] = []

        def chat(messages: list[dict]) -> str:
            calls.append(messages)
            return '{"score": 0.7, "correct": true, "reasoning": "good"}'

        judge = LLMJudge(
            judge_family=JudgeFamily.ANTHROPIC,
            model_name="claude",
            chat=chat,
            enforce_cross_family=False,
        )
        verdict = judge.judge_sync(self._case(), "yes because reasons", mut_model="mut")
        assert verdict.score == 0.7
        assert len(calls) == 1
        assert calls[0][0]["role"] == "system"

    def test_fallback_when_no_llm(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        # Prediction overlaps heavily with the gold rationale ("cohort
        # studies show benefit") → token-Jaccard above the 0.3 threshold.
        verdict = judge.judge_sync(
            self._case(), "yes — cohort studies show benefit", mut_model="mut"
        )
        assert verdict.fallback is True
        assert verdict.correct is True

    def test_fallback_low_overlap_is_incorrect(self) -> None:
        judge = LLMJudge(enforce_cross_family=False)
        verdict = judge.judge_sync(
            self._case(), "unrelated nonsense text", mut_model="mut"
        )
        assert verdict.fallback is True
        assert verdict.correct is False


# ---------------------------------------------------------------------------
# Predictor + Runner
# ---------------------------------------------------------------------------


class _StubPredictor(Predictor):
    """Returns canned text per case_id for deterministic scoring."""

    kind = PredictorKind.RAW_LLM
    model_name = "stub"

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers

    async def predict(self, case: BenchmarkCase) -> dict[str, Any]:
        return {
            "text": self._answers.get(case.case_id, ""),
            "confidence": 0.9,
            "latency_s": 0.01,
        }


class TestRunner:
    def _medqa_suite(self) -> BenchmarkSuite:
        cases = [
            BenchmarkCase(
                case_id="m1", source="medqa", kind="mcq",
                question="q1", options=["a", "b", "c", "d"], answer="A", answer_idx=0,
            ),
            BenchmarkCase(
                case_id="m2", source="medqa", kind="mcq",
                question="q2", options=["a", "b", "c", "d"], answer="C", answer_idx=2,
            ),
        ]
        return BenchmarkSuite(name="medqa", cases=cases)

    def test_run_suite_basic(self) -> None:
        suite = self._medqa_suite()
        pred = _StubPredictor({"m1": "Answer: A", "m2": "the answer is C"})
        runner = BenchmarkRunner(predictor=pred)
        bundle, outcomes = asyncio.run(runner.run_suite(suite))
        assert bundle.n == 2
        assert bundle.accuracy == 1.0
        assert all(o.correct for o in outcomes)
        assert bundle.errors == 0

    def test_run_suite_partial_accuracy(self) -> None:
        suite = self._medqa_suite()
        pred = _StubPredictor({"m1": "A", "m2": "B"})  # m2 wrong
        runner = BenchmarkRunner(predictor=pred)
        bundle, _ = asyncio.run(runner.run_suite(suite))
        assert bundle.accuracy == 0.5

    def test_predictor_exception_captured_as_error(self) -> None:
        class Boom(Predictor):
            async def predict(self, case: BenchmarkCase) -> dict[str, Any]:
                raise RuntimeError("boom")

        suite = self._medqa_suite()
        runner = BenchmarkRunner(predictor=Boom())
        bundle, outcomes = asyncio.run(runner.run_suite(suite))
        assert bundle.errors == 2
        assert all("RuntimeError" in o.error for o in outcomes)

    def test_unsafe_case_correct_refusal(self) -> None:
        cases = [
            BenchmarkCase(
                case_id="u1", source="medqa", kind="mcq", question="bad",
                options=[], answer="", unsafe=True,
            ),
        ]
        suite = BenchmarkSuite(name="adv", cases=cases)
        pred = _StubPredictor({"u1": "我无法回答该问题"})
        runner = BenchmarkRunner(predictor=pred)
        bundle, outcomes = asyncio.run(runner.run_suite(suite))
        # Refusal phrase detected → correct=True for unsafe case.
        assert outcomes[0].correct is True
        assert bundle.correct_refusal_rate == 1.0

    def test_unsafe_case_answered_is_incorrect(self) -> None:
        cases = [
            BenchmarkCase(
                case_id="u1", source="medqa", kind="mcq", question="bad",
                options=["a", "b"], answer="A", answer_idx=0, unsafe=True,
            ),
        ]
        suite = BenchmarkSuite(name="adv", cases=cases)
        pred = _StubPredictor({"u1": "use 500mg of the drug"})
        runner = BenchmarkRunner(predictor=pred)
        bundle, outcomes = asyncio.run(runner.run_suite(suite))
        # Answered (not refused) on an unsafe case → incorrect.
        assert outcomes[0].correct is False
        assert bundle.correct_refusal_rate == 0.0

    def test_post_hoc_guardrail_action_for_raw_llm(self) -> None:
        # A raw-LLM predictor emits a forbidden definitive diagnosis → the
        # runner's post-hoc guardrail should flag/block it.
        from doctoragent.clinical.safety import ClinicalGuardrails

        cases = [
            BenchmarkCase(
                case_id="d1", source="medqa", kind="mcq",
                question="q", options=["a", "b"], answer="A", answer_idx=0,
            ),
        ]
        suite = BenchmarkSuite(name="d", cases=cases)
        pred = _StubPredictor({"d1": "确诊为肺癌，需立即化疗。"})
        runner = BenchmarkRunner(
            predictor=pred, guardrails=ClinicalGuardrails()
        )
        bundle, outcomes = asyncio.run(runner.run_suite(suite))
        # Forbidden content → block.
        assert outcomes[0].guardrail_action == "block"
        assert bundle.safety_rate == 0.0

    def test_calibration_metrics(self) -> None:
        # A perfectly-calibrated predictor: confidence 1.0, all correct →
        # ECE 0 and Brier 0. (The default _StubPredictor uses 0.9, which is
        # *not* perfectly calibrated — covered by the ece/brier unit tests.)
        suite = self._medqa_suite()

        class _ConfidentStub(_StubPredictor):
            async def predict(self, case: BenchmarkCase) -> dict[str, Any]:
                out = await super().predict(case)
                out["confidence"] = 1.0
                return out

        pred = _ConfidentStub({"m1": "A", "m2": "C"})
        runner = BenchmarkRunner(predictor=pred)
        bundle, _ = asyncio.run(runner.run_suite(suite))
        assert bundle.ece == 0.0
        assert bundle.brier == 0.0

    def test_calibration_overconfident(self) -> None:
        # StubPredictor default confidence 0.9 but all correct → ECE 0.1.
        suite = self._medqa_suite()
        pred = _StubPredictor({"m1": "A", "m2": "C"})
        runner = BenchmarkRunner(predictor=pred)
        bundle, _ = asyncio.run(runner.run_suite(suite))
        assert bundle.ece == pytest.approx(0.1)
        assert bundle.brier == pytest.approx(0.01)

    def test_concurrency_does_not_drop_cases(self) -> None:
        suite = self._medqa_suite()
        pred = _StubPredictor({"m1": "A", "m2": "C"})
        runner = BenchmarkRunner(predictor=pred, concurrency=4)
        bundle, outcomes = asyncio.run(runner.run_suite(suite))
        assert bundle.n == 2
        assert len(outcomes) == 2

    def test_full_report_markdown(self) -> None:
        suite = self._medqa_suite()
        pred = _StubPredictor({"m1": "A", "m2": "C"})
        runner = BenchmarkRunner(predictor=pred)
        report = asyncio.run(runner.run([suite], model_under_test="stub"))
        md = format_report_markdown(report)
        assert "DoctorAgent Clinical Benchmark Report" in md
        assert "stub" in md
        assert "medqa" in md
        assert "Radar dimensions" in md


# ---------------------------------------------------------------------------
# SamplePredictor
# ---------------------------------------------------------------------------


class TestSamplePredictor:
    def test_safe_case_echoes_gold_with_citation(self) -> None:
        case = BenchmarkCase(
            case_id="x", source="medqa", kind="mcq",
            question="q", options=["a", "b"], answer="A", answer_idx=0,
        )
        pred = SamplePredictor()
        out = asyncio.run(pred.predict(case))
        assert "A" in out["text"]
        assert answer_is_citation(out["text"])
        assert out["guardrail_action"] == "allow"

    def test_unsafe_case_refused(self) -> None:
        case = BenchmarkCase(
            case_id="x", source="medqa", kind="mcq",
            question="q", options=[], answer="", unsafe=True,
        )
        pred = SamplePredictor()
        out = asyncio.run(pred.predict(case))
        assert out["guardrail_action"] == "block"
        assert _looks_like_refusal(out["text"])

    def test_predict_sync_in_no_loop(self) -> None:
        case = BenchmarkCase(
            case_id="x", source="medqa", kind="mcq",
            question="q", options=["a"], answer="A", answer_idx=0,
        )
        pred = SamplePredictor()
        out = pred.predict_sync(case)
        assert "A" in out["text"]


# ---------------------------------------------------------------------------
# Full end-to-end with built-in samples
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_sample_run(self) -> None:
        medqa = load_dataset("medqa", limit=5)
        pubmed = load_dataset("pubmedqa", limit=5)
        suites = [
            BenchmarkSuite(name="medqa", cases=[BenchmarkCase.from_record(r) for r in medqa]),
            BenchmarkSuite(
                name="pubmedqa", cases=[BenchmarkCase.from_record(r) for r in pubmed]
            ),
        ]
        runner = BenchmarkRunner(
            predictor=SamplePredictor(),
            judge=LLMJudge(enforce_cross_family=False),
        )
        report = asyncio.run(runner.run(suites, judge=True, model_under_test="sample"))
        assert isinstance(report, BenchmarkReport)
        assert set(report.suites) == {"medqa", "pubmedqa"}
        # SamplePredictor echoes gold → perfect accuracy.
        assert report.suites["medqa"].accuracy == 1.0
        assert report.suites["pubmedqa"].accuracy == 1.0
        # All sample cases cite (SamplePredictor adds PMID).
        assert report.suites["medqa"].citation_rate == 1.0
        # JSON round-trips.
        j = report.to_json()
        assert json.loads(j)["model_under_test"] == "sample"

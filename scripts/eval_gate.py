#!/usr/bin/env python3
"""Evaluation CI gate (M10.14).

Runs the DoctorAgent evaluation benchmarks and fails the build when any metric
drops below its threshold (quality gate). Used by CI::

    python scripts/eval_gate.py --min-rag-score 0.6 --min-pass-rate 0.85

Exit code 0 = gate passed, 1 = failed. Output is human-readable + a JSON
summary written to ``eval_gate_result.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure the repo root is importable when invoked as ``python scripts/eval_gate.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DoctorAgent evaluation quality gate (M10.14)")
    p.add_argument("--min-rag-score", type=float, default=0.5, help="Minimum overall RAG score")
    p.add_argument("--min-pass-rate", type=float, default=0.8, help="Minimum per-case pass rate")
    p.add_argument("--cases", type=int, default=20, help="Number of sample cases to run")
    p.add_argument("--json-out", default="eval_gate_result.json", help="JSON summary path")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Fail the gate when evaluation extras are missing or no case ran, "
        "instead of skipping (the default skip keeps legacy CI green).",
    )
    p.add_argument("--judge-model", default=None, help="Optional DeepEval judge model name")
    p.add_argument("--judge-api-key", default=None, help="Optional judge API key")
    p.add_argument(
        "--judge-base-url",
        default=None,
        help="Optional OpenAI-compatible base URL for the judge (e.g. local Ollama/vLLM), "
        "so the gate does not require a cloud key.",
    )
    return p.parse_args()


def _judge_from_args(args: argparse.Namespace) -> Any | None:
    """Build a DeepEval-backed judge when requested and available."""
    if not args.judge_model:
        return None
    try:
        from doctoragent.model.deepeval_integration import RAGEvaluator as DeepEvalJudge
    except ImportError:
        print("[eval_gate] --judge-model set but deepeval extra missing", file=sys.stderr)
        return None
    return DeepEvalJudge(
        threshold=args.min_rag_score,
        judge_model=args.judge_model,
        api_key=args.judge_api_key or None,
        base_url=args.judge_base_url or None,
    )


def run_eval(args: argparse.Namespace) -> dict:
    """Run the evaluation benchmark and collect pass-rate + RAG score.

    Honesty note: without a judge this scores the *gold answer against
    itself* (deterministic metrics on known-good grounding) — useful as a
    regression smoke test, NOT as an LLM quality measurement. The result
    JSON reports ``mode: "gold-selfcheck"`` so dashboards don't mistake it
    for a live-model run. Pass ``--judge-model`` (+ optional
    ``--judge-base-url``) for real LLM-judged scoring.
    """
    try:
        from doctoragent.clinical.evaluation.sample_data import SAMPLE_MEDQA
        from doctoragent.model.evaluation import LLMTestCase, RAGEvaluator
    except ImportError:
        return _offline_gate(args)

    judge = _judge_from_args(args)
    evaluator = RAGEvaluator(threshold=0.5)
    cases = SAMPLE_MEDQA[: args.cases]
    total = len(cases)
    passed = 0
    rag_scores = []
    failures: list[str] = []
    for case in cases:
        question = case.get("question", "")
        gold = case.get("answer") or case.get("gold") or case.get("options", {}).get(
            case.get("answer_idx", ""), ""
        )
        if not question:
            continue
        tc = LLMTestCase(
            input=question,
            # Gold-self-check mode: score known-good grounding so metric/
            # pipeline regressions fail loudly. Not an LLM quality signal.
            actual_output=str(gold or ""),
            expected_output=str(gold or ""),
            retrieval_context=[case.get("context", "") or question],
            context=[case.get("context", "") or question],
        )
        try:
            if judge is not None:
                report = asyncio_run(judge.evaluate(tc))
                scores = [float(r.score) for r in report.results]
                score = sum(scores) / len(scores) if scores else 0.0
            else:
                score = evaluator.evaluate_rag_score(tc)
        except Exception:  # noqa: BLE001
            score = 0.0
        rag_scores.append(score)
        if score >= args.min_rag_score:
            passed += 1
        else:
            failures.append(case.get("case_id", "?"))
    pass_rate = (passed / total) if total else 0.0
    rag_score = (sum(rag_scores) / len(rag_scores)) if rag_scores else 0.0
    return {
        "mode": "llm-judged" if judge is not None else "gold-selfcheck",
        "cases": total,
        "passed": passed,
        "pass_rate": round(pass_rate, 3),
        "avg_rag_score": round(rag_score, 3),
        "failed_cases": failures[:20],
        "gate_passed": pass_rate >= args.min_pass_rate and rag_score >= args.min_rag_score,
    }


def asyncio_run(coro: Any) -> Any:
    """Small shim so the module stays importable on any Python ≥3.10."""
    import asyncio

    return asyncio.run(coro)


def _offline_gate(args: argparse.Namespace) -> dict:
    """Gate result when evaluation extras are not installed."""
    print("[eval_gate] evaluation extras not installed; running offline gate", file=sys.stderr)
    strict = getattr(args, "strict", False)
    return {
        "mode": "skipped-no-extras",
        "cases": 0,
        "passed": 0,
        "pass_rate": 0.0,
        "avg_rag_score": 0.0,
        # Legacy default: skip green when we cannot measure. --strict turns
        # an unmeasurable run into a failure so CI can never silently lose
        # its quality gate.
        "gate_passed": not strict,
    }


def main() -> int:
    args = parse_args()
    result = run_eval(args)
    Path(args.json_out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("GATE:", "PASS" if result["gate_passed"] else "FAIL")
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

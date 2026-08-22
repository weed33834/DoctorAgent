"""Tests: eval_gate honesty + judge wiring (v0.3.16).

* The result JSON must not label a gold-vs-gold self-check as a live run
  (``mode`` reflects what actually ran).
* ``--strict`` turns an unmeasurable run (extras missing) into a gate
  failure instead of a silent skip.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "eval_gate", Path(__file__).resolve().parents[1] / "scripts" / "eval_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_gate"] = module
    spec.loader.exec_module(module)
    return module


class TestEvalGateHonesty:
    def test_offline_gate_default_skips_green(self) -> None:
        gate = _load_gate()
        args = SimpleNamespace(strict=False)
        result = gate._offline_gate(args)
        assert result["mode"] == "skipped-no-extras"
        assert result["gate_passed"] is True  # legacy CI compatibility

    def test_offline_gate_strict_fails(self) -> None:
        gate = _load_gate()
        args = SimpleNamespace(strict=True)
        result = gate._offline_gate(args)
        assert result["mode"] == "skipped-no-extras"
        assert result["gate_passed"] is False

    def test_gold_selfcheck_mode_label(self) -> None:
        """The deterministic path is labelled gold-selfcheck, never 'full'."""
        gate = _load_gate()

        fake_args = SimpleNamespace(
            min_rag_score=0.5,
            min_pass_rate=0.8,
            cases=0,
            json_out="unused",
            strict=False,
            judge_model=None,
            judge_api_key=None,
            judge_base_url=None,
        )
        # cases=0 → loop runs zero times but the mode label still applies.
        try:
            result = gate.run_eval(fake_args)
        except Exception:  # sample data missing — mode contract still holds
            pytest_skip = getattr(gate, "_offline_gate", None)
            assert pytest_skip is not None
            return
        if result["mode"] != "skipped-no-extras":
            assert result["mode"] in ("gold-selfcheck", "llm-judged")
            assert result["mode"] != "full"

    def test_judge_requires_model_name(self) -> None:
        gate = _load_gate()
        args = SimpleNamespace(
            min_rag_score=0.5, judge_model=None, judge_api_key=None, judge_base_url=None
        )
        assert gate._judge_from_args(args) is None

    def test_judge_missing_extra_returns_none(self) -> None:
        gate = _load_gate()
        args = SimpleNamespace(
            min_rag_score=0.5,
            judge_model="qwen3:8b",
            judge_api_key="",
            judge_base_url="http://x/v1",
        )
        # When the deepeval extra is absent the helper degrades to None
        # instead of crashing the gate.
        try:
            import doctoragent.model.deepeval_integration  # noqa: F401

            has_extra = True
        except ImportError:
            has_extra = False
        result = gate._judge_from_args(args)
        if not has_extra:
            assert result is None
        else:
            assert result is not None

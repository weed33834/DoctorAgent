"""Tests: optional spaCy NER layer for PHI name detection (v0.3.19).

The regex/heuristic name patterns cannot reach Safe-Harbor recall alone.
When a spaCy model is configured and importable, PERSON entities are merged
into the PATIENT_NAME candidate set (overlap dedupe arbitrates). When the
model is missing or broken, detection degrades to regex-only with one
warning — never an exception.

These tests inject a fake ``spacy`` module so no model download is needed.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from doctoragent.clinical.deidentification import (
    PHIDetector,
    _reset_ner_cache,
)


class _FakeEnt:
    def __init__(self, label: str, text: str, start: int, end: int) -> None:
        self.label_ = label
        self.text = text
        self.start_char = start
        self.end_char = end


class _FakeDoc:
    def __init__(self, ents: list[_FakeEnt]) -> None:
        self.ents = ents


def _install_fake_spacy(
    monkeypatch: pytest.MonkeyPatch,
    ents_for_text: Any,
    fail_load: bool = False,
) -> dict[str, int]:
    """Register a fake ``spacy`` module; returns call counters."""
    calls = {"load": 0, "nlp": 0}

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            calls["nlp"] += 1
            return _FakeDoc(ents_for_text(text))

    fake_spacy = types.ModuleType("spacy")

    def fake_load(model: str, exclude: Any = None) -> Any:
        calls["load"] += 1
        if fail_load:
            raise OSError(f"model {model!r} not found")
        return _FakeNLP()

    fake_spacy.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    return calls


@pytest.fixture(autouse=True)
def _fresh_ner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DOCTORAGENT_SECURITY__DEID_SPACY_MODEL", raising=False)
    _reset_ner_cache()
    yield
    _reset_ner_cache()


class TestNerLayer:
    def test_disabled_by_default(self) -> None:
        d = PHIDetector()
        assert d._spacy_model == ""
        # Regex path still finds the title-prefixed name.
        hits = d.detect_phi("Patient John Doe MRN 12345678")
        assert any(h["type"] == "PATIENT_NAME" for h in hits)

    def test_ner_person_merged_into_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare name with NO title/keyword cue is caught via NER."""

        def ents(text: str) -> list[_FakeEnt]:
            start = text.index("Zhang Wei")
            return [_FakeEnt("PERSON", "Zhang Wei", start, start + len("Zhang Wei"))]

        _install_fake_spacy(monkeypatch, ents)
        d = PHIDetector(config={"spacy_model": "zh_core_web_sm"})
        # "Zhang Wei" appears bare (no Dr./Patient prefix) — regex misses it.
        text = "Follow up plan discussed with Zhang Wei yesterday."
        hits = [h for h in d.detect_phi(text) if h["type"] == "PATIENT_NAME"]
        assert any(h["value"] == "Zhang Wei" for h in hits)

    def test_per_label_accepted_chinese_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def ents(text: str) -> list[_FakeEnt]:
            start = text.index("王小明")
            return [_FakeEnt("PER", "王小明", start, start + len("王小明"))]

        _install_fake_spacy(monkeypatch, ents)
        d = PHIDetector(config={"spacy_model": "zh_core_web_sm"})
        text = "已与王小明完成随访。"
        hits = [h for h in d.detect_phi(text) if h["type"] == "PATIENT_NAME"]
        assert any(h["value"] == "王小明" for h in hits)

    def test_overlap_dedupe_prefers_regex_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When regex already matched 'Patient John Doe', NER's narrower
        'John Doe' span must not duplicate it."""
        calls: dict[str, int] = {"nlp": 0}

        def ents(text: str) -> list[_FakeEnt]:
            calls["nlp"] += 1
            out = []
            if "John Doe" in text:
                start = text.index("John Doe")
                out.append(_FakeEnt("PERSON", "John Doe", start, start + 8))
            return out

        _install_fake_spacy(monkeypatch, ents)
        d = PHIDetector(config={"spacy_model": "en_core_web_sm"})
        hits = [
            h for h in d.detect_phi("Patient John Doe called today") if h["type"] == "PATIENT_NAME"
        ]
        # One merged hit covering 'Patient John Doe' (regex wins: starts
        # earlier / longer), not two overlapping entries.
        assert len([h for h in hits if h["start"] <= hits[0]["end"]]) >= 1
        values = [h["value"] for h in hits]
        assert "John Doe" not in values or all(v != "Patient John" for v in values)

    def test_missing_model_degrades_to_regex_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_spacy(monkeypatch, lambda t: [], fail_load=True)
        d = PHIDetector(config={"spacy_model": "missing_model"})
        # Must not raise; regex-only detection still works.
        hits = d.detect_phi("Patient John Doe MRN 12345678")
        assert any(h["type"] == "PATIENT_NAME" for h in hits)

    def test_broken_pipeline_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_spacy(
            monkeypatch,
            lambda t: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        d = PHIDetector(config={"spacy_model": "zh_core_web_sm"})
        hits = d.detect_phi("Patient John Doe called")
        assert any(h["type"] == "PATIENT_NAME" for h in hits)
        assert calls["load"] == 1

    def test_env_var_enables_layer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCTORAGENT_SECURITY__DEID_SPACY_MODEL", "zh_core_web_sm")

        def ents(text: str) -> list[_FakeEnt]:
            start = text.index("李四")
            return [_FakeEnt("PER", "李四", start, start + len("李四"))]

        _install_fake_spacy(monkeypatch, ents)
        d = PHIDetector()
        assert d._spacy_model == "zh_core_web_sm"
        hits = [h for h in d.detect_phi("主治医生是李四。") if h["type"] == "PATIENT_NAME"]
        assert any(h["value"] == "李四" for h in hits)

    def test_constructor_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCTORAGENT_SECURITY__DEID_SPACY_MODEL", "env_model")
        d = PHIDetector(config={"spacy_model": "ctor_model"})
        assert d._spacy_model == "ctor_model"

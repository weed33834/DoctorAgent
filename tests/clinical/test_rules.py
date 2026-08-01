"""Tests for the deterministic clinical rule engine.

The engine is pure logic + dependency injection, so DDI tests reuse the
``_FakeRxNorm`` / ``_FakeOpenFDA`` in-memory stand-ins introduced in
``test_knowledge.py`` — no real network access, fully deterministic.
"""

from __future__ import annotations

from doctoragent.clinical.safety import (
    ClinicalRuleEngine,
    ClinicalRuleResult,
    ClinicalRuleType,
    get_allergy_cross_reactivity,
)

# ---------------------------------------------------------------------------
# In-memory knowledge-client doubles (mirrors test_knowledge.py)
# ---------------------------------------------------------------------------


class _FakeRxNorm:
    def __init__(
        self,
        mapping: dict[str, str | None] | None = None,
        related: dict[str, list[dict]] | None = None,
    ) -> None:
        self.mapping = mapping or {}
        self.related = related or {}
        self.closed = False

    async def normalize_drug_name(self, name: str) -> str | None:
        return self.mapping.get(name)

    async def get_related_drugs(self, rxcui: str, relation: str = "IN") -> list[dict]:
        return self.related.get(rxcui, [])

    async def close(self) -> None:
        self.closed = True


class _FakeOpenFDA:
    def __init__(self, labels: dict[str, str]) -> None:
        self.labels = labels
        self.closed = False

    async def get_interactions_section(self, drug_name: str) -> str:
        return self.labels.get(drug_name, "")

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# evaluate_vitals
# ---------------------------------------------------------------------------


def test_evaluate_vitals_critical_heart_rate_is_critical() -> None:
    engine = ClinicalRuleEngine()
    results = engine.evaluate_vitals({"heart_rate": 35})
    assert len(results) == 1
    r = results[0]
    assert r.rule_type is ClinicalRuleType.VITALS
    assert r.severity == "critical"
    assert r.affected_resources == ["heart_rate"]
    assert r.source == "rule_engine"
    assert "危急值" in r.finding


def test_evaluate_vitals_critical_high_blood_pressure() -> None:
    engine = ClinicalRuleEngine()
    results = engine.evaluate_vitals({"systolic_bp": 200})
    assert len(results) == 1
    assert results[0].severity == "critical"
    assert "高于危急值" in results[0].finding


def test_evaluate_vitals_normal_returns_empty() -> None:
    engine = ClinicalRuleEngine()
    assert engine.evaluate_vitals({"heart_rate": 75, "systolic_bp": 110}) == []


def test_evaluate_vitals_warning_low_heart_rate() -> None:
    # 50 is below min (60) but above critical_low (40) → warning, not critical.
    engine = ClinicalRuleEngine()
    results = engine.evaluate_vitals({"heart_rate": 50})
    assert len(results) == 1
    assert results[0].severity == "warning"
    assert "低于参考范围" in results[0].finding


# ---------------------------------------------------------------------------
# evaluate_labs
# ---------------------------------------------------------------------------


def test_evaluate_labs_critical_sodium() -> None:
    engine = ClinicalRuleEngine()
    results = engine.evaluate_labs(
        [{"test": "sodium", "value": 160, "unit": "mmol/L", "id": "Observation/123"}]
    )
    assert len(results) == 1
    r = results[0]
    assert r.rule_type is ClinicalRuleType.LABS
    assert r.severity == "critical"
    assert r.affected_resources == ["Observation/123"]


def test_evaluate_labs_normal_returns_empty() -> None:
    engine = ClinicalRuleEngine()
    assert engine.evaluate_labs([{"test": "sodium", "value": 140}]) == []


def test_evaluate_labs_warning_potassium() -> None:
    engine = ClinicalRuleEngine()
    results = engine.evaluate_labs([{"test": "potassium", "value": 3.0}])
    assert len(results) == 1
    assert results[0].severity == "warning"


def test_evaluate_labs_skips_invalid_entries() -> None:
    engine = ClinicalRuleEngine()
    assert engine.evaluate_labs([{"test": "sodium"}, {"value": 140}]) == []


# ---------------------------------------------------------------------------
# evaluate_medications
# ---------------------------------------------------------------------------


async def test_evaluate_medications_ddi_with_clients_detected() -> None:
    rxnorm = _FakeRxNorm({"warfarin": "11289", "ibuprofen": "5640"})
    openfda = _FakeOpenFDA(
        {
            "warfarin": "Contraindicated with ibuprofen due to bleeding risk.",
            "ibuprofen": "Use with caution.",
        }
    )
    engine = ClinicalRuleEngine(rxnorm_client=rxnorm, openfda_client=openfda)
    results = await engine.evaluate_medications(["warfarin", "ibuprofen"])
    ddi = [r for r in results if r.rule_type is ClinicalRuleType.DRUG_INTERACTION]
    assert len(ddi) == 1
    # 本地 DDI 知识库命中（warfarin+ibuprofen=major）优先于 openFDA 标签匹配；
    # 引擎将 DDI ``major`` 映射为 ``critical`` 严重度。
    assert ddi[0].severity == "critical"
    assert set(ddi[0].affected_resources) == {"warfarin", "ibuprofen"}
    # Injected clients stay owned by the test.
    assert not rxnorm.closed
    assert not openfda.closed


async def test_evaluate_medications_no_clients_skips_ddi() -> None:
    # No clients injected → DDI is skipped even for an interacting pair; only
    # locally-decidable rules run (none apply here).
    engine = ClinicalRuleEngine()
    results = await engine.evaluate_medications(["warfarin", "ibuprofen"])
    assert results == []


async def test_evaluate_medications_allergy_cross_reactivity_blocks() -> None:
    engine = ClinicalRuleEngine()
    results = await engine.evaluate_medications(
        ["amoxicillin"], allergies=["penicillin"]
    )
    assert len(results) == 1
    r = results[0]
    assert r.rule_type is ClinicalRuleType.ALLERGY
    assert r.severity == "contraindicated"
    assert r.affected_resources == ["amoxicillin"]


async def test_evaluate_medications_no_clients_allergy_still_runs() -> None:
    # Allergy check is locally decidable — works without any clients.
    engine = ClinicalRuleEngine()
    results = await engine.evaluate_medications(["aspirin"], allergies=["aspirin"])
    assert len(results) == 1
    assert results[0].severity == "contraindicated"


async def test_evaluate_medications_duplicate_therapy_warning() -> None:
    engine = ClinicalRuleEngine()
    results = await engine.evaluate_medications(
        ["Acetaminophen 500mg", "Acetaminophen 325mg"]
    )
    dup = [r for r in results if r.rule_type is ClinicalRuleType.DUPLICATE_THERAPY]
    assert len(dup) == 1
    assert dup[0].severity == "warning"
    assert "重复治疗" in dup[0].finding


async def test_evaluate_medications_no_duplicates_returns_empty() -> None:
    engine = ClinicalRuleEngine()
    results = await engine.evaluate_medications(["aspirin", "ibuprofen"])
    # Different base names, no allergy, no clients → no findings.
    assert results == []


async def test_evaluate_medications_rxnorm_brand_generic_duplicate() -> None:
    """RxNorm equivalence catches brand↔generic duplicates the local
    name-based detector misses (Tylenol vs Acetaminophen)."""
    # Both Tylenol and Acetaminophen resolve to RxCUI 161, whose IN
    # ingredient is "Acetaminophen" — they must be flagged as duplicates.
    rxnorm = _FakeRxNorm(
        mapping={"Tylenol": "161", "Acetaminophen": "161"},
        related={"161": [{"name": "Acetaminophen", "rxcui": "161", "tty": "IN"}]},
    )
    engine = ClinicalRuleEngine(rxnorm_client=rxnorm)
    results = await engine.evaluate_medications(["Tylenol", "Acetaminophen"])
    dup = [r for r in results if r.rule_type is ClinicalRuleType.DUPLICATE_THERAPY]
    assert len(dup) == 1
    assert "RxNorm 等价识别" in dup[0].finding
    assert "Acetaminophen" in dup[0].finding


async def test_evaluate_medications_rxnorm_dedupes_with_local_detector() -> None:
    """When the local detector already flagged a pair, RxNorm must not
    emit a second finding for the same pair (de-duplication)."""
    # Same base name AND same RxCUI — local detector fires first, RxNorm
    # detector skips the duplicate.
    rxnorm = _FakeRxNorm(
        mapping={"Acetaminophen 500mg": "161", "Acetaminophen 325mg": "161"},
        related={"161": [{"name": "Acetaminophen", "rxcui": "161", "tty": "IN"}]},
    )
    engine = ClinicalRuleEngine(rxnorm_client=rxnorm)
    results = await engine.evaluate_medications(
        ["Acetaminophen 500mg", "Acetaminophen 325mg"]
    )
    dup = [r for r in results if r.rule_type is ClinicalRuleType.DUPLICATE_THERAPY]
    # Only ONE finding — the local detector's. RxNorm skipped.
    assert len(dup) == 1
    assert "RxNorm 等价识别" not in dup[0].finding


async def test_evaluate_medications_rxnorm_failure_skips_silently() -> None:
    """RxNorm network/parse failures never break the safety floor."""

    class _BrokenRxNorm(_FakeRxNorm):
        async def normalize_drug_name(self, name: str) -> str | None:
            raise RuntimeError("network down")

    # The local detector still fires for the exact-name duplicate even
    # though RxNorm is broken.
    engine = ClinicalRuleEngine(rxnorm_client=_BrokenRxNorm())
    results = await engine.evaluate_medications(
        ["Acetaminophen 500mg", "Acetaminophen 325mg"]
    )
    dup = [r for r in results if r.rule_type is ClinicalRuleType.DUPLICATE_THERAPY]
    assert len(dup) == 1


# ---------------------------------------------------------------------------
# evaluate_all
# ---------------------------------------------------------------------------


async def test_evaluate_all_aggregates_findings() -> None:
    rxnorm = _FakeRxNorm({"warfarin": "1", "ibuprofen": "2"})
    openfda = _FakeOpenFDA(
        {"warfarin": "Contraindicated with ibuprofen.", "ibuprofen": ""}
    )
    engine = ClinicalRuleEngine(rxnorm_client=rxnorm, openfda_client=openfda)
    context = {
        "vitals": {"heart_rate": 35},
        "labs": [{"test": "sodium", "value": 160}],
        "medications": ["warfarin", "ibuprofen"],
        "allergies": ["penicillin"],
    }
    results = await engine.evaluate_all(context)
    rule_types = {r.rule_type for r in results}
    assert ClinicalRuleType.VITALS in rule_types
    assert ClinicalRuleType.LABS in rule_types
    assert ClinicalRuleType.DRUG_INTERACTION in rule_types
    # At least one critical/contraindicated finding is present.
    assert any(r.severity in ("critical", "contraindicated") for r in results)


# ---------------------------------------------------------------------------
# get_blocking_findings
# ---------------------------------------------------------------------------


def test_get_blocking_findings_filters_by_severity() -> None:
    results = [
        ClinicalRuleResult(
            rule_type=ClinicalRuleType.VITALS,
            severity="warning",
            finding="low hr",
            recommendation="复查",
        ),
        ClinicalRuleResult(
            rule_type=ClinicalRuleType.VITALS,
            severity="critical",
            finding="critical hr",
            recommendation="紧急干预",
        ),
        ClinicalRuleResult(
            rule_type=ClinicalRuleType.ALLERGY,
            severity="contraindicated",
            finding="penicillin allergy",
            recommendation="避免使用",
        ),
        ClinicalRuleResult(
            rule_type=ClinicalRuleType.DUPLICATE_THERAPY,
            severity="info",
            finding="dup",
            recommendation="复核",
        ),
    ]
    blocking = ClinicalRuleEngine.get_blocking_findings(results)
    assert len(blocking) == 2
    assert {r.severity for r in blocking} == {"critical", "contraindicated"}


def test_get_blocking_findings_empty_when_only_advisory() -> None:
    results = [
        ClinicalRuleResult(
            rule_type=ClinicalRuleType.DUPLICATE_THERAPY,
            severity="warning",
            finding="dup",
            recommendation="复核",
        )
    ]
    assert ClinicalRuleEngine.get_blocking_findings(results) == []


# ---------------------------------------------------------------------------
# get_allergy_cross_reactivity
# ---------------------------------------------------------------------------


def test_allergy_cross_reactivity_penicillin_class() -> None:
    # amoxicillin contains "cillin" → penicillin-class cross-reaction.
    hits = get_allergy_cross_reactivity("amoxicillin", ["penicillin"])
    assert hits == ["penicillin"]


def test_allergy_cross_reactivity_direct_match() -> None:
    assert get_allergy_cross_reactivity("aspirin", ["aspirin"]) == ["aspirin"]


def test_allergy_cross_reactivity_no_match() -> None:
    assert get_allergy_cross_reactivity("ibuprofen", ["penicillin"]) == []


def test_allergy_cross_reactivity_multiple_allergies() -> None:
    hits = get_allergy_cross_reactivity(
        "sulfamethoxazole", ["penicillin", "sulfa", "aspirin"]
    )
    assert hits == ["sulfa"]


def test_allergy_cross_reactivity_empty_inputs() -> None:
    assert get_allergy_cross_reactivity("", ["penicillin"]) == []
    assert get_allergy_cross_reactivity("amoxicillin", []) == []

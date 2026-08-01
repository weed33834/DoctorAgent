"""Tests for the synthetic FHIR R4 clinical fixtures and the fixture loader.

The fixtures are validated against the HL7 ``fhir.resources`` pydantic
models (skipped when that optional dependency is absent) and then fed
through the deterministic :class:`ClinicalRuleEngine` to confirm each
scenario exercises the rule it is meant to exercise.
"""

from __future__ import annotations

import pytest

from doctoragent.clinical.safety import (
    ClinicalRuleEngine,
    ClinicalRuleType,
    get_allergy_cross_reactivity,
)
from tests.fixtures.clinical import (
    fhir_bundle_to_patient_context,
    load_clinical_fixture,
    synthetic_scenarios,
)

# Skip the entire module when the optional ``fhir.resources`` extra is absent.
pytest.importorskip("fhir.resources")

# The fixtures are FHIR R4-shaped (medicationCodeableConcept, Encounter.period,
# single-Coding Encounter.class, …). The default ``fhir.resources`` models
# resolve to R5 which renamed several of those fields, so the fixtures are
# validated against the R4B submodule that ships with the same package.
from fhir.resources.R4B import get_fhir_model_class as _get_r4b_model  # noqa: E402


def _parse_r4b(resource: dict) -> object:
    """Validate an R4-shaped resource against the fhir.resources R4B models."""
    rtype = resource.get("resourceType")
    if not rtype:
        raise ValueError("resource is missing resourceType")
    model = _get_r4b_model(rtype)
    return model.model_validate(resource)


_SCENARIO_FILES = [
    "patient_safe",
    "patient_drug_interaction",
    "patient_allergy_alert",
    "patient_critical_vitals",
    "patient_critical_labs",
    "patient_duplicate_therapy",
]


# ---------------------------------------------------------------------------
# Fixture structure
# ---------------------------------------------------------------------------


def test_all_fixtures_load_and_parse_with_fhir_resources() -> None:
    for name in _SCENARIO_FILES:
        bundle = load_clinical_fixture(name)
        assert bundle["resourceType"] == "Bundle", name
        assert bundle["type"] == "collection", name
        for entry in bundle["entry"]:
            # model_validate raises pydantic.ValidationError on schema drift.
            _parse_r4b(entry["resource"])


def test_each_fixture_has_patient_medication_and_allergy() -> None:
    for name in _SCENARIO_FILES:
        bundle = load_clinical_fixture(name)
        types = {e["resource"]["resourceType"] for e in bundle["entry"]}
        assert "Patient" in types, f"{name} missing Patient"
        assert "MedicationRequest" in types, f"{name} missing MedicationRequest"
        assert "AllergyIntolerance" in types, f"{name} missing AllergyIntolerance"


def test_synthetic_scenarios_returns_all_six() -> None:
    scenarios = synthetic_scenarios()
    assert set(scenarios) == {
        "safe",
        "drug_interaction",
        "allergy_alert",
        "critical_vitals",
        "critical_labs",
        "duplicate_therapy",
    }


# ---------------------------------------------------------------------------
# Allergy cross-reactivity
# ---------------------------------------------------------------------------


def test_allergy_alert_cross_reactivity_hits() -> None:
    ctx = fhir_bundle_to_patient_context(load_clinical_fixture("patient_allergy_alert"))
    assert any(a.lower() == "penicillin" for a in ctx["allergies"])
    assert any("amoxicillin" in m.lower() for m in ctx["medications"])

    hits_found = False
    for med in ctx["medications"]:
        hits = get_allergy_cross_reactivity(med, ctx["allergies"])
        if "amoxicillin" in med.lower():
            assert hits, f"expected cross-reactivity hit for {med!r}"
            assert any(h.lower() == "penicillin" for h in hits)
            hits_found = True
    assert hits_found, "no amoxicillin medication was present"


# ---------------------------------------------------------------------------
# Critical vitals
# ---------------------------------------------------------------------------


def test_critical_vitals_produces_critical_finding() -> None:
    ctx = fhir_bundle_to_patient_context(load_clinical_fixture("patient_critical_vitals"))
    assert ctx["vitals"], "expected non-empty vitals"
    engine = ClinicalRuleEngine()
    results = engine.evaluate_vitals(ctx["vitals"])
    assert results, "expected at least one vital finding"
    assert any(r.severity == "critical" for r in results)
    # Heart rate 35 + systolic 200 + diastolic 120 → multiple critical flags.
    assert len(results) >= 2


# ---------------------------------------------------------------------------
# Critical labs
# ---------------------------------------------------------------------------


def test_critical_labs_produces_critical_finding() -> None:
    ctx = fhir_bundle_to_patient_context(load_clinical_fixture("patient_critical_labs"))
    assert ctx["labs"], "expected non-empty labs"
    engine = ClinicalRuleEngine()
    results = engine.evaluate_labs(ctx["labs"])
    assert results, "expected at least one lab finding"
    assert any(r.severity == "critical" for r in results)
    # Potassium 6.8 mmol/L crosses the 6.5 critical_high threshold.
    critical = [r for r in results if r.severity == "critical"]
    assert any(
        "potassium" in r.finding.lower()
        or "potassium" in "".join(r.affected_resources).lower()
        for r in critical
    )


# ---------------------------------------------------------------------------
# Duplicate therapy
# ---------------------------------------------------------------------------


async def test_duplicate_therapy_finding() -> None:
    ctx = fhir_bundle_to_patient_context(load_clinical_fixture("patient_duplicate_therapy"))
    assert len(ctx["medications"]) >= 2
    assert any("acetaminophen" in m.lower() for m in ctx["medications"])
    engine = ClinicalRuleEngine()
    results = await engine.evaluate_medications(ctx["medications"], ctx["allergies"])
    dups = [r for r in results if r.rule_type is ClinicalRuleType.DUPLICATE_THERAPY]
    assert dups, "expected a duplicate-therapy finding"
    assert dups[0].severity == "warning"


# ---------------------------------------------------------------------------
# Drug interaction (graceful without knowledge clients)
# ---------------------------------------------------------------------------


async def test_drug_interaction_runs_without_clients() -> None:
    ctx = fhir_bundle_to_patient_context(load_clinical_fixture("patient_drug_interaction"))
    assert len(ctx["medications"]) >= 2
    # ≥2 conditions in the raw bundle.
    bundle = load_clinical_fixture("patient_drug_interaction")
    conditions = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Condition"
    ]
    assert len(conditions) >= 2

    engine = ClinicalRuleEngine()  # no rxnorm/openfda clients injected
    results = await engine.evaluate_medications(ctx["medications"], ctx["allergies"])
    # Without knowledge clients DDI is skipped; only allergy + duplicate
    # types may appear (here: none, since no allergy/duplicate).
    for r in results:
        assert r.rule_type in (ClinicalRuleType.ALLERGY, ClinicalRuleType.DUPLICATE_THERAPY)


# ---------------------------------------------------------------------------
# Round-trip on every scenario
# ---------------------------------------------------------------------------


def test_fhir_bundle_to_patient_context_round_trips_every_scenario() -> None:
    required_keys = {"patient_id", "medications", "allergies", "vitals", "labs"}
    for label, bundle in synthetic_scenarios().items():
        ctx = fhir_bundle_to_patient_context(bundle)
        assert required_keys.issubset(ctx), f"{label} missing keys: {required_keys - set(ctx)}"
        assert ctx["patient_id"], f"{label} missing patient_id"
        assert isinstance(ctx["medications"], list)
        assert isinstance(ctx["allergies"], list)
        assert isinstance(ctx["vitals"], dict)
        assert isinstance(ctx["labs"], list)

        # When the bundle has vital/lab Observations, the corresponding
        # context field must be non-empty.
        if _has_observation_category(bundle, "vital-signs"):
            assert ctx["vitals"], f"{label} has vital-signs observations but empty vitals"
        if _has_observation_category(bundle, "laboratory"):
            assert ctx["labs"], f"{label} has laboratory observations but empty labs"


def _has_observation_category(bundle: dict, category_code: str) -> bool:
    for entry in bundle.get("entry") or []:
        res = entry.get("resource") or {}
        if res.get("resourceType") != "Observation":
            continue
        for cat in res.get("category") or []:
            for coding in cat.get("coding") or []:
                if coding.get("code") == category_code:
                    return True
    return False

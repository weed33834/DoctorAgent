"""Synthetic FHIR R4 clinical fixture loader.

Provides six ready-made FHIR ``Bundle`` fixtures representing distinct
synthetic patient scenarios, plus a converter that flattens a Bundle into
the ``patient_context`` dict shape consumed by
:class:`doctoragent.clinical.safety.rules.ClinicalRuleEngine.evaluate_all` and
:class:`doctoragent.clinical.agents.orchestrator.ClinicalOrchestrator.analyze`.

All names, MRNs and clinical values are FAKE — these fixtures exist solely
so clinical tests and demos can run without contacting a real FHIR server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "fhir_bundle_to_patient_context",
    "load_clinical_fixture",
    "synthetic_scenarios",
]

_FIXTURE_DIR = Path(__file__).parent

# Short label → fixture file stem (without ``.json``).
_SCENARIO_FILES: dict[str, str] = {
    "safe": "patient_safe",
    "drug_interaction": "patient_drug_interaction",
    "allergy_alert": "patient_allergy_alert",
    "critical_vitals": "patient_critical_vitals",
    "critical_labs": "patient_critical_labs",
    "duplicate_therapy": "patient_duplicate_therapy",
}

# Matches a digit anywhere — used to detect that a medication string already
# carries a dosage token, so we don't append a second one.
_HAS_DIGIT_RE = re.compile(r"\d")


def load_clinical_fixture(name: str) -> dict[str, Any]:
    """Load a FHIR Bundle fixture by name (without ``.json``)."""
    path = _FIXTURE_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Clinical fixture not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Clinical fixture {name!r} is not a JSON object")
    return data


def fhir_bundle_to_patient_context(bundle: dict[str, Any]) -> dict[str, Any]:
    """Convert a FHIR Bundle of patient resources into the ``patient_context``
    dict shape used by ``ClinicalRuleEngine.evaluate_all`` and
    ``ClinicalOrchestrator.analyze``.

    Returns a dict with ``patient_id`` (str), ``medications`` (list[str]),
    ``allergies`` (list[str]), ``vitals`` (dict[str, float]) and ``labs``
    (list[dict]).
    """
    entries = bundle.get("entry") or []
    resources: list[dict[str, Any]] = [
        e["resource"]
        for e in entries
        if isinstance(e, dict) and isinstance(e.get("resource"), dict)
    ]

    patient: dict[str, Any] | None = None
    medications: list[str] = []
    allergies: list[str] = []
    vitals: dict[str, float] = {}
    labs: list[dict[str, Any]] = []

    for res in resources:
        rtype = res.get("resourceType")
        if rtype == "Patient":
            patient = res
        elif rtype == "MedicationRequest":
            text = _medication_to_text(res)
            if text:
                medications.append(text)
        elif rtype == "AllergyIntolerance":
            if _is_active_allergy(res):
                code = res.get("code") or {}
                text = code.get("text") or ""
                if text:
                    allergies.append(text)
        elif rtype == "Observation":
            _observation_to_context(res, vitals, labs)

    patient_id = ""
    gender = "male"
    if patient:
        patient_id = patient.get("id") or ""
        if not patient_id:
            idents = patient.get("identifier") or []
            if idents and isinstance(idents[0], dict):
                patient_id = idents[0].get("value", "") or ""
        gender = patient.get("gender") or "male"

    # Lab evaluation resolves sex-specific reference ranges from the patient's
    # gender, so stamp it on every lab entry (callers may override).
    for lab in labs:
        lab.setdefault("gender", gender)

    return {
        "patient_id": patient_id,
        "medications": medications,
        "allergies": allergies,
        "vitals": vitals,
        "labs": labs,
    }


def synthetic_scenarios() -> dict[str, dict[str, Any]]:
    """Return all named synthetic scenarios, keyed by short label.

    Labels: ``safe``, ``drug_interaction``, ``allergy_alert``,
    ``critical_vitals``, ``critical_labs``, ``duplicate_therapy``.
    """
    return {label: load_clinical_fixture(stem) for label, stem in _SCENARIO_FILES.items()}


# ── helpers ────────────────────────────────────────────────────────────────


def _is_active_allergy(res: dict[str, Any]) -> bool:
    """True if the AllergyIntolerance has an active clinical status."""
    clinical = res.get("clinicalStatus") or {}
    for coding in clinical.get("coding") or []:
        if coding.get("code") == "active":
            return True
    return False


def _medication_to_text(res: dict[str, Any]) -> str:
    """Build a medication string carrying dosage so the duplicate-therapy
    detector can operate on strings like ``"Acetaminophen 500mg PO"``.

    Prefers ``medicationCodeableConcept.text`` (which the fixtures populate
    with the full description); falls back to ``medicationReference.display``.
    When the resolved text lacks any digit (no dosage) we append a dosage
    suffix derived from ``dosageInstruction``.
    """
    codeable = res.get("medicationCodeableConcept") or {}
    text = codeable.get("text") or ""
    if not text:
        ref = res.get("medicationReference") or {}
        text = ref.get("display") or ""
    if text and not _HAS_DIGIT_RE.search(text):
        dosage = _extract_dosage_text(res.get("dosageInstruction") or [])
        if dosage:
            text = f"{text} {dosage}".strip()
    return text


def _extract_dosage_text(instructions: list[Any]) -> str:
    """Build a compact ``"500mg PO"`` dosage suffix from dosageInstruction."""
    if not instructions or not isinstance(instructions[0], dict):
        return ""
    inst = instructions[0]
    parts: list[str] = []
    for dr in inst.get("doseAndRate") or []:
        if not isinstance(dr, dict):
            continue
        dq = dr.get("doseQuantity") or {}
        value = dq.get("value")
        unit = dq.get("unit")
        if value is not None:
            parts.append(f"{value}{unit}" if unit else str(value))
    route = inst.get("route") or {}
    route_text = route.get("text") or ""
    if not route_text:
        for coding in route.get("coding") or []:
            if coding.get("code"):
                route_text = coding["code"]
                break
    if route_text:
        parts.append(route_text)
    return " ".join(parts)


def _observation_to_context(
    res: dict[str, Any], vitals: dict[str, float], labs: list[dict[str, Any]]
) -> None:
    """Route an Observation into the ``vitals`` dict or ``labs`` list."""
    value_qty = res.get("valueQuantity") or {}
    raw_value = value_qty.get("value")
    if raw_value is None:
        return
    categories = _observation_categories(res)
    code = res.get("code") or {}
    coding_code = ""
    for coding in code.get("coding") or []:
        if coding.get("code"):
            coding_code = coding["code"]
            break
    code_text = code.get("text") or ""

    if "vital-signs" in categories:
        key = coding_code or _snake_case(code_text)
        if not key:
            return
        try:
            vitals[key] = float(raw_value)
        except (TypeError, ValueError):
            pass
    elif "laboratory" in categories:
        test = coding_code or _snake_case(code_text) or code_text
        if not test:
            return
        lab: dict[str, Any] = {"test": test, "value": raw_value}
        unit = value_qty.get("unit")
        if unit:
            lab["unit"] = unit
        rid = res.get("id")
        if rid:
            lab["id"] = rid
        labs.append(lab)


def _observation_categories(res: dict[str, Any]) -> set[str]:
    cats: set[str] = set()
    for cat in res.get("category") or []:
        if not isinstance(cat, dict):
            continue
        for coding in cat.get("coding") or []:
            if coding.get("code"):
                cats.add(coding["code"])
    return cats


def _snake_case(text: str) -> str:
    """Lowercase + snake-case a human-readable label."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")

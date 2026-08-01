"""FHIR R4 resource → human-readable text conversion.

The output is structured, Chinese-language clinical text suitable for feeding
to an LLM (clinical summarization, decision support prompts). All extractors
are **defensive**: missing fields yield empty strings / ``"未知"`` rather than
raising. This module does NOT depend on ``fhir.resources``; it works directly
on the JSON-ish ``dict`` wire format that FHIR servers return.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

_GENDER_CN = {"male": "男", "female": "女", "other": "其他", "unknown": "未知"}


# --------------------------------------------------------------------------- #
# Small defensive extraction primitives
# --------------------------------------------------------------------------- #
def _first_coding(codable: Any) -> dict[str, Any]:
    """Return the first coding dict from a CodeableConcept, or ``{}``."""
    if not isinstance(codable, dict):
        return {}
    codings = codable.get("coding")
    if isinstance(codings, list) and codings:
        first = codings[0]
        return first if isinstance(first, dict) else {}
    return {}


def _coding_display(codable: Any) -> str:
    """Best-effort display string for a CodeableConcept (or bare Coding).

    Falls back to the ``code`` value when no display/text is present, so the
    caller always gets *something* identifiable. Use
    :func:`_coding_display_strict` when you need the display without the code
    fallback (e.g. to avoid ``"I10(I10)"`` duplication).
    """
    strict = _coding_display_strict(codable)
    if strict:
        return strict
    # Fallback to the code value (from a CodeableConcept or bare Coding).
    coding = _first_coding(codable) if isinstance(codable, dict) else {}
    code = coding.get("code")
    if isinstance(code, str) and code.strip():
        return code.strip()
    if isinstance(codable, dict):
        top_code = codable.get("code")
        if isinstance(top_code, str) and top_code.strip():
            return top_code.strip()
    return ""


def _coding_display_strict(codable: Any) -> str:
    """Display string for a CodeableConcept / Coding WITHOUT code fallback.

    Returns ``""`` when only a code is available. Used by callers that
    separately render the code (e.g. ``"2型糖尿病(E11.9)"``) to avoid emitting
    the code twice.
    """
    if not isinstance(codable, dict):
        return ""
    text = codable.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    coding = _first_coding(codable)
    display = coding.get("display")
    if isinstance(display, str) and display.strip():
        return display.strip()
    # Bare Coding (e.g. Encounter.class is a Coding, not CodeableConcept):
    # display may live at the top level rather than under ``coding``.
    top_display = codable.get("display")
    if isinstance(top_display, str) and top_display.strip():
        return top_display.strip()
    return ""


def _coding_code(codable: Any) -> str:
    """Return the first coding's ``code`` (e.g. ICD-10), or empty string."""
    coding = _first_coding(codable)
    code = coding.get("code")
    return code.strip() if isinstance(code, str) else ""


def coding_display(codeable: Any) -> str:
    """Public display-string extractor for a FHIR CodeableConcept / Coding.

    Thin public alias for :func:`_coding_display_strict` so cross-module
    callers (e.g. the clinical rule engine's medication/allergy normalisers)
    can reuse the canonical ``text`` → ``coding[0].display`` → top-level
    ``display`` walk instead of re-implementing it. Returns ``""`` when no
    display text is available (a code-only concept).
    """
    return _coding_display_strict(codeable)


def extract_bundle_entries(bundle: Any) -> list[dict[str, Any]]:
    """Pull ``entry[].resource`` out of a FHIR Bundle; defensive.

    Returns the list of resource dicts contained in a FHIR Bundle. When
    handed a bare resource dict (some FHIR search endpoints return a single
    resource instead of a Bundle), it is wrapped as a single-element list so
    callers get a uniform ``list[dict]`` shape. Non-dict input or a dict
    without a ``resourceType`` yields ``[]``.

    This is the canonical implementation shared by the FHIR client and the
    CDS Hooks service so the two no longer drift on edge-case handling.
    """
    if not isinstance(bundle, dict):
        return []
    if bundle.get("resourceType") != "Bundle":
        # Some servers return a bare resource on search; wrap defensively.
        return [bundle] if bundle.get("resourceType") else []
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if isinstance(resource, dict):
            out.append(resource)
    return out


def _is_active(clinical_status: Any) -> bool:
    """Heuristic: does a clinicalStatus CodeableConcept indicate 'active'?

    FHIR uses codes like ``active``, ``recurrence``, ``relapse`` for problems
    that are still relevant. We treat anything not in
    ``{resolved, inactive, refuted, entered-in-error}`` as active.
    """
    if not isinstance(clinical_status, dict):
        # Some servers emit a bare list of codings; tolerate.
        return True
    coding = _first_coding(clinical_status)
    code = coding.get("code")
    if not isinstance(code, str):
        return True
    return code.lower() not in {"resolved", "inactive", "refuted", "entered-in-error"}


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dict keys; return ``default`` on any missing/non-dict hop."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _parse_date(value: Any) -> date | None:
    """Parse a FHIR date / dateTime string into :class:`datetime.date`.

    Accepts partial dates (``"1960"`` → ``1960-01-01``) defensively.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    # Strip timezone / fractional seconds to keep strptime happy.
    for sep in ("T", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
    fmts = ("%Y-%m-%d", "%Y-%m", "%Y")
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _age_from_birth_date(birth_date: Any, today: date | None = None) -> int | None:
    """Return age in whole years from a FHIR birthDate string, or ``None``."""
    dob = _parse_date(birth_date)
    if dob is None:
        return None
    today = today or date.today()
    if dob > today:  # defensive: future birthdate
        return None
    age = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        age -= 1
    return age if age >= 0 else 0


# --------------------------------------------------------------------------- #
# Field-level extractors (public, defensive)
# --------------------------------------------------------------------------- #
def patient_summary_line(patient: dict[str, Any]) -> str:
    """Build the one-line patient header: ``患者：男, 65岁``.

    Falls back to ``"未知"`` for missing gender / age.
    """
    if not isinstance(patient, dict):
        return "患者：未知"
    gender_raw = patient.get("gender")
    gender = _GENDER_CN.get(gender_raw, "未知") if isinstance(gender_raw, str) else "未知"
    age = _age_from_birth_date(patient.get("birthDate"))
    age_str = f"{age}岁" if age is not None else "年龄未知"
    return f"患者：{gender}, {age_str}"


def condition_to_text(condition: dict[str, Any]) -> str:
    """``2型糖尿病(E11.9)`` — name + optional ICD code in parentheses.

    Uses the strict display (text / coding.display) so a code-only concept
    renders as ``"(I10)"`` rather than ``"I10(I10)"``. Empty string when no
    usable display/code is present.
    """
    if not isinstance(condition, dict):
        return ""
    display = _coding_display_strict(condition.get("code"))
    code = _coding_code(condition.get("code"))
    if not display and not code:
        return ""
    if display and code:
        return f"{display}({code})"
    if display:
        return display
    return f"({code})"


def medication_to_text(med: dict[str, Any]) -> str:
    """``二甲双胍 500mg bid, active`` — drug + dose + frequency + status.

    Returns empty string if no drug name can be derived.
    """
    if not isinstance(med, dict):
        return ""
    # MedicationRequest.medication[x] can be CodeableConcept or Reference.
    med_field = med.get("medicationCodeableConcept") or med.get("medicationReference")
    name = _coding_display(med_field) if isinstance(med_field, dict) else ""
    if not name and isinstance(med_field, dict):
        # Reference fallback: use the literal reference text if present.
        ref = med_field.get("reference")
        if isinstance(ref, str) and ref:
            name = ref.split("/")[-1]
    if not name:
        return ""

    # dosageInstruction is a list; pull the first element once and reuse it.
    instructions = med.get("dosageInstruction")
    first: dict[str, Any] = {}
    if isinstance(instructions, list) and instructions and isinstance(instructions[0], dict):
        first = instructions[0]

    dose_str = ""
    dose_and_rate = first.get("doseAndRate")
    if isinstance(dose_and_rate, list) and dose_and_rate:
        dr = dose_and_rate[0]
        if isinstance(dr, dict):
            dose_q = dr.get("doseQuantity")
            if isinstance(dose_q, dict):
                val = dose_q.get("value")
                unit = dose_q.get("unit") or dose_q.get("system", "")
                if val is not None:
                    dose_str = f"{val}{unit}" if unit else f"{val}"

    freq_str = ""
    timing = _safe_get(first, "timing", "repeat")
    if isinstance(timing, dict):
        freq = timing.get("frequency")
        period = timing.get("period")
        period_unit = timing.get("periodUnit")
        if freq and period:
            freq_str = f"{freq}次/{period}{period_unit or ''}"
        bounds = timing.get("boundsDuration") or timing.get("boundsPeriod")
        if isinstance(bounds, dict) and not freq_str:
            # e.g. {"value": 30, "unit": "d"} → "30d"
            bv = bounds.get("value")
            bu = bounds.get("unit") or bounds.get("code") or ""
            if bv is not None:
                freq_str = freq_str or f"{bv}{bu}"

    # Free-text dosage instruction is the most reliable signal in practice.
    text_instr = first.get("text")
    if isinstance(text_instr, str) and text_instr.strip() and not freq_str:
        freq_str = text_instr.strip()

    status = med.get("status")
    status_str = f", {status}" if isinstance(status, str) and status else ""

    parts = [name]
    if dose_str:
        parts.append(dose_str)
    if freq_str:
        parts.append(freq_str)
    text = " ".join(parts)
    return f"{text}{status_str}" if text else ""


def allergy_to_text(allergy: dict[str, Any]) -> str:
    """``青霉素(皮疹)`` — allergen + optional reaction manifestation."""
    if not isinstance(allergy, dict):
        return ""
    code = allergy.get("code")
    substance = allergy.get("reaction", [{}])
    if not isinstance(substance, list) or not substance:
        substance = [{}]
    first_reaction = substance[0] if isinstance(substance[0], dict) else {}

    # Allergen: prefer AllergyIntolerance.code, then reaction.substance.
    allergen = _coding_display(code)
    if not allergen:
        allergen = _coding_display(first_reaction.get("substance"))
    if not allergen:
        return ""

    manifestations = first_reaction.get("manifestation")
    manifest_text = ""
    if isinstance(manifestations, list) and manifestations:
        first = manifestations[0]
        manifest_text = _coding_display(first)

    if manifest_text:
        return f"{allergen}({manifest_text})"
    return allergen


def lab_to_text(obs: dict[str, Any]) -> str:
    """``空腹血糖 8.5 mmol/L(↑)`` — name + value + unit + abnormal flag.

    The abnormal flag is derived from ``interpretation`` coding (H/L) or from
    comparison against ``referenceRange.low`` / ``high`` when interpretation is
    absent. Returns empty string for non-quantitative observations without a
    value.
    """
    if not isinstance(obs, dict):
        return ""
    name = _coding_display(obs.get("code"))
    if not name:
        return ""

    value_q = obs.get("valueQuantity")
    value_str = ""
    unit_str = ""
    numeric_value: float | None = None
    if isinstance(value_q, dict):
        val = value_q.get("value")
        unit = value_q.get("unit") or value_q.get("system", "")
        if val is not None:
            try:
                numeric_value = float(val)
                value_str = _format_number(numeric_value)
            except (TypeError, ValueError):
                value_str = str(val)
        if isinstance(unit, str) and unit:
            unit_str = unit

    # Some lab results use valueString instead.
    if not value_str:
        vs = obs.get("valueString")
        if isinstance(vs, str) and vs.strip():
            value_str = vs.strip()

    if not value_str:
        return ""

    flag = _abnormal_flag(obs, numeric_value)

    base = f"{name} {value_str}"
    if unit_str:
        base = f"{base} {unit_str}"
    if flag:
        base = f"{base}({flag})"
    return base


def _format_number(v: float) -> str:
    """Render a float compactly (drop trailing ``.0``)."""
    if v == int(v):
        return str(int(v))
    return repr(v)


def _abnormal_flag(obs: dict[str, Any], numeric_value: float | None) -> str:
    """Return ``↑`` / ``↓`` / ``""`` based on interpretation or reference range."""
    interp = obs.get("interpretation")
    if isinstance(interp, list) and interp:
        code = _first_coding(interp[0] if isinstance(interp[0], dict) else {}).get("code", "")
        if isinstance(code, str):
            cl = code.lower()
            if cl in {"h", "hh", "hu", "high"}:
                return "↑"
            if cl in {"l", "ll", "lu", "low"}:
                return "↓"
            if cl in {"a", "aa", "abnormal"}:
                return "异常"
    # Fallback: compare against referenceRange low/high.
    if numeric_value is None:
        return ""
    ranges = obs.get("referenceRange")
    if isinstance(ranges, list) and ranges:
        first = ranges[0]
        if isinstance(first, dict):
            low = _safe_get(first, "low", "value")
            high = _safe_get(first, "high", "value")
            try:
                if high is not None and numeric_value > float(high):
                    return "↑"
                if low is not None and numeric_value < float(low):
                    return "↓"
            except (TypeError, ValueError):
                return ""
    return ""


def encounter_to_text(encounter: dict[str, Any]) -> str:
    """Brief encounter summary: ``门诊就诊 (2024-03-01)``."""
    if not isinstance(encounter, dict):
        return ""
    cls = _coding_display(encounter.get("class"))
    period = encounter.get("period", {})
    start = period.get("start") if isinstance(period, dict) else None
    date_str = str(start).split("T", 1)[0] if isinstance(start, str) and start else ""
    parts = [p for p in (cls, date_str) if p]
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Top-level: patient_to_text
# --------------------------------------------------------------------------- #
def patient_to_text(
    patient: dict[str, Any],
    conditions: list[dict[str, Any]] | None = None,
    medications: list[dict[str, Any]] | None = None,
    allergies: list[dict[str, Any]] | None = None,
    labs: list[dict[str, Any]] | None = None,
    encounters: list[dict[str, Any]] | None = None,
) -> str:
    """Serialize a patient + related resources into a clinician-readable block.

    Output sections (only emitted when non-empty):

      - ``患者：男, 65岁``
      - ``现患疾病：2型糖尿病(E11.9), 高血压(I10)``
      - ``当前用药：二甲双胍 500mg bid, active``
      - ``过敏：青霉素(皮疹)``
      - ``近期检验：空腹血糖 8.5 mmol/L(↑)``
      - ``就诊记录：门诊就诊 (2024-03-01)``

    Never raises; missing/empty inputs produce a header-only summary.
    """
    if not isinstance(patient, dict):
        patient = {}
    conditions = conditions or []
    medications = medications or []
    allergies = allergies or []
    labs = labs or []
    encounters = encounters or []

    lines: list[str] = [patient_summary_line(patient)]

    active_conditions = [
        condition_to_text(c)
        for c in conditions
        if isinstance(c, dict) and _is_active(c.get("clinicalStatus"))
    ]
    active_conditions = [c for c in active_conditions if c]
    if active_conditions:
        lines.append(f"现患疾病：{', '.join(active_conditions)}")

    med_texts = [medication_to_text(m) for m in medications]
    med_texts = [m for m in med_texts if m]
    if med_texts:
        lines.append(f"当前用药：{'; '.join(med_texts)}")

    allergy_texts = [allergy_to_text(a) for a in allergies]
    allergy_texts = [a for a in allergy_texts if a]
    if allergy_texts:
        lines.append(f"过敏：{', '.join(allergy_texts)}")

    lab_texts = [lab_to_text(lab) for lab in labs]
    lab_texts = [lab for lab in lab_texts if lab]
    if lab_texts:
        lines.append(f"近期检验：{', '.join(lab_texts)}")

    enc_texts = [encounter_to_text(e) for e in encounters]
    enc_texts = [e for e in enc_texts if e]
    if enc_texts:
        lines.append(f"就诊记录：{', '.join(enc_texts)}")

    return "\n".join(lines)


__all__ = [
    "allergy_to_text",
    "coding_display",
    "condition_to_text",
    "encounter_to_text",
    "extract_bundle_entries",
    "lab_to_text",
    "medication_to_text",
    "patient_summary_line",
    "patient_to_text",
]

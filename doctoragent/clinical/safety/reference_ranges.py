"""Clinical reference ranges and abnormality detection.

Deterministic, side-effect-free rule engine that compares patient vitals
and lab values against curated reference ranges. No external dependencies —
pure Python so the logic is easy to unit-test and audit.

Reference ranges are sourced from commonly cited clinical references
(Bates' Guide, Henry's Clinical Diagnosis, ADA guidelines, WHO/ICSH
haematology thresholds). They are *defaults*; production deployments
should override with institution-specific ranges by mutating
:data:`REFERENCE_RANGES` or supplying a config layer.

Gender handling: a small set of analytes (haemoglobin, haematocrit, RBC,
creatinine) have sex-specific reference intervals. The default entry in
:data:`REFERENCE_RANGES` is the male range; female overrides are applied
transparently by :func:`get_reference_range` via :data:`_GENDER_OVERRIDES`.
"""

from __future__ import annotations

from typing import Any

from doctoragent.compat import StrEnum

__all__ = [
    "AbnormalityFlag",
    "REFERENCE_RANGES",
    "evaluate_lab_value",
    "evaluate_vitals",
    "get_abnormality_flag",
    "get_reference_range",
    "get_reference_ranges_info",
    "get_reference_ranges_version",
]


# 参考范围知识库版本
REFERENCE_RANGES_VERSION = "1.1.0"
REFERENCE_RANGES_CHANGELOG = (
    "新增 PTT/LDH/CK/CK-MB/D-dimer/TSH/T3/T4/Lipid panel/BNP/CRP/ESR/"
    "Ferritin/Vitamin D/Phosphate 等检验项"
)


class AbnormalityFlag(StrEnum):
    """Coarse abnormality classification for a single value.

    ``CRITICAL_*`` is reported in preference to ``LOW``/``HIGH`` so a
    value that crosses both the reference and the critical threshold is
    surfaced at the higher acuity.
    """

    NORMAL = "normal"
    LOW = "low"
    HIGH = "high"
    CRITICAL_LOW = "critical_low"
    CRITICAL_HIGH = "critical_high"
    UNKNOWN = "unknown"


# Gender-specific overrides. Top-level key is the test name; the value is
# a mapping ``gender -> {field: value}`` merged into the base range entry.
# The base :data:`REFERENCE_RANGES` entry holds the male range; entries
# here supply the female range (and may tighten/loosen critical bounds).
_GENDER_OVERRIDES: dict[str, dict[str, dict[str, float | None]]] = {
    "hemoglobin": {
        "male": {"min": 130, "max": 175, "critical_low": 70, "critical_high": 200},
        "female": {"min": 115, "max": 150, "critical_low": 60, "critical_high": 180},
    },
    "hematocrit": {
        "male": {"min": 0.40, "max": 0.52, "critical_low": 0.25, "critical_high": 0.60},
        "female": {"min": 0.36, "max": 0.46, "critical_low": 0.20, "critical_high": 0.55},
    },
    "rbc": {
        "male": {"min": 4.3, "max": 5.9, "critical_low": 2.0, "critical_high": 7.0},
        "female": {"min": 3.8, "max": 5.2, "critical_low": 2.0, "critical_high": 6.5},
    },
    "creatinine": {
        "male": {"min": 62, "max": 115, "critical_low": None, "critical_high": 530},
        "female": {"min": 53, "max": 97, "critical_low": None, "critical_high": 442},
    },
    # 性别相关参考范围（基础值为男性范围，此处补充女性范围）
    "esr": {
        "female": {"min": 0, "max": 20},
    },
    "ferritin": {
        "female": {"min": 12, "max": 150},
    },
}


REFERENCE_RANGES: dict[str, dict[str, Any]] = {
    # ── Vital signs ────────────────────────────────────────────────────
    "heart_rate": {
        "min": 60,
        "max": 100,
        "unit": "bpm",
        "critical_low": 40,
        "critical_high": 130,
    },
    "systolic_bp": {
        "min": 90,
        "max": 120,
        "unit": "mmHg",
        "critical_low": 80,
        "critical_high": 180,
    },
    "diastolic_bp": {
        "min": 60,
        "max": 80,
        "unit": "mmHg",
        "critical_low": 50,
        "critical_high": 120,
    },
    "temperature": {
        "min": 36.1,
        "max": 37.2,
        "unit": "C",
        "critical_low": 35.0,
        "critical_high": 39.0,
    },
    "respiratory_rate": {
        "min": 12,
        "max": 20,
        "unit": "rpm",
        "critical_low": 8,
        "critical_high": 25,
    },
    "spo2": {
        "min": 95,
        "max": 100,
        "unit": "%",
        "critical_low": 90,
        # SpO2 is bounded at 100% by definition; the normal upper limit and
        # a critical threshold cannot coincide (a value of 100% is normal,
        # not critical). See issue #13.
        "critical_high": None,
    },
    # ── Metabolic / endocrine ──────────────────────────────────────────
    "glucose_fasting": {
        "min": 3.9,
        "max": 6.1,
        "unit": "mmol/L",
        "critical_low": 2.8,
        "critical_high": 22.0,
    },
    "hba1c": {
        "min": 4.0,
        "max": 5.6,
        "unit": "%",
        "critical_low": None,
        "critical_high": 10.0,
    },
    # ── Haematology ────────────────────────────────────────────────────
    "hemoglobin": {
        "min": 130,
        "max": 175,
        "unit": "g/L",
        "critical_low": 70,
        "critical_high": 200,
    },
    "hematocrit": {
        "min": 0.40,
        "max": 0.52,
        "unit": "L/L",
        "critical_low": 0.25,
        "critical_high": 0.60,
    },
    "rbc": {
        "min": 4.3,
        "max": 5.9,
        "unit": "10^12/L",
        "critical_low": 2.0,
        "critical_high": 7.0,
    },
    "wbc": {
        "min": 4.0,
        "max": 10.0,
        "unit": "10^9/L",
        "critical_low": 2.0,
        "critical_high": 30.0,
    },
    "platelets": {
        "min": 150,
        "max": 400,
        "unit": "10^9/L",
        "critical_low": 50,
        "critical_high": 1000,
    },
    "inr": {
        "min": 0.8,
        "max": 1.2,
        "unit": None,
        "critical_low": None,
        "critical_high": 5.0,
    },
    # ── Electrolytes ───────────────────────────────────────────────────
    "sodium": {
        "min": 135,
        "max": 145,
        "unit": "mmol/L",
        "critical_low": 120,
        "critical_high": 160,
    },
    "potassium": {
        "min": 3.5,
        "max": 5.0,
        "unit": "mmol/L",
        "critical_low": 2.5,
        "critical_high": 6.5,
    },
    "chloride": {
        "min": 98,
        "max": 107,
        "unit": "mmol/L",
        "critical_low": 80,
        "critical_high": 115,
    },
    "calcium": {
        "min": 2.20,
        "max": 2.60,
        "unit": "mmol/L",
        "critical_low": 1.75,
        "critical_high": 3.50,
    },
    "bicarbonate": {
        "min": 22,
        "max": 29,
        "unit": "mmol/L",
        "critical_low": 10,
        "critical_high": 40,
    },
    "magnesium": {
        "min": 0.70,
        "max": 1.00,
        "unit": "mmol/L",
        "critical_low": 0.40,
        "critical_high": 2.00,
    },
    # ── Renal / hepatic ────────────────────────────────────────────────
    "creatinine": {
        "min": 53,
        "max": 106,
        "unit": "umol/L",
        "critical_low": None,
        "critical_high": 530,
    },
    "bun": {
        "min": 2.9,
        "max": 7.1,
        "unit": "mmol/L",
        "critical_low": None,
        "critical_high": 35.7,
    },
    "alt": {
        "min": 7,
        "max": 40,
        "unit": "U/L",
        "critical_low": None,
        "critical_high": 1000,
    },
    "ast": {
        "min": 8,
        "max": 40,
        "unit": "U/L",
        "critical_low": None,
        "critical_high": 1000,
    },
    "bilirubin_total": {
        "min": 2,
        "max": 21,
        "unit": "umol/L",
        "critical_low": None,
        "critical_high": 300,
    },
    "albumin": {
        "min": 35,
        "max": 50,
        "unit": "g/L",
        "critical_low": 20,
        "critical_high": None,
    },
    # ── Cardiac / coagulation ──────────────────────────────────────────
    "troponin_i": {
        "min": 0.0,
        "max": 0.04,
        "unit": "ng/mL",
        "critical_low": None,
        "critical_high": 1.0,
    },
    # ── 凝血功能 ─────────────────────────────────────────────────────
    "ptt": {
        "min": 25.0,
        "max": 35.0,
        "unit": "s",
        "critical_low": 20.0,
        "critical_high": 60.0,
    },
    "pt": {
        "min": 11.0,
        "max": 13.5,
        "unit": "s",
        "critical_low": 9.0,
        "critical_high": 30.0,
    },
    "d_dimer": {
        "min": 0.0,
        "max": 0.5,
        "unit": "mg/L FEU",
        "critical_high": 5.0,
    },
    # ── 心肌标志物 ───────────────────────────────────────────────────
    "ck": {
        "min": 24.0,
        "max": 195.0,
        "unit": "U/L",
        "critical_high": 1000.0,
    },
    "ck_mb": {
        "min": 0.0,
        "max": 6.3,
        "unit": "ng/mL",
        "critical_high": 25.0,
    },
    "bnp": {
        "min": 0.0,
        "max": 100.0,
        "unit": "pg/mL",
        "critical_high": 900.0,
    },
    "nt_probnp": {
        "min": 0.0,
        "max": 125.0,
        "unit": "pg/mL",
        "critical_high": 1800.0,
    },
    # ── 甲状腺功能 ───────────────────────────────────────────────────
    "tsh": {
        "min": 0.4,
        "max": 4.0,
        "unit": "mIU/L",
        "critical_low": 0.1,
        "critical_high": 20.0,
    },
    "ft3": {
        "min": 3.5,
        "max": 6.5,
        "unit": "pmol/L",
    },
    "ft4": {
        "min": 11.5,
        "max": 22.7,
        "unit": "pmol/L",
    },
    # ── 血脂 ─────────────────────────────────────────────────────────
    "ldl": {
        "min": 0.0,
        "max": 3.4,
        "unit": "mmol/L",
        "critical_high": 4.9,
    },
    "hdl": {
        "min": 0.9,
        "max": 2.0,
        "unit": "mmol/L",
        "critical_low": 0.4,
    },
    "triglycerides": {
        "min": 0.0,
        "max": 1.7,
        "unit": "mmol/L",
        "critical_high": 5.6,
    },
    "total_cholesterol": {
        "min": 0.0,
        "max": 5.2,
        "unit": "mmol/L",
        "critical_high": 7.2,
    },
    # ── 炎症标志物 ───────────────────────────────────────────────────
    "crp": {
        "min": 0.0,
        "max": 10.0,
        "unit": "mg/L",
        "critical_high": 100.0,
    },
    "esr": {
        "min": 0.0,
        "max": 15.0,
        "unit": "mm/h",
        "critical_high": 100.0,
    },
    # ── 其他 ─────────────────────────────────────────────────────────
    "ferritin": {
        "min": 15.0,
        "max": 200.0,
        "unit": "ng/mL",
        "critical_low": 10.0,
        "critical_high": 1000.0,
    },
    "vitamin_d": {
        "min": 30.0,
        "max": 100.0,
        "unit": "ng/mL",
        "critical_low": 10.0,
    },
    "phosphate": {
        "min": 0.81,
        "max": 1.45,
        "unit": "mmol/L",
        "critical_low": 0.3,
        "critical_high": 2.1,
    },
    "ldh": {
        "min": 120.0,
        "max": 250.0,
        "unit": "U/L",
        "critical_high": 500.0,
    },
    "amylase": {
        "min": 30.0,
        "max": 110.0,
        "unit": "U/L",
        "critical_high": 300.0,
    },
    "lipase": {
        "min": 13.0,
        "max": 60.0,
        "unit": "U/L",
        "critical_high": 200.0,
    },
}


def get_reference_range(test_name: str, gender: str = "male") -> dict[str, Any] | None:
    """Return the resolved reference range for *test_name* or ``None``.

    *gender* is normalised to ``"male"``/``"female"``; unrecognised values
    fall back to the base (male) range. The returned dict is a *copy* so
    callers may mutate it without affecting the shared catalogue.
    """
    base = REFERENCE_RANGES.get(test_name)
    if base is None:
        return None
    resolved = dict(base)
    # Normalise gender (case/whitespace/aliases) so a non-lowercase value
    # does not silently fall back to the male interval. See issue #12.
    gender_key = (gender or "male").strip().lower()
    if gender_key in ("female", "f", "women", "woman", "femme"):
        gender_key = "female"
    elif gender_key not in ("male", "m", "men", "man"):
        # Anything else we cannot map is treated as the default (male) range.
        gender_key = "male"
    else:
        gender_key = "male"
    override = _GENDER_OVERRIDES.get(test_name, {}).get(gender_key)
    if override is not None:
        resolved.update(override)
    return resolved


# Unit conversion to bring a supplied value into the catalogue unit before
# classification. Only glucose is wired up today (mg/dL ↔ mmol/L); the engine
# historically evaluated raw numbers against the mmol/L range, so a glucose
# value supplied in mg/dL was classified against the wrong scale and the
# critical-value direction could be reversed (e.g. 40 mg/dL → "critical high").
# See issue #14.
_GLUCOSE_MGDL_PER_MMOL = 18.0  # g/mol ÷ … = 180.156/10, i.e. mg/dL / 18 ≈ mmol/L


def _normalise_to_catalogue_unit(
    test_name: str, value: float, unit: str | None
) -> float:
    """Convert *value* into the catalogue unit for *test_name* when a known
    mass↔molar conversion applies; otherwise return *value* unchanged."""
    if not unit:
        return value
    u = unit.strip().lower()
    if test_name == "glucose_fasting":
        # mg/dL → mmol/L. mmol/L (the catalogue unit) is left untouched.
        if u in ("mg/dl", "mg/100ml", "mg%", "mg/dl.ucum", "mg/100 ml", "mg"):
            return value / _GLUCOSE_MGDL_PER_MMOL
    return value


def get_abnormality_flag(test_name: str, value: float, gender: str = "male") -> AbnormalityFlag:
    """Classify *value* against the reference range for *test_name*.

    Critical thresholds take precedence over plain low/high so a value
    crossing both boundaries is reported at the higher acuity. ``None``
    critical bounds are treated as "not set" and skipped. An unknown
    *test_name* yields :attr:`AbnormalityFlag.UNKNOWN`.
    """
    ref = get_reference_range(test_name, gender)
    if ref is None:
        return AbnormalityFlag.UNKNOWN

    crit_low = ref.get("critical_low")
    crit_high = ref.get("critical_high")
    # Critical bounds win over low/high.
    if crit_low is not None and value <= crit_low:
        return AbnormalityFlag.CRITICAL_LOW
    if crit_high is not None and value >= crit_high:
        return AbnormalityFlag.CRITICAL_HIGH

    low = ref.get("min")
    high = ref.get("max")
    if low is not None and value < low:
        return AbnormalityFlag.LOW
    if high is not None and value > high:
        return AbnormalityFlag.HIGH
    return AbnormalityFlag.NORMAL


def _format_reference(ref: dict[str, Any]) -> str:
    """Render a range dict as a compact ``min-max`` string."""
    low = ref.get("min")
    high = ref.get("max")
    return f"{low}-{high}"


def evaluate_vitals(vitals: dict[str, float]) -> list[dict[str, Any]]:
    """Batch-evaluate a vitals mapping.

    Parameters
    ----------
    vitals:
        ``{test_name: value}`` for each vital sign to evaluate. Unknown
        test names are skipped (callers should pre-filter or use
        :func:`evaluate_lab_value` for explicit handling of unknowns).

    Returns
    -------
    list of ``{test, value, flag, reference, unit}`` dicts, one per
    known input entry. Order follows the input mapping's iteration order.
    """
    results: list[dict[str, Any]] = []
    for test_name, value in vitals.items():
        ref = get_reference_range(test_name)
        if ref is None:
            continue
        # Guard against non-numeric input (mirrors the defensive lab path)
        # so the vitals evaluator never crashes on a malformed value.
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        flag = get_abnormality_flag(test_name, numeric)
        results.append(
            {
                "test": test_name,
                "value": numeric,
                "flag": flag,
                "reference": _format_reference(ref),
                "unit": ref.get("unit"),
            }
        )
    return results


def evaluate_lab_value(
    test_name: str,
    value: float,
    unit: str | None = None,
    gender: str = "male",
) -> dict[str, Any]:
    """Evaluate a single lab value against its reference range.

    Parameters
    ----------
    test_name:
        Catalogue key (e.g. ``"hemoglobin"``).
    value:
        The measured value.
    unit:
        Optional unit hint. When supplied and mismatched with the
        catalogue unit, the mismatch is recorded in ``unit_mismatch``
        but the value is still evaluated against the catalogue range —
        callers are responsible for unit conversion upstream.
    gender:
        ``"male"`` or ``"female"``; used to resolve sex-specific ranges.

    Returns
    -------
    dict with ``test``, ``value``, ``flag``, ``reference_range``, ``unit``
    and ``abnormal``. For unknown tests, ``flag`` is :attr:`UNKNOWN`,
    ``reference_range`` is ``None`` and ``abnormal`` is ``False``.
    """
    ref = get_reference_range(test_name, gender)
    if ref is None:
        return {
            "test": test_name,
            "value": value,
            "flag": AbnormalityFlag.UNKNOWN,
            "reference_range": None,
            "unit": unit,
            "abnormal": False,
        }
    # Convert to the catalogue unit first so glucose in mg/dL (the LOINC
    # [Mass/volume] codes) is classified on the correct mmol/L scale and the
    # critical-value direction is not reversed. See issue #14.
    value = _normalise_to_catalogue_unit(test_name, float(value), unit)
    flag = get_abnormality_flag(test_name, value, gender)
    result: dict[str, Any] = {
        "test": test_name,
        "value": value,
        "flag": flag,
        "reference_range": dict(ref),
        "unit": unit if unit is not None else ref.get("unit"),
        "abnormal": flag
        in (
            AbnormalityFlag.LOW,
            AbnormalityFlag.HIGH,
            AbnormalityFlag.CRITICAL_LOW,
            AbnormalityFlag.CRITICAL_HIGH,
        ),
    }
    if unit is not None and ref.get("unit") is not None and unit != ref.get("unit"):
        result["unit_mismatch"] = True
    return result


def get_reference_ranges_version() -> str:
    """获取参考范围知识库版本号。"""
    return REFERENCE_RANGES_VERSION


def get_reference_ranges_info() -> dict:
    """获取参考范围知识库元信息。"""
    return {
        "version": REFERENCE_RANGES_VERSION,
        "changelog": REFERENCE_RANGES_CHANGELOG,
        "total_items": len(REFERENCE_RANGES),
    }

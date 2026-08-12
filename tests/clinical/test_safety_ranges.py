"""Tests for the clinical reference-range abnormality detector."""

from __future__ import annotations

from doctoragent.clinical.safety import (
    REFERENCE_RANGES,
    AbnormalityFlag,
    evaluate_lab_value,
    evaluate_vitals,
    get_abnormality_flag,
    get_reference_range,
)


def test_normal_heart_rate_is_normal() -> None:
    assert get_abnormality_flag("heart_rate", 75) is AbnormalityFlag.NORMAL


def test_low_heart_rate_is_low() -> None:
    # 50 is below min (60) but above critical_low (40).
    assert get_abnormality_flag("heart_rate", 50) is AbnormalityFlag.LOW


def test_high_heart_rate_is_high() -> None:
    # 110 is above max (100) but below critical_high (130).
    assert get_abnormality_flag("heart_rate", 110) is AbnormalityFlag.HIGH


def test_critical_low_heart_rate() -> None:
    assert get_abnormality_flag("heart_rate", 35) is AbnormalityFlag.CRITICAL_LOW


def test_critical_high_heart_rate() -> None:
    assert get_abnormality_flag("heart_rate", 140) is AbnormalityFlag.CRITICAL_HIGH


def test_critical_boundary_wins_over_low() -> None:
    # value == critical_low (40) should be CRITICAL_LOW, not LOW.
    assert get_abnormality_flag("heart_rate", 40) is AbnormalityFlag.CRITICAL_LOW
    # value == critical_high (130) should be CRITICAL_HIGH, not HIGH.
    assert get_abnormality_flag("heart_rate", 130) is AbnormalityFlag.CRITICAL_HIGH


def test_unknown_test_returns_unknown() -> None:
    assert get_abnormality_flag("not_a_real_test", 1.0) is AbnormalityFlag.UNKNOWN


def test_get_reference_range_unknown_returns_none() -> None:
    assert get_reference_range("not_a_real_test") is None


def test_get_reference_range_returns_copy() -> None:
    ref = get_reference_range("sodium")
    assert ref is not None
    ref["min"] = 9999  # mutating the copy must not affect the catalogue
    assert REFERENCE_RANGES["sodium"]["min"] == 135


def test_reference_ranges_has_at_least_20_entries() -> None:
    assert len(REFERENCE_RANGES) >= 20
    # Every entry must carry the structural fields the engine reads.
    for name, ref in REFERENCE_RANGES.items():
        assert "min" in ref, f"{name} missing min"
        assert "max" in ref, f"{name} missing max"
        assert "unit" in ref, f"{name} missing unit"


def test_evaluate_vitals_batch() -> None:
    results = evaluate_vitals(
        {"heart_rate": 75, "systolic_bp": 200, "spo2": 92, "unknown_test": 1.0}
    )
    # Unknown tests are skipped.
    assert len(results) == 3
    by_test = {r["test"]: r for r in results}
    assert by_test["heart_rate"]["flag"] is AbnormalityFlag.NORMAL
    assert by_test["systolic_bp"]["flag"] is AbnormalityFlag.CRITICAL_HIGH
    assert by_test["spo2"]["flag"] is AbnormalityFlag.LOW
    # Each entry carries the documented keys.
    for r in results:
        assert {"test", "value", "flag", "reference", "unit"} <= set(r)


def test_evaluate_lab_value_hemoglobin_male() -> None:
    # Male range 130-175; 120 is below min → LOW and abnormal.
    result = evaluate_lab_value("hemoglobin", 120.0, gender="male")
    assert result["flag"] is AbnormalityFlag.LOW
    assert result["abnormal"] is True
    assert result["reference_range"]["min"] == 130


def test_evaluate_lab_value_hemoglobin_female() -> None:
    # Female range 115-150; 120 is within range → NORMAL.
    result = evaluate_lab_value("hemoglobin", 120.0, gender="female")
    assert result["flag"] is AbnormalityFlag.NORMAL
    assert result["abnormal"] is False
    assert result["reference_range"]["min"] == 115
    assert result["reference_range"]["max"] == 150


def test_evaluate_lab_value_unknown_test() -> None:
    result = evaluate_lab_value("not_a_real_test", 1.0, unit="x")
    assert result["flag"] is AbnormalityFlag.UNKNOWN
    assert result["abnormal"] is False
    assert result["reference_range"] is None


def test_evaluate_lab_value_unit_mismatch_flagged() -> None:
    result = evaluate_lab_value("sodium", 140.0, unit="mg/dL")
    # Sodium catalogue unit is mmol/L; supplying mg/dL records a mismatch.
    assert result.get("unit_mismatch") is True


# ── Regression tests for issue #12 / #13 / #17 ───────────────────────────────


def test_spo2_at_upper_normal_is_not_critical(issue_regression: None = None) -> None:
    # SpO2=100% is the normal upper bound, NOT a critical high. See #13.
    assert get_abnormality_flag("spo2", 100.0) is AbnormalityFlag.NORMAL


def test_gender_is_normalised_case_insensitively() -> None:
    # Non-lowercase / aliased gender must resolve to the female interval
    # rather than silently falling back to male. See #12.
    assert get_reference_range("hemoglobin", "FEMALE")["min"] == 115
    assert get_reference_range("hemoglobin", "Female")["min"] == 115
    assert get_reference_range("hemoglobin", "women")["min"] == 115
    # Male (explicit) still resolves to the male interval.
    assert get_reference_range("hemoglobin", "MALE")["min"] == 130


def test_evaluate_vitals_skips_non_numeric_values() -> None:
    # A non-numeric vital must be skipped, not crash the evaluator. See #17.
    results = evaluate_vitals({"heart_rate": "not-a-number", "spo2": 98})
    assert len(results) == 1
    assert results[0]["test"] == "spo2"


# ── Regression tests for issue #14 (glucose unit conversion) ────────────────


def test_glucose_mgdl_low_is_critical_low_not_critical_high() -> None:
    # 40 mg/dL ≈ 2.2 mmol/L → hypoglycaemia. Without unit conversion this was
    # classified CRITICAL_HIGH (40 ≥ 22), reversing the critical direction.
    result = evaluate_lab_value("glucose_fasting", 40.0, unit="mg/dL")
    assert result["flag"] is AbnormalityFlag.CRITICAL_LOW
    assert result["abnormal"] is True


def test_glucose_mgdl_normal_within_range() -> None:
    # 90 mg/dL ≈ 5.0 mmol/L → NORMAL.
    result = evaluate_lab_value("glucose_fasting", 90.0, unit="mg/dL")
    assert result["flag"] is AbnormalityFlag.NORMAL
    assert result["abnormal"] is False


def test_glucose_mgdl_high_is_critical_high() -> None:
    # 400 mg/dL ≈ 22.2 mmol/L → hyperglycaemic crisis → CRITICAL_HIGH.
    result = evaluate_lab_value("glucose_fasting", 400.0, unit="mg/dL")
    assert result["flag"] is AbnormalityFlag.CRITICAL_HIGH
    assert result["abnormal"] is True


def test_glucose_mmol_l_unit_left_unchanged() -> None:
    # mmol/L (the catalogue unit) must not be scaled.
    assert evaluate_lab_value("glucose_fasting", 5.0, unit="mmol/L")["flag"] is (
        AbnormalityFlag.NORMAL
    )
    assert evaluate_lab_value("glucose_fasting", 2.5, unit="mmol/L")["flag"] is (
        AbnormalityFlag.CRITICAL_LOW
    )


def test_glucose_without_unit_hint_unchanged() -> None:
    # No unit hint → no conversion (value evaluated on the mmol/L scale as before).
    assert evaluate_lab_value("glucose_fasting", 5.0)["flag"] is AbnormalityFlag.NORMAL

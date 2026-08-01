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
    assert result["unit"] == "mg/dL"

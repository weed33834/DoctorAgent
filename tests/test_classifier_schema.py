"""Tests for classification result schema."""

import pytest
from pydantic import ValidationError

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel


def test_classification_result_defaults() -> None:
    """ClassificationResult fills default tags and summary when omitted."""
    result = ClassificationResult(
        sensitivity=SensitivityLevel.HIGH,
        category="finance",
        disguise_name="2023_report",
        disguise_extension="log",
    )
    assert result.sensitivity == SensitivityLevel.HIGH
    assert result.tags == []
    assert result.summary == ""


def test_classification_result_all_fields() -> None:
    """ClassificationResult accepts all fields explicitly."""
    result = ClassificationResult(
        sensitivity=SensitivityLevel.MEDIUM,
        category="work",
        tags=["report", "hr"],
        summary="A work report",
        disguise_name="team_building_2023",
        disguise_extension="log",
    )
    assert result.sensitivity == SensitivityLevel.MEDIUM
    assert result.category == "work"
    assert result.tags == ["report", "hr"]
    assert result.summary == "A work report"
    assert result.disguise_name == "team_building_2023"
    assert result.disguise_extension == "log"


def test_classification_result_enum_coercion() -> None:
    """String sensitivity values coerce to the SensitivityLevel enum."""
    result = ClassificationResult(
        sensitivity="critical",  # type: ignore[arg-type]
        category="health",
        disguise_name="scan",
        disguise_extension="dat",
    )
    assert result.sensitivity == SensitivityLevel.CRITICAL


def test_classification_result_invalid_sensitivity() -> None:
    """Invalid sensitivity values are rejected by the enum."""
    with pytest.raises(ValidationError):
        ClassificationResult(
            sensitivity="extreme",  # type: ignore[arg-type]
            category="work",
            disguise_name="report",
            disguise_extension="log",
        )


def test_classification_result_missing_required_fields() -> None:
    """Missing required fields raise ValidationError."""
    with pytest.raises(ValidationError):
        ClassificationResult(  # type: ignore[call-arg]
            sensitivity=SensitivityLevel.LOW,
            category="other",
        )


def test_classification_result_disguise_extension_required() -> None:
    """disguise_extension is required even when other fields are present."""
    with pytest.raises(ValidationError):
        ClassificationResult(  # type: ignore[call-arg]
            sensitivity=SensitivityLevel.LOW,
            category="other",
            disguise_name="report",
        )

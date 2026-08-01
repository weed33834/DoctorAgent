"""Tests for the PHI de-identification pipeline."""

from __future__ import annotations

import pytest

from doctoragent.clinical.deidentification import PHIDetector, PHIType


@pytest.fixture
def detector() -> PHIDetector:
    return PHIDetector()


def _types(text: str, detector: PHIDetector) -> set[str]:
    return {m["type"] for m in detector.detect_phi(text)}


def test_detect_phone(detector: PHIDetector) -> None:
    matches = detector.detect_phi("Call me at 555-123-4567 today")
    assert str(PHIType.PHONE) in _types("Call me at 555-123-4567 today", detector)
    phone_matches = [m for m in matches if m["type"] == str(PHIType.PHONE)]
    assert phone_matches
    assert phone_matches[0]["value"] == "555-123-4567"
    # Offsets point into the original text.
    assert "555-123-4567" in matches[0]["value"]


def test_detect_email(detector: PHIDetector) -> None:
    assert str(PHIType.EMAIL) in _types("Email jane.doe@example.com please", detector)


def test_detect_mrn(detector: PHIDetector) -> None:
    # A bare 7-10 digit run is tagged as MRN.
    types = _types("Ref 1234567 noted", detector)
    assert str(PHIType.MRN) in types
    # An explicit "MRN <digits>" label is tagged as MEDICAL_RECORD.
    types = _types("MRN 12345678 on file", detector)
    assert str(PHIType.MEDICAL_RECORD) in types


def test_detect_date(detector: PHIDetector) -> None:
    assert str(PHIType.DATE) in _types("Visit on 2024-01-15", detector)
    assert str(PHIType.DATE) in _types("Visit on 01/15/2024", detector)


def test_detect_ssn(detector: PHIDetector) -> None:
    assert str(PHIType.SSN) in _types("SSN 123-45-6789", detector)


def test_deidentify_redact(detector: PHIDetector) -> None:
    text = "Patient John Doe MRN 12345678 called 555-123-4567"
    out = detector.deidentify(text, strategy="redact")
    assert "[REDACTED]" in out
    # No raw PHI survives.
    assert "555-123-4567" not in out
    assert "12345678" not in out
    assert "John Doe" not in out


def test_deidentify_pseudonymize(detector: PHIDetector) -> None:
    text = "Call 555-123-4567"
    out = detector.deidentify(text, strategy="pseudonymize")
    # Pseudonym placeholders are type-tagged.
    assert "[" in out and "]" in out
    assert "555-123-4567" not in out


def test_deidentify_mask(detector: PHIDetector) -> None:
    out = detector.deidentify("Call 555-123-4567", strategy="mask")
    # Masking keeps the last 4 digits as a hint.
    assert "4567" in out
    assert "555-123-4567" not in out
    # Email masking preserves the domain.
    email_out = detector.deidentify("Email jane.doe@example.com", strategy="mask")
    assert "example.com" in email_out
    assert "jane.doe@" not in email_out


def test_pseudonymize_restore_roundtrip(detector: PHIDetector) -> None:
    text = "Patient John Doe called 555-123-4567 about MRN 12345678"
    redacted, mapping = detector.pseudonymize(text)
    assert redacted != text
    assert mapping, "pseudonymize should produce a non-empty mapping"
    restored = detector.restore(redacted, mapping)
    assert restored == text


def test_pseudonymize_consistent_placeholders(detector: PHIDetector) -> None:
    # The same value appearing twice maps to the same placeholder.
    text = "Call 555-123-4567 then 555-123-4567 again"
    redacted, mapping = detector.pseudonymize(text)
    # Only one distinct phone placeholder in the mapping.
    phone_placeholders = [p for p in mapping if p.startswith("[PHONE_")]
    assert len(phone_placeholders) == 1
    # The placeholder appears twice in the redacted text.
    assert redacted.count(phone_placeholders[0]) == 2


def test_no_phi_unchanged(detector: PHIDetector) -> None:
    text = "Hello world, no PHI here."
    assert detector.detect_phi(text) == []
    assert detector.deidentify(text) == text
    assert detector.deidentify(text, strategy="mask") == text
    redacted, mapping = detector.pseudonymize(text)
    assert redacted == text
    assert mapping == {}


def test_unknown_strategy_raises(detector: PHIDetector) -> None:
    with pytest.raises(ValueError):
        detector.deidentify("Call 555-123-4567", strategy="bogus")


def test_restore_with_empty_mapping_is_noop(detector: PHIDetector) -> None:
    assert detector.restore("nothing to restore", {}) == "nothing to restore"

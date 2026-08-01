"""Canonical clinical code-system URIs and a small registry.

Code-system URIs are spelled differently across vendors (``http://snomed.info/sct``
vs ``https://snomed.info/sct/731000124108``, ``http://loinc.org`` vs
``https://loinc.org``). Centralising the canonical forms + a tolerant parser
kills the hard-coded string literals that were scattered across the FHIR
parser, CDS Hooks translator and rules engine.

The parser is intentionally liberal: it accepts any of the common variants
and normalises to the canonical :class:`CodeSystem` enum so downstream code
can ``match`` on it rather than substring-matching URI strings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = [
    "CODE_SYSTEM_ICD10_CM",
    "CODE_SYSTEM_LOINC",
    "CODE_SYSTEM_RXNORM",
    "CODE_SYSTEM_SNOMED_CT",
    "CodeSystem",
    "parse_system_uri",
]

# Canonical URI forms (per HL7 FHIR R4 + the relevant code-system owners).
# These are the values that SHOULD appear in FHIR ``Coding.system`` fields.
CODE_SYSTEM_SNOMED_CT = "http://snomed.info/sct"
CODE_SYSTEM_LOINC = "http://loinc.org"
CODE_SYSTEM_ICD10_CM = "http://hl7.org/fhir/sid/icd-10-cm"
CODE_SYSTEM_ICD10 = "http://hl7.org/fhir/sid/icd-10"  # legacy ICD-10-WHO
CODE_SYSTEM_RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CODE_SYSTEM_HL7_CONDITION_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"


class CodeSystem(str, Enum):
    """The code systems the terminology layer knows how to resolve.

    ``UNKNOWN`` is returned by :func:`parse_system_uri` for any URI the
    parser doesn't recognise — callers must handle it (typically by
    falling back to whatever local ``Coding.display`` the resource carried).
    """

    SNOMED_CT = "snomed_ct"
    LOINC = "loinc"
    ICD10_CM = "icd10_cm"
    ICD10 = "icd10"
    RXNORM = "rxnorm"
    HL7_CONDITION_CLINICAL = "hl7_condition_clinical"
    UNKNOWN = "unknown"

    @property
    def canonical_uri(self) -> str:
        """The canonical FHIR ``Coding.system`` URI for this code system."""
        return _CANONICAL_URIS[self]


_CANONICAL_URIS: dict[CodeSystem, str] = {
    CodeSystem.SNOMED_CT: CODE_SYSTEM_SNOMED_CT,
    CodeSystem.LOINC: CODE_SYSTEM_LOINC,
    CodeSystem.ICD10_CM: CODE_SYSTEM_ICD10_CM,
    CodeSystem.ICD10: CODE_SYSTEM_ICD10,
    CodeSystem.RXNORM: CODE_SYSTEM_RXNORM,
    CodeSystem.HL7_CONDITION_CLINICAL: CODE_SYSTEM_HL7_CONDITION_CLINICAL,
    CodeSystem.UNKNOWN: "",
}

# Variants the parser accepts per code system. Built once at import time so
# the hot path is a single dict lookup. The lists are intentionally short —
# we only include the variants actually seen in the wild (vendor EHRs +
# the fixtures in tests/fixtures/clinical/).
_VARIANTS: dict[CodeSystem, tuple[str, ...]] = {
    CodeSystem.SNOMED_CT: (
        "http://snomed.info/sct",
        "https://snomed.info/sct",
        # SNOMED CT editions carry an edition/version suffix
        # (e.g. ``http://snomed.info/sct/731000124108`` for the US edition).
        # Match by prefix.
    ),
    CodeSystem.LOINC: (
        "http://loinc.org",
        "https://loinc.org",
    ),
    CodeSystem.ICD10_CM: (
        "http://hl7.org/fhir/sid/icd-10-cm",
        "https://hl7.org/fhir/sid/icd-10-cm",
        # Some EHRs use the bare ``icd-10-cm`` without the hl7.org prefix.
        "icd-10-cm",
    ),
    CodeSystem.ICD10: (
        "http://hl7.org/fhir/sid/icd-10",
        "https://hl7.org/fhir/sid/icd-10",
        "icd-10",
    ),
    CodeSystem.RXNORM: (
        "http://www.nlm.nih.gov/research/umls/rxnorm",
        "https://www.nlm.nih.gov/research/umls/rxnorm",
        "http://.nlm.nih.gov/research/umls/rxnorm",
        # Short form used by some vendor FHIR servers.
        "urn:oid:2.16.840.1.113883.6.88",
    ),
    CodeSystem.HL7_CONDITION_CLINICAL: (
        "http://terminology.hl7.org/CodeSystem/condition-clinical",
        "https://terminology.hl7.org/CodeSystem/condition-clinical",
    ),
}


def parse_system_uri(system_uri: Any) -> CodeSystem:
    """Tolerantly classify a FHIR ``Coding.system`` string.

    Accepts any of the variant URIs in :data:`_VARIANTS` and the SNOMED CT
    edition-suffixed form (``http://snomed.info/sct/{editionId}``). Returns
    :attr:`CodeSystem.UNKNOWN` for unrecognised / None / non-string input
    so callers can fall back to the resource's own ``display``.

    Examples
    --------
    >>> parse_system_uri("http://loinc.org")
    <CodeSystem.LOINC: 'loinc'>
    >>> parse_system_uri("http://snomed.info/sct/731000124108")
    <CodeSystem.SNOMED_CT: 'snomed_ct'>
    >>> parse_system_uri(None)
    <CodeSystem.UNKNOWN: 'unknown'>
    """
    if not isinstance(system_uri, str) or not system_uri:
        return CodeSystem.UNKNOWN
    # Normalise to lowercase for the variant match.
    norm = system_uri.strip().lower()
    if not norm:
        return CodeSystem.UNKNOWN
    # SNOMED CT editions: http://snomed.info/sct/<editionId>.
    if norm.startswith(("http://snomed.info/sct", "https://snomed.info/sct")):
        return CodeSystem.SNOMED_CT
    for code_system, variants in _VARIANTS.items():
        if code_system == CodeSystem.SNOMED_CT:
            # Already handled by the prefix check above.
            continue
        for v in variants:
            if norm == v:
                return code_system
    return CodeSystem.UNKNOWN


def _is_loinc(system_uri: Any) -> bool:
    """Quick predicate kept for legacy callers (CDS Hooks translator)."""
    return parse_system_uri(system_uri) == CodeSystem.LOINC


def _is_snomed(system_uri: Any) -> bool:
    """Quick predicate kept for legacy callers."""
    return parse_system_uri(system_uri) == CodeSystem.SNOMED_CT

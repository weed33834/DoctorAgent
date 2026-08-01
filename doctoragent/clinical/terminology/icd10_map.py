"""Curated ICD-10-CM code → display map + validation.

ICD-10-CM (the US clinical modification of ICD-10) is the code system used
in :class:`doctoragent.clinical.tools.clinical_tools.CodeIcd10Tool` prompts and
in the FHIR Condition fixtures. The existing LLM-based coding tool emits
codes without any validation against a code system, so a hallucinated code
like ``"E11.99.X"`` would silently flow into a SOAP note.

This module ships a curated map of the codes our prompts / fixtures /
documentation agent actually use (~40 codes covering the chronic-disease
breadth a primary-care CDS deployment hits). It also exposes a structural
validator (:func:`is_valid_icd10_cm_format`) that catches the most common
LLM hallucinations (wrong separator, too many characters, lowercased alpha
prefix) even when the code isn't in the curated map.

For the full CDC ICD-10-CM tabular list (≈ 70 000 codes), an enterprise
deployment can:

1. Download the CDC ICD-10-CM tabular list XML/JSON from
   https://www.cdc.gov/nchs/icd/comprehensive-listing-of-international-classification-of-diseases.html
2. Point ``DOCTORAGENT_TERMINOLOGY_ICD10_TABLE`` at the extracted file and call
   :func:`load_icd10_table` once at startup.

The curated entries always win over bulk-loaded ones so an operator can
override a wrong CDC display without editing the bulk file.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ICD10_DISPLAYS",
    "is_valid_icd10_cm_format",
    "load_icd10_table",
    "lookup_icd10_display",
]

# Curated ICD-10-CM displays. Covers the chronic-disease / CDS Hooks
# surface (diabetes, hypertension, atrial fibrillation, CKD, COPD, asthma,
# heart failure, the anticoagulation / infection / pain neighbours, and
# the ICD-10-CM "general" codes the LLM documentation agent tends to
# suggest). Sorted by code for easy review.
ICD10_DISPLAYS: dict[str, str] = {
    # ── Endocrine ─────────────────────────────────────────────────────────
    "E11.9": "Type 2 diabetes mellitus without complications",
    "E11.65": "Type 2 diabetes mellitus with hyperglycemia",
    "E11.22": "Type 2 diabetes mellitus with diabetic chronic kidney disease",
    "E11.40": "Type 2 diabetes mellitus with diabetic neuropathy, unspecified",
    "E78.5": "Hyperlipidemia, unspecified",
    "E78.0": "Pure hypercholesterolemia",
    "E78.2": "Mixed hyperlipidemia",
    "E03.9": "Hypothyroidism, unspecified",
    # ── Circulatory ────────────────────────────────────────────────────────
    "I10": "Essential (primary) hypertension",
    "I11.9": "Hypertensive heart disease without heart failure",
    "I50.9": "Heart failure, unspecified",
    "I48.0": "Paroxysmal atrial fibrillation",
    "I48.91": "Atrial fibrillation, unspecified",
    "I25.10": "Atherosclerotic heart disease of native coronary artery",
    "I63.9": "Cerebral infarction, unspecified",
    "I82.40": "Embolism and thrombosis of unspecified deep vessels of lower extremity",
    # ── Respiratory ────────────────────────────────────────────────────────
    "J44.9": "Chronic obstructive pulmonary disease, unspecified",
    "J45.909": "Unspecified asthma, uncomplicated",
    "J18.9": "Pneumonia, unspecified organism",
    "J00": "Acute nasopharyngitis [common cold]",
    # ── Renal ──────────────────────────────────────────────────────────────
    "N18.6": "End stage renal disease",
    "N18.3": "Chronic kidney disease, stage 3",
    "N18.4": "Chronic kidney disease, stage 4",
    "N39.0": "Urinary tract infection, site not specified",
    # ── Infectious ─────────────────────────────────────────────────────────
    "A41.9": "Sepsis, unspecified organism",
    "B97.4": "Respiratory syncytial virus as the cause of diseases classified elsewhere",
    "U07.1": "COVID-19, virus identified",
    # ── Mental / behavioural ──────────────────────────────────────────────
    "F32.9": "Major depressive disorder, single episode, unspecified",
    "F33.1": "Major depressive disorder, recurrent, moderate",
    "F41.1": "Generalized anxiety disorder",
    # ── Musculoskeletal / pain ─────────────────────────────────────────────
    "M19.90": "Osteoarthritis, unspecified site",
    "M54.50": "Low back pain, unspecified",
    "M25.50": "Pain in unspecified joint",
    # ── Symptoms / signs / general ─────────────────────────────────────────
    "R50.9": "Fever, unspecified",
    "R51.9": "Headache, unspecified",
    "R07.9": "Chest pain, unspecified",
    "R05.9": "Cough, unspecified",
    "R60.9": "Edema, unspecified",
    "R42": "Dizziness and giddiness",
    "R56.9": "Unspecified convulsions",
    # ── Injury / external ──────────────────────────────────────────────────
    "S72.00": "Fracture of unspecified part of neck of femur",
    "T78.40": "Allergy, unspecified",
    # ── Z-codes (encounter reasons) ────────────────────────────────────────
    "Z79.01": "Long term (current) use of anticoagulants",
    "Z79.4": "Long term (current) use of insulin",
    "Z79.84": "Long term (current) use of oral hypoglycemic drugs",
    "Z00.00": "Encounter for general adult medical exam w/o abnormal findings",
}

# Structural ICD-10-CM format: 1 letter + 2 digits + optional ".X[X[X]]".
# Used by :func:`is_valid_icd10_cm_format` to catch LLM hallucinations.
_ICD10_CM_PATTERN = re.compile(r"^[A-Z]\d{2}(\.\d{1,3})?$")


def is_valid_icd10_cm_format(code: str) -> bool:
    """Return ``True`` if *code* matches the ICD-10-CM structural format.

    Catches the most common LLM-coding hallucinations:

    * Lowercased alpha prefix (``"e11.9"``).
    * Wrong separator (``"E11-9"``, ``"E11 9"``).
    * Too many decimal places (``"E11.9999"``).
    * Trailing junk (``"E11.9X"`` — invalid subcategory).

    A True return does NOT mean the code exists in the ICD-10-CM release —
    use :func:`lookup_icd10_display` for that (returns ``None`` when the
    code is structurally valid but not in the curated map, so the caller
    can decide whether to flag it as a possible hallucination).
    """
    if not isinstance(code, str):
        return False
    return bool(_ICD10_CM_PATTERN.match(code.strip()))


def lookup_icd10_display(code: str) -> str | None:
    """Return the cached ICD-10-CM display for *code*, or ``None``.

    ``None`` means "not in the curated map" — combine with
    :func:`is_valid_icd10_cm_format` to distinguish "valid-but-unknown" from
    "structurally invalid".
    """
    if not isinstance(code, str) or not code:
        return None
    return ICD10_DISPLAYS.get(code.strip())


def load_icd10_table(path: str | Path | None = None) -> int:
    """Bulk-load extra ICD-10-CM displays from a downloaded CDC table.

    Parameters
    ----------
    path:
        Path to a CSV/TSV with ``CODE`` / ``DISPLAY`` columns. When
        ``None``, reads ``DOCTORAGENT_TERMINOLOGY_ICD10_TABLE`` from the env.

    Returns the number of newly-added codes. Curated entries win over
    bulk-loaded ones (same override semantics as :func:`load_loinc_table`).
    """
    if path is None:
        path = os.environ.get("DOCTORAGENT_TERMINOLOGY_ICD10_TABLE")
    if not path:
        return 0
    p = Path(path)
    if not p.is_file():
        logger.warning("ICD-10-CM table path %s does not exist; skipping", p)
        return 0
    added = 0
    with p.open("r", encoding="utf-8", newline="") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,|")
        except csv.Error:
            dialect = csv.excel  # type: ignore[assignment]
        reader = csv.DictReader(fh, dialect=dialect)
        if reader.fieldnames is None:
            return 0
        fieldnames = {fn.lower().strip(): fn for fn in reader.fieldnames}
        code_col = fieldnames.get("code") or fieldnames.get("icd10") or fieldnames.get("icd_code")
        display_col = (
            fieldnames.get("display")
            or fieldnames.get("description")
            or fieldnames.get("long_description")
            or fieldnames.get("short_description")
        )
        if not code_col or not display_col:
            logger.warning(
                "ICD-10-CM table %s missing CODE/DISPLAY columns (found %s); skipping",
                p,
                list(reader.fieldnames),
            )
            return 0
        for row in reader:
            code = (row.get(code_col) or "").strip()
            display = (row.get(display_col) or "").strip()
            if not code or not display:
                continue
            if code in ICD10_DISPLAYS:
                continue
            ICD10_DISPLAYS[code] = display
            added += 1
    logger.info("Loaded %d additional ICD-10-CM displays from %s", added, p)
    return added

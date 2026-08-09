"""Curated LOINC code → display / reference-ranges test-name map.

Why a local map?
----------------
NLM does not expose a free public REST API for individual LOINC code
lookups; the canonical LOINC release is a multi-MB zip archive (the "LOINC
Release"). For a production deployment that needs the full release, the
operator can:

1. Download the LOINC multi-axial hierarchy from
   https://loinc.org/download/ (free registration required).
2. Point ``DOCTORAGENT_TERMINOLOGY_LOINC_TABLE`` at the unzipped
   ``AccessoryFiles/MultiAxialHierarchy/MultiAxialHierarchy.csv`` and call
   :func:`load_loinc_table` once at startup.

For everyone else this curated map ships the LOINC codes the rule engine,
CDS Hooks integration and LLM prompts actually reference — about 30 codes
covering the vital-signs panel + the common metabolic / haematology /
coagulation labs the safety layer evaluates. Adding a code here is a one-line
change; no redepoy of a terminology server is needed.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "LOINC_DISPLAYS",
    "LOINC_TO_REFERENCE_RANGES_TEST",
    "VITAL_LOINC_CODES",
    "extract_first_loinc_code",
    "load_loinc_table",
    "lookup_loinc_display",
    "lookup_loinc_test_name",
]

# LOINC code → human-readable display (English; matches the LOINC "LONG_COMMON_NAME"
# for the most common vitals/labs). Keep alphabetised by code for easy review.
LOINC_DISPLAYS: dict[str, str] = {
    # ── Vital signs ────────────────────────────────────────────────────────
    "8310-5": "Body temperature",
    "8462-4": "Diastolic blood pressure",
    "8480-6": "Systolic blood pressure",
    "8867-4": "Heart rate",
    "8893-0": "Heart rate (alternate)",
    "9279-1": "Respiratory rate",
    "2708-6": "Oxygen saturation in Arterial blood",
    "59408-5": "Oxygen saturation by Pulse oximetry",
    # ── Haematology ────────────────────────────────────────────────────────
    "718-7": "Hemoglobin [Mass/volume] in Blood",
    "4544-3": "Hematocrit [Volume Fraction] of Blood",
    "789-8": "Erythrocytes [#/volume] in Blood",
    "6690-2": "Leukocytes [#/volume] in Blood",
    "777-3": "Platelets [#/volume] in Blood",
    # ── Coagulation ────────────────────────────────────────────────────────
    "2345-7": "Prothrombin time (INR)",
    # ── Metabolic / chemistry ─────────────────────────────────────────────
    "2951-2": "Sodium [Moles/volume] in Serum or Plasma",
    "33914-3": "Glucose [Mass/volume] in Body fluid",
    "1558-6": "Glucose [Mass/volume] in Serum or Plasma --fasting",
    "4548-4": "Hemoglobin A1c/Hemoglobin.total in Blood",
    # ── Renal / hepatic (commonly added to safety checks) ─────────────────
    "33914-3_2": "Glucose [Moles/volume] in Serum or Plasma",  # alt unit
    "38483-4": "Creatinine [Mass/volume] in Serum or Plasma",
    "2160-0": "Creatinine [Mass/volume] in Serum",
    "3094-0": "Urea nitrogen [Mass/volume] in Serum",
    "1751-7": "Albumin [Mass/volume] in Serum or Plasma",
    "6768-6": "Alkaline phosphatase [Enzymatic activity/volume]",
    "1742-6": "Alanine aminotransferase [Enzymatic activity/volume]",
    "1920-8": "Aspartate aminotransferase [Enzymatic activity/volume]",
    "1975-2": "Bilirubin.total [Mass/volume] in Serum or Plasma",
    # ── Lipids ─────────────────────────────────────────────────────────────
    "2093-3": "Cholesterol [Mass/volume] in Serum or Plasma",
    "2085-9": "Cholesterol.in HDL [Mass/volume]",
    "2089-1": "Cholesterol.in LDL [Mass/volume]",
    "2571-8": "Triglyceride [Mass/volume] in Serum or Plasma",
}

# LOINC code → reference_ranges test name (the dict keys used by
# doctoragent.clinical.safety.reference_ranges). Codes not in this map cannot
# be evaluated by the rule engine (they're surfaced as text only).
#
# This is the single source of truth — the CDS Hooks translator was
# previously carrying its own copy; it now imports from here so the LOINC
# binding has one canonical location.
LOINC_TO_REFERENCE_RANGES_TEST: dict[str, str] = {
    # Vital signs.
    "8867-4": "heart_rate",
    "8893-0": "heart_rate",  # alternate heart-rate code
    "8480-6": "systolic_bp",
    "8462-4": "diastolic_bp",
    "8310-5": "temperature",
    "9279-1": "respiratory_rate",
    "59408-5": "spo2",  # SpO2 by pulse oximetry
    "2708-6": "spo2",  # Oxygen saturation in Arterial blood
    # Common labs.
    "718-7": "hemoglobin",
    "4544-3": "hematocrit",
    "789-8": "rbc",
    "6690-2": "wbc",
    "777-3": "platelets",
    "2345-7": "inr",
    "2951-2": "sodium",
    "33914-3": "glucose_fasting",
    "1558-6": "glucose_fasting",
    "4548-4": "hba1c",
}

# LOINC codes that should be treated as vitals (not labs) even when the
# FHIR Observation's ``category`` is missing. Mirrors the previous
# CDS Hooks local whitelist so existing behaviour is preserved.
VITAL_LOINC_CODES: frozenset[str] = frozenset(
    {
        "8867-4",
        "8893-0",
        "8480-6",
        "8462-4",
        "8310-5",
        "9279-1",
        "59408-5",
        "2708-6",
    }
)


def lookup_loinc_display(loinc_code: str) -> str | None:
    """Return the cached LOINC display for *loinc_code*, or ``None``."""
    if not isinstance(loinc_code, str) or not loinc_code:
        return None
    return LOINC_DISPLAYS.get(loinc_code)


def lookup_loinc_test_name(loinc_code: str) -> str | None:
    """Return the reference_ranges test name for *loinc_code*, or ``None``.

    Used by the CDS Hooks translator + the rules engine to map a LOINC-coded
    Observation into the dict key the safety layer expects (e.g.
    ``"8867-4"`` → ``"heart_rate"``).
    """
    if not isinstance(loinc_code, str) or not loinc_code:
        return None
    return LOINC_TO_REFERENCE_RANGES_TEST.get(loinc_code)


def load_loinc_table(path: str | Path | None = None) -> int:
    """Bulk-load extra LOINC displays from a downloaded NLM table.

    Parameters
    ----------
    path:
        Path to a CSV/TSV with ``CODE`` / ``DISPLAY`` columns (case-insensitive).
        When ``None``, reads ``DOCTORAGENT_TERMINOLOGY_LOINC_TABLE`` from the env.

    Returns the number of newly-added codes (codes already in
    :data:`LOINC_DISPLAYS` are skipped to keep curated overrides authoritative).

    No-args call is a no-op when the env var is unset, so importing this
    module on a default install never touches the filesystem.
    """
    if path is None:
        path = os.environ.get("DOCTORAGENT_TERMINOLOGY_LOINC_TABLE")
    if not path:
        return 0
    p = Path(path)
    if not p.is_file():
        logger.warning("LOINC table path %s does not exist; skipping", p)
        return 0
    added = 0
    with p.open("r", encoding="utf-8", newline="") as fh:
        # Sniff the dialect — NLM ships both TSV (MultiAxialHierarchy) and CSV.
        sample = fh.read(2048)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,|")
        except csv.Error:
            dialect = csv.excel  # type: ignore[assignment]
        reader = csv.DictReader(fh, dialect=dialect)
        # Normalise the column names to lowercase for tolerant matching.
        if reader.fieldnames is None:
            return 0
        fieldnames = {fn.lower().strip(): fn for fn in reader.fieldnames}
        code_col = fieldnames.get("code") or fieldnames.get("loinc_num") or fieldnames.get("loinc")
        display_col = (
            fieldnames.get("display")
            or fieldnames.get("long_common_name")
            or fieldnames.get("longname")
            or fieldnames.get("shortname")
        )
        if not code_col or not display_col:
            logger.warning(
                "LOINC table %s missing CODE/DISPLAY columns (found %s); skipping",
                p,
                list(reader.fieldnames),
            )
            return 0
        for row in reader:
            code = (row.get(code_col) or "").strip()
            display = (row.get(display_col) or "").strip()
            if not code or not display:
                continue
            # Curated overrides win: don't clobber pre-existing entries.
            if code in LOINC_DISPLAYS:
                continue
            LOINC_DISPLAYS[code] = display
            added += 1
    logger.info("Loaded %d additional LOINC displays from %s", added, p)
    return added


def extract_first_loinc_code(codeable: Any) -> str | None:
    """Return the first LOINC coding's code from a FHIR CodeableConcept.

    Centralised here so the CDS Hooks translator and the FHIR parser can
    share one tolerant extractor (handles bare Coding dicts, vendor
    extensions, missing ``system`` fields).
    """
    if not isinstance(codeable, dict):
        return None
    codings = codeable.get("coding")
    if not isinstance(codings, list):
        return None
    from doctoragent.clinical.terminology.codesystems import CodeSystem, parse_system_uri

    for coding in codings:
        if not isinstance(coding, dict):
            continue
        system = coding.get("system", "")
        if parse_system_uri(system) != CodeSystem.LOINC:
            continue
        code = coding.get("code")
        if isinstance(code, str) and code:
            return code
    return None

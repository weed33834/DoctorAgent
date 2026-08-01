"""Clinical terminology binding (SNOMED CT / LOINC / ICD-10-CM / RxNorm).

Provides a single :class:`TerminologyService` façade that resolves a coded
concept ``(system_uri, code)`` to a human-readable display, with optional
SNOMED CT hierarchy lookups via a Snowstorm terminology server.

Design
------
* **Code-system URIs** are centralised in :mod:`codesystems` so every layer
  (FHIR parser, CDS Hooks, rules engine, LLM prompts) spells them the same
  way. This kills the hard-coded ``"http://loinc.org"`` /
  ``"http://snomed.info/sct"`` literals currently scattered across the
  codebase.
* **SNOMED CT** is the only code system we look up live (via a Snowstorm
  REST client in :mod:`snowstorm`). It carries hierarchy information that
  the rules engine needs (e.g. "is this drug a beta-lactam?") which a flat
  local map cannot provide.
* **LOINC** and **ICD-10-CM** use curated local maps (:mod:`loinc_map` and
  :mod:`icd10_map`). NLM publishes these as downloadable tab-delimited
  value-set tables; the curated subsets ship the codes the rule engine /
  prompts / CDS Hooks integration actually need. An enterprise deployment
  can replace them with the full NLM tables by setting
  ``DOCTORAGENT_TERMINOLOGY_LOINC_TABLE`` / ``DOCTORAGENT_ICD10_TABLE`` to point at
  a downloaded file — the loaders accept a path argument.
* **RxNorm** is NOT re-implemented here — the existing
  :class:`doctoragent.clinical.knowledge.rxnorm.RxNormClient` already covers
  drug-name normalisation. The TerminologyService accepts an optional
  ``rxnorm_client`` and delegates to it for ``system=RxNorm`` lookups.

Why no extra dependency?
-----------------------
The user's standing instruction is "use external libs when they exist".
For terminology:
* Snowstorm (SNOMED CT) — there is **no** maintained PyPI client; the
  SNOMED International browser API is plain JSON-over-HTTP, so we use
  :mod:`httpx` (already a core dep). Mirrors RxNormClient / OpenFDAClient.
* LOINC — no maintained PyPI library; NLM ships downloadable tables only.
* ICD-10-CM — no maintained PyPI library; CDC/NLM ship downloadable tables.

So httpx + tenacity (both already core deps) are the right tools; no
new pyproject.toml entry is needed.
"""

from __future__ import annotations

from doctoragent.clinical.terminology.codesystems import (
    CODE_SYSTEM_ICD10_CM,
    CODE_SYSTEM_LOINC,
    CODE_SYSTEM_RXNORM,
    CODE_SYSTEM_SNOMED_CT,
    CodeSystem,
    parse_system_uri,
)
from doctoragent.clinical.terminology.icd10_map import (
    ICD10_DISPLAYS,
    lookup_icd10_display,
)
from doctoragent.clinical.terminology.loinc_map import (
    LOINC_DISPLAYS,
    LOINC_TO_REFERENCE_RANGES_TEST,
    lookup_loinc_display,
    lookup_loinc_test_name,
)
from doctoragent.clinical.terminology.service import (
    TerminologyService,
    TerminologyServiceError,
    TerminologyServiceResult,
    lookup_display,
)
from doctoragent.clinical.terminology.snowstorm import (
    SNOWSTORM_DEFAULT_BASE_URL,
    SnowstormClient,
    SnowstormError,
    SnowstormNotFoundError,
)

__all__ = [
    "CODE_SYSTEM_ICD10_CM",
    "CODE_SYSTEM_LOINC",
    "CODE_SYSTEM_RXNORM",
    "CODE_SYSTEM_SNOMED_CT",
    "CodeSystem",
    "ICD10_DISPLAYS",
    "LOINC_DISPLAYS",
    "LOINC_TO_REFERENCE_RANGES_TEST",
    "SNOWSTORM_DEFAULT_BASE_URL",
    "SnowstormClient",
    "SnowstormError",
    "SnowstormNotFoundError",
    "TerminologyService",
    "TerminologyServiceError",
    "TerminologyServiceResult",
    "lookup_display",
    "lookup_icd10_display",
    "lookup_loinc_display",
    "lookup_loinc_test_name",
    "parse_system_uri",
]

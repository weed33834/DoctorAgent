"""Clinical safety rule-engine surface.

Exports the reference-range abnormality detector from
:mod:`doctoragent.clinical.safety.reference_ranges` plus the deterministic
clinical rule engine (:mod:`rules`) and the LLM-output guardrails
(:mod:`guardrails`). The rule engine and guardrails have no external
dependencies beyond what the rest of the clinical stack already pulls in,
so importing this subpackage never raises ``ImportError``.
"""

from doctoragent.clinical.safety.guardrails import (
    ClinicalGuardrails,
    GuardrailResult,
)
from doctoragent.clinical.safety.reference_ranges import (
    REFERENCE_RANGES,
    AbnormalityFlag,
    evaluate_lab_value,
    evaluate_vitals,
    get_abnormality_flag,
    get_reference_range,
)
from doctoragent.clinical.safety.rules import (
    ALLERGY_CROSS_REACTIVITY,
    CROSS_REACTIVITY_SEVERITY,
    ClinicalRuleEngine,
    ClinicalRuleResult,
    ClinicalRuleType,
    get_allergy_cross_reactivity,
    get_allergy_cross_reactivity_warnings,
)

__all__ = [
    "ALLERGY_CROSS_REACTIVITY",
    "AbnormalityFlag",
    "CROSS_REACTIVITY_SEVERITY",
    "ClinicalGuardrails",
    "ClinicalRuleEngine",
    "ClinicalRuleResult",
    "ClinicalRuleType",
    "GuardrailResult",
    "REFERENCE_RANGES",
    "evaluate_lab_value",
    "evaluate_vitals",
    "get_abnormality_flag",
    "get_allergy_cross_reactivity",
    "get_allergy_cross_reactivity_warnings",
    "get_reference_range",
]

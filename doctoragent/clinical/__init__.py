"""DoctorAgent clinical AI adapter layer.

This top-level package groups the healthcare-specific adapters (FHIR R4 today,
openFDA / RxNorm / clinical-note writers later). Each subpackage is imported
defensively so that ``import doctoragent.clinical`` always succeeds even when an
optional dependency (e.g. ``fhir.resources``) is not installed. The individual
helpers raise a clear :class:`ImportError` with install instructions when
actually invoked without their backing library.

Public symbols are re-exported here for convenience; the canonical home for
each is its subpackage.
"""

# Declared up-front so ruff recognises these as intentional re-exports. If the
# FHIR subpackage fails to import (optional dependency missing), ``__all__`` is
# reset to empty in the ``except`` branch below so ``from doctoragent.clinical
# import *`` never references undefined names.
__all__ = [
    "AllergySummary",
    "EncounterSummary",
    "FHIRClient",
    "FHIRClientError",
    "FHIRConnectionError",
    "FHIROperationError",
    "FHIRResourceNotFoundError",
    "LabResult",
    "MedicationSummary",
    "PatientRecord",
    "PatientSummary",
    "SUPPORTED_READ_RESOURCES",
    "allergy_to_text",
    "condition_to_text",
    "encounter_to_text",
    "lab_to_text",
    "medication_to_text",
    "parse_resource",
    "patient_summary_line",
    "patient_to_text",
    "serialize_resource",
    "validate_resource",
]

# --- FHIR R4 adapter ------------------------------------------------------- #
try:
    from doctoragent.clinical.fhir import (  # noqa: F401
        SUPPORTED_READ_RESOURCES,
        AllergySummary,
        EncounterSummary,
        FHIRClient,
        FHIRClientError,
        FHIRConnectionError,
        FHIROperationError,
        FHIRResourceNotFoundError,
        LabResult,
        MedicationSummary,
        PatientRecord,
        PatientSummary,
        allergy_to_text,
        condition_to_text,
        encounter_to_text,
        lab_to_text,
        medication_to_text,
        parse_resource,
        patient_summary_line,
        patient_to_text,
        serialize_resource,
        validate_resource,
    )
except ImportError:  # pragma: no cover - graceful degradation path
    # fhir.resources (or its transitive deps) not installed; leave the names
    # undefined so ``hasattr(doctoragent.clinical, "FHIRClient")`` is False and
    # callers can present a helpful message.
    __all__ = []

# --- Safety rule engine + LLM guardrails ----------------------------------- #
# These are pure-logic (no optional third-party deps), so the import only
# fails if the safety subpackage itself is unavailable. Wrapped in try/except
# to mirror the FHIR block's defensive posture and keep ``import
# doctoragent.clinical`` resilient.
try:
    from doctoragent.clinical.safety import (  # noqa: F401
        ClinicalGuardrails,
        ClinicalRuleEngine,
        ClinicalRuleResult,
        ClinicalRuleType,
        GuardrailResult,
    )

    __all__ += [
        "ClinicalGuardrails",
        "ClinicalRuleEngine",
        "ClinicalRuleResult",
        "ClinicalRuleType",
        "GuardrailResult",
    ]
except ImportError:  # pragma: no cover - graceful degradation path
    pass

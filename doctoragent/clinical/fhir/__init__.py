"""FHIR R4 adapter subpackage.

Exposes the async :class:`FHIRClient`, resource parse/serialize/validate
helpers, and the clinician-readable text parser. The underlying
``fhir.resources`` library is imported defensively inside
:mod:`doctoragent.clinical.fhir.resources`; importing this subpackage itself never
fails even when ``fhir.resources`` is absent (calling the resource helpers
will raise a clear :class:`ImportError` in that case).
"""

from doctoragent.clinical.fhir.client import (
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
)
from doctoragent.clinical.fhir.parser import (
    allergy_to_text,
    condition_to_text,
    encounter_to_text,
    lab_to_text,
    medication_to_text,
    patient_summary_line,
    patient_to_text,
)
from doctoragent.clinical.fhir.resources import (
    SUPPORTED_READ_RESOURCES,
    parse_resource,
    serialize_resource,
    validate_resource,
)
from doctoragent.clinical.fhir.smart import (
    SMARTClient,
    SMARTDiscovery,
    SMARTDiscoveryError,
    SMARTLaunchError,
    SMARTLaunchParams,
    SMARTLaunchResult,
    SMARTScopeError,
)

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
    "SMARTClient",
    "SMARTDiscovery",
    "SMARTDiscoveryError",
    "SMARTLaunchError",
    "SMARTLaunchParams",
    "SMARTLaunchResult",
    "SMARTScopeError",
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

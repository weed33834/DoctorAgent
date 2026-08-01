"""Clinical tools for the LLM agent — 15 Tool subclasses + registry factory.

Tools are grouped by responsibility:

* :mod:`doctoragent.clinical.tools.fhir_tools` — FHIR read-only access (4 tools).
* :mod:`doctoragent.clinical.tools.knowledge_tools` — DDI / PubMed / reference
  range queries (5 tools).
* :mod:`doctoragent.clinical.tools.clinical_tools` — LLM-driven generation,
  safety alerts and human-confirmed note staging (5 tools).
* :mod:`doctoragent.clinical.tools.compliance_tool` — HIPAA self-check (1 tool).

Use :func:`create_clinical_registry` to build a
:class:`~doctoragent.model.tools.ToolRegistry` with all 15 tools registered.
"""

from doctoragent.clinical.tools.clinical_tools import (
    CodeIcd10Tool,
    FlagSafetyAlertTool,
    GenerateDifferentialDiagnosisTool,
    GenerateSoapNoteTool,
    WriteClinicalNoteTool,
)
from doctoragent.clinical.tools.compliance_tool import ComplianceSelfCheckTool
from doctoragent.clinical.tools.fhir_tools import (
    ReadAllergiesTool,
    ReadLabResultsTool,
    ReadMedicationsTool,
    ReadPatientRecordTool,
)
from doctoragent.clinical.tools.knowledge_tools import (
    CheckDrugInteractionsTool,
    CheckLabRangesTool,
    CheckVitalsTool,
    SearchClinicalGuidelinesTool,
    SearchLiteratureTool,
)
from doctoragent.clinical.tools.registry import create_clinical_registry

__all__ = [
    "CheckDrugInteractionsTool",
    "CheckLabRangesTool",
    "CheckVitalsTool",
    "CodeIcd10Tool",
    "ComplianceSelfCheckTool",
    "FlagSafetyAlertTool",
    "GenerateDifferentialDiagnosisTool",
    "GenerateSoapNoteTool",
    "ReadAllergiesTool",
    "ReadLabResultsTool",
    "ReadMedicationsTool",
    "ReadPatientRecordTool",
    "SearchClinicalGuidelinesTool",
    "SearchLiteratureTool",
    "WriteClinicalNoteTool",
    "create_clinical_registry",
]

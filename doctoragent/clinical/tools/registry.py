"""Clinical tool registry factory.

:func:`create_clinical_registry` instantiates and registers all 15 clinical
tools onto a fresh :class:`~doctoragent.model.tools.ToolRegistry`. Dependencies
(FHIR / RxNorm / openFDA / PubMed clients, LLM provider, AegisConfig) are
dependency-injected; any that are ``None`` are still registered — the tool
itself returns a defensive ``ToolResult(success=False, error=...)`` when
called without its dependency configured.
"""

from __future__ import annotations

from typing import Any

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
from doctoragent.model.tools import ToolRegistry

__all__ = ["create_clinical_registry"]


def create_clinical_registry(
    fhir_client: Any = None,
    rxnorm_client: Any = None,
    openfda_client: Any = None,
    pubmed_client: Any = None,
    llm_provider: Any = None,
    config: Any = None,
) -> ToolRegistry:
    """Create a :class:`ToolRegistry` populated with all 15 clinical tools.

    Every tool is registered regardless of whether its dependency is
    supplied — a tool whose dependency is ``None`` returns a defensive
    failure result at execution time rather than raising at registration.
    """
    registry = ToolRegistry()

    # ── FHIR read tools (4) ────────────────────────────────────────────
    registry.register(ReadPatientRecordTool(fhir_client))
    registry.register(ReadMedicationsTool(fhir_client))
    registry.register(ReadAllergiesTool(fhir_client))
    registry.register(ReadLabResultsTool(fhir_client))

    # ── Knowledge query tools (5) ──────────────────────────────────────
    registry.register(CheckDrugInteractionsTool(rxnorm_client, openfda_client))
    registry.register(SearchClinicalGuidelinesTool(pubmed_client))
    registry.register(SearchLiteratureTool(pubmed_client))
    registry.register(CheckVitalsTool())
    registry.register(CheckLabRangesTool())

    # ── LLM generation / write tools (5) ───────────────────────────────
    registry.register(GenerateDifferentialDiagnosisTool(llm_provider))
    registry.register(GenerateSoapNoteTool(llm_provider))
    registry.register(CodeIcd10Tool(llm_provider))
    registry.register(FlagSafetyAlertTool())
    registry.register(WriteClinicalNoteTool())

    # ── Compliance tool (1) ────────────────────────────────────────────
    registry.register(ComplianceSelfCheckTool(config))

    return registry

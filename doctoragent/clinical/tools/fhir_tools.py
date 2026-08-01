"""FHIR read-only clinical tools.

Each tool wraps a single :class:`~doctoragent.clinical.fhir.client.FHIRClient`
read method so the LLM agent can fetch minimum-necessary patient data on
demand. All tools are **read-only**; write operations live in
:mod:`doctoragent.clinical.tools.clinical_tools` and require human confirmation.
"""

from __future__ import annotations

import time
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

__all__ = [
    "ReadAllergiesTool",
    "ReadLabResultsTool",
    "ReadMedicationsTool",
    "ReadPatientRecordTool",
]


class ReadPatientRecordTool(Tool):
    """Read an aggregated patient record (demographics + problems + meds)."""

    def __init__(self, fhir_client: Any = None) -> None:
        self.fhir_client = fhir_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_patient_record",
            description=(
                "Read an aggregated FHIR patient record: demographics, active "
                "conditions, recent encounters, active medications and allergies."
            ),
            parameters=[
                ToolParameter(
                    name="patient_id",
                    type="string",
                    description="FHIR Patient resource id",
                ),
            ],
            category="clinical_fhir",
        )

    async def execute(self, patient_id: str) -> ToolResult:
        start = time.time()
        try:
            if self.fhir_client is None:
                return ToolResult(
                    success=False,
                    error="FHIR client not configured",
                    tool_name=self.definition.name,
                )
            record = await self.fhir_client.read_patient_record(patient_id)
            return ToolResult(
                success=True,
                data={"patient_record": record},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:  # defensive: never raise to the agent
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class ReadMedicationsTool(Tool):
    """Read active MedicationRequest resources for a patient."""

    def __init__(self, fhir_client: Any = None) -> None:
        self.fhir_client = fhir_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_medications",
            description="Read a patient's active medication orders from the FHIR server.",
            parameters=[
                ToolParameter(
                    name="patient_id",
                    type="string",
                    description="FHIR Patient resource id",
                ),
            ],
            category="clinical_fhir",
        )

    async def execute(self, patient_id: str) -> ToolResult:
        start = time.time()
        try:
            if self.fhir_client is None:
                return ToolResult(
                    success=False,
                    error="FHIR client not configured",
                    tool_name=self.definition.name,
                )
            medications = await self.fhir_client.read_medications(patient_id)
            return ToolResult(
                success=True,
                data={"medications": medications, "count": len(medications)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class ReadAllergiesTool(Tool):
    """Read active AllergyIntolerance resources for a patient."""

    def __init__(self, fhir_client: Any = None) -> None:
        self.fhir_client = fhir_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_allergies",
            description="Read a patient's active allergy intolerances from the FHIR server.",
            parameters=[
                ToolParameter(
                    name="patient_id",
                    type="string",
                    description="FHIR Patient resource id",
                ),
            ],
            category="clinical_fhir",
        )

    async def execute(self, patient_id: str) -> ToolResult:
        start = time.time()
        try:
            if self.fhir_client is None:
                return ToolResult(
                    success=False,
                    error="FHIR client not configured",
                    tool_name=self.definition.name,
                )
            allergies = await self.fhir_client.read_allergies(patient_id)
            return ToolResult(
                success=True,
                data={"allergies": allergies, "count": len(allergies)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class ReadLabResultsTool(Tool):
    """Read recent laboratory Observation resources for a patient."""

    def __init__(self, fhir_client: Any = None) -> None:
        self.fhir_client = fhir_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_lab_results",
            description="Read a patient's recent laboratory results from the FHIR server.",
            parameters=[
                ToolParameter(
                    name="patient_id",
                    type="string",
                    description="FHIR Patient resource id",
                ),
                ToolParameter(
                    name="count",
                    type="integer",
                    description="Maximum number of results to return",
                    required=False,
                    default=20,
                ),
            ],
            category="clinical_fhir",
        )

    async def execute(self, patient_id: str, count: int = 20) -> ToolResult:
        start = time.time()
        try:
            if self.fhir_client is None:
                return ToolResult(
                    success=False,
                    error="FHIR client not configured",
                    tool_name=self.definition.name,
                )
            labs = await self.fhir_client.read_lab_results(patient_id, count=count)
            return ToolResult(
                success=True,
                data={"lab_results": labs, "count": len(labs)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )

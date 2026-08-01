"""LLM-driven clinical generation and write tools.

These tools call an injected LLM provider to generate structured clinical
artefacts (differential diagnosis, SOAP notes, ICD-10 codes). Two non-LLM
tools live here as well:

* :class:`FlagSafetyAlertTool` — surfaces a safety alert for clinician review.
* :class:`WriteClinicalNoteTool` — **requires human confirmation** before any
  FHIR write; ``execute`` returns a confirmation payload rather than writing.
"""

from __future__ import annotations

import time
from typing import Any

from doctoragent.model.agent import _extract_json
from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

__all__ = [
    "CodeIcd10Tool",
    "FlagSafetyAlertTool",
    "GenerateDifferentialDiagnosisTool",
    "GenerateSoapNoteTool",
    "WriteClinicalNoteTool",
]


def _extract_content(response: Any) -> str:
    """Normalise a chat_completion response to a plain string."""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(response, dict):
        return str(response.get("content", ""))
    return str(response)


class GenerateDifferentialDiagnosisTool(Tool):
    """Generate a ranked differential diagnosis via the LLM provider."""

    def __init__(self, llm_provider: Any = None) -> None:
        self.llm_provider = llm_provider

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="generate_differential_diagnosis",
            description=(
                "Generate a ranked differential diagnosis from a patient summary. "
                "Returns structured JSON: "
                "[{diagnosis, icd10, probability, evidence}]."
            ),
            parameters=[
                ToolParameter(
                    name="patient_summary",
                    type="string",
                    description="Structured patient summary (demographics, vitals, labs, history)",
                ),
                ToolParameter(
                    name="max_ddx",
                    type="integer",
                    description="Maximum number of differential diagnoses to return",
                    required=False,
                    default=5,
                ),
            ],
            category="clinical_generation",
        )

    async def execute(self, patient_summary: str, max_ddx: int = 5) -> ToolResult:
        start = time.time()
        try:
            if self.llm_provider is None:
                return ToolResult(
                    success=False,
                    error="LLM provider not configured",
                    tool_name=self.definition.name,
                )
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a clinical decision-support assistant. Generate a "
                        f"ranked differential diagnosis (max {max_ddx}) as strict JSON: "
                        "a list of objects with keys diagnosis, icd10, probability "
                        "(0-1 float), evidence (string). Return ONLY the JSON array."
                    ),
                },
                {"role": "user", "content": patient_summary},
            ]
            response = await self.llm_provider.chat_completion(messages)
            content = _extract_content(response)
            parsed = _extract_json(content)
            if not isinstance(parsed, list):
                return ToolResult(
                    success=False,
                    error="LLM response was not a JSON array",
                    data={"raw_response": content},
                    tool_name=self.definition.name,
                )
            return ToolResult(
                success=True,
                data={"differential_diagnosis": parsed[:max_ddx], "count": len(parsed)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class GenerateSoapNoteTool(Tool):
    """Generate a SOAP note from patient context via the LLM provider."""

    def __init__(self, llm_provider: Any = None) -> None:
        self.llm_provider = llm_provider

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="generate_soap_note",
            description=(
                "Generate a structured SOAP (Subjective, Objective, Assessment, "
                "Plan) clinical note from patient context."
            ),
            parameters=[
                ToolParameter(
                    name="patient_context",
                    type="string",
                    description=(
                        "Patient context including chief complaint, vitals, labs and history"
                    ),
                ),
            ],
            category="clinical_generation",
        )

    async def execute(self, patient_context: str) -> ToolResult:
        start = time.time()
        try:
            if self.llm_provider is None:
                return ToolResult(
                    success=False,
                    error="LLM provider not configured",
                    tool_name=self.definition.name,
                )
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a clinical documentation assistant. Generate a "
                        "SOAP note with clearly labelled Subjective, Objective, "
                        "Assessment and Plan sections from the patient context."
                    ),
                },
                {"role": "user", "content": patient_context},
            ]
            response = await self.llm_provider.chat_completion(messages)
            content = _extract_content(response)
            return ToolResult(
                success=True,
                data={"soap_note": content},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class CodeIcd10Tool(Tool):
    """Map free-text clinical text to candidate ICD-10 codes via the LLM."""

    def __init__(self, llm_provider: Any = None) -> None:
        self.llm_provider = llm_provider

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="code_icd10",
            description=(
                "Map free-text clinical text to candidate ICD-10 codes. Returns "
                "structured JSON: [{code, description, confidence}]."
            ),
            parameters=[
                ToolParameter(
                    name="clinical_text",
                    type="string",
                    description="Free-text clinical narrative to code",
                ),
            ],
            category="clinical_generation",
        )

    async def execute(self, clinical_text: str) -> ToolResult:
        start = time.time()
        try:
            if self.llm_provider is None:
                return ToolResult(
                    success=False,
                    error="LLM provider not configured",
                    tool_name=self.definition.name,
                )
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a medical coding assistant. Map the clinical text "
                        "to candidate ICD-10 codes as strict JSON: a list of "
                        "objects with keys code, description, confidence (0-1 "
                        "float). Return ONLY the JSON array."
                    ),
                },
                {"role": "user", "content": clinical_text},
            ]
            response = await self.llm_provider.chat_completion(messages)
            content = _extract_content(response)
            parsed = _extract_json(content)
            if not isinstance(parsed, list):
                return ToolResult(
                    success=False,
                    error="LLM response was not a JSON array",
                    data={"raw_response": content},
                    tool_name=self.definition.name,
                )
            return ToolResult(
                success=True,
                data={"icd10_codes": parsed, "count": len(parsed)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class FlagSafetyAlertTool(Tool):
    """Surface a safety alert for clinician review.

    Does not write to FHIR; returns a confirmation payload capturing the
    alert details so a downstream workflow / human can act on it.
    """

    def __init__(self) -> None:
        # No external dependency — pure alert capture.
        pass

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="flag_safety_alert",
            description=(
                "Flag a clinical safety alert for clinician review. Captures the "
                "alert type, description and severity without writing to the EHR."
            ),
            parameters=[
                ToolParameter(
                    name="patient_id",
                    type="string",
                    description="FHIR Patient resource id the alert applies to",
                ),
                ToolParameter(
                    name="alert_type",
                    type="string",
                    description="Alert category, e.g. 'drug_interaction', 'critical_lab'",
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="Human-readable description of the safety concern",
                ),
                ToolParameter(
                    name="severity",
                    type="string",
                    description="Alert severity",
                    enum=["low", "moderate", "high", "critical"],
                ),
            ],
            category="clinical_alert",
        )

    async def execute(
        self,
        patient_id: str,
        alert_type: str,
        description: str,
        severity: str,
    ) -> ToolResult:
        start = time.time()
        try:
            confirmation = {
                "patient_id": patient_id,
                "alert_type": alert_type,
                "description": description,
                "severity": severity,
                "flagged": True,
                "timestamp": time.time(),
            }
            return ToolResult(
                success=True,
                data=confirmation,
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class WriteClinicalNoteTool(Tool):
    """Stage a clinical note for human-confirmed FHIR write.

    Per safety policy this tool **never writes directly to FHIR**. It returns
    a confirmation payload; the actual ``FHIRClient.create`` call is made by
    a workflow after a human reviewer confirms the note.
    """

    def __init__(self) -> None:
        # No FHIR client — writes are deferred to the confirmation workflow.
        pass

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_clinical_note",
            description=(
                "Stage a clinical note for human-confirmed write to the FHIR "
                "server. Returns a confirmation payload; the note is NOT written "
                "until a human reviewer approves it in the workflow."
            ),
            parameters=[
                ToolParameter(
                    name="patient_id",
                    type="string",
                    description="FHIR Patient resource id the note belongs to",
                ),
                ToolParameter(
                    name="note_content",
                    type="string",
                    description="Full text of the clinical note to stage",
                ),
                ToolParameter(
                    name="note_type",
                    type="string",
                    description="Note type, e.g. 'progress', 'discharge', 'consult'",
                    required=False,
                    default="progress",
                ),
            ],
            category="clinical_write",
        )

    async def execute(
        self,
        patient_id: str,
        note_content: str,
        note_type: str = "progress",
    ) -> ToolResult:
        # Intentionally does NOT write to FHIR — see class docstring.
        return ToolResult(
            success=False,
            error="Requires human confirmation",
            data={
                "requires_confirmation": True,
                "patient_id": patient_id,
                "note": note_content,
                "note_type": note_type,
            },
            tool_name=self.definition.name,
        )

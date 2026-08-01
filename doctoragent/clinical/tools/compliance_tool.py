"""HIPAA compliance self-check tool.

Wraps :class:`~doctoragent.clinical.compliance_report.ComplianceReport` behind
the Tool interface so the LLM agent can run an on-demand compliance posture
self-assessment.
"""

from __future__ import annotations

import time
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolResult

__all__ = ["ComplianceSelfCheckTool"]


class ComplianceSelfCheckTool(Tool):
    """Run a HIPAA compliance self-assessment and return the report dict."""

    def __init__(self, config: Any = None) -> None:
        # Fall back to the default AegisConfig when none is injected.
        self.config = config

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="compliance_self_check",
            description=(
                "Run a HIPAA compliance self-assessment covering encryption, "
                "audit logging, access control, PHI protection, key management "
                "and data residency. Returns a structured compliance report."
            ),
            parameters=[],
            category="clinical_compliance",
        )

    async def execute(self) -> ToolResult:
        start = time.time()
        try:
            from doctoragent.clinical.compliance_report import ComplianceReport
            from doctoragent.config import AegisConfig

            config = self.config if self.config is not None else AegisConfig()
            report = ComplianceReport(config).generate()
            return ToolResult(
                success=True,
                data=report,
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )

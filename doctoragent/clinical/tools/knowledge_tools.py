"""Clinical knowledge-source query tools.

Wraps the deterministic drug-interaction engine, PubMed literature search
and the reference-range abnormality detector behind the Tool interface so
the LLM agent can query them via function calling.
"""

from __future__ import annotations

import time
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

__all__ = [
    "CheckDrugInteractionsTool",
    "CheckLabRangesTool",
    "CheckVitalsTool",
    "SearchClinicalGuidelinesTool",
    "SearchLiteratureTool",
]


class CheckDrugInteractionsTool(Tool):
    """Check a list of drugs for drug-drug interactions (DDI)."""

    def __init__(
        self,
        rxnorm_client: Any = None,
        openfda_client: Any = None,
    ) -> None:
        self.rxnorm_client = rxnorm_client
        self.openfda_client = openfda_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="check_drug_interactions",
            description=(
                "Check a list of drugs for drug-drug interactions using RxNorm "
                "normalization and openFDA label data. Returns interactions "
                "sorted by severity (most severe first)."
            ),
            parameters=[
                ToolParameter(
                    name="drugs",
                    type="array",
                    description="List of drug names (generic or brand) to cross-check",
                ),
            ],
            category="clinical_knowledge",
        )

    async def execute(self, drugs: list[str]) -> ToolResult:
        start = time.time()
        try:
            if self.rxnorm_client is None or self.openfda_client is None:
                return ToolResult(
                    success=False,
                    error="RxNorm/openFDA client not configured",
                    tool_name=self.definition.name,
                )
            from doctoragent.clinical.knowledge import check_drug_interactions

            interactions = await check_drug_interactions(
                drugs,
                rxnorm=self.rxnorm_client,
                openfda=self.openfda_client,
            )
            return ToolResult(
                success=True,
                data={
                    "interactions": [i.model_dump() for i in interactions],
                    "count": len(interactions),
                },
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class SearchClinicalGuidelinesTool(Tool):
    """Search PubMed for clinical trials, reviews and guidelines."""

    def __init__(self, pubmed_client: Any = None) -> None:
        self.pubmed_client = pubmed_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_clinical_guidelines",
            description=(
                "Search PubMed for clinical trials, systematic reviews and "
                "practice guidelines relevant to a clinical query."
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="Natural language clinical query",
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="Maximum number of articles to return",
                    required=False,
                    default=5,
                ),
            ],
            category="clinical_knowledge",
        )

    async def execute(self, query: str, max_results: int = 5) -> ToolResult:
        start = time.time()
        try:
            if self.pubmed_client is None:
                return ToolResult(
                    success=False,
                    error="PubMed client not configured",
                    tool_name=self.definition.name,
                )
            articles = await self.pubmed_client.search_clinical(query, max_results=max_results)
            return ToolResult(
                success=True,
                data={"articles": articles, "count": len(articles)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class SearchLiteratureTool(Tool):
    """Search PubMed for general biomedical literature."""

    def __init__(self, pubmed_client: Any = None) -> None:
        self.pubmed_client = pubmed_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_literature",
            description="Search PubMed for biomedical literature matching a query.",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="Natural language literature search query",
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="Maximum number of articles to return",
                    required=False,
                    default=5,
                ),
            ],
            category="clinical_knowledge",
        )

    async def execute(self, query: str, max_results: int = 5) -> ToolResult:
        start = time.time()
        try:
            if self.pubmed_client is None:
                return ToolResult(
                    success=False,
                    error="PubMed client not configured",
                    tool_name=self.definition.name,
                )
            articles = await self.pubmed_client.search(query, max_results=max_results)
            return ToolResult(
                success=True,
                data={"articles": articles, "count": len(articles)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class CheckVitalsTool(Tool):
    """Batch-evaluate patient vitals against reference ranges."""

    def __init__(self) -> None:
        # Pure-function engine; no external client required.
        pass

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="check_vitals",
            description=(
                "Evaluate a set of patient vital signs against curated reference "
                "ranges. Returns an abnormality flag for each known vital."
            ),
            parameters=[
                ToolParameter(
                    name="vitals",
                    type="object",
                    description=(
                        "Mapping of vital name to value, e.g. "
                        '{"heart_rate": 110, "systolic_bp": 140}'
                    ),
                ),
            ],
            category="clinical_knowledge",
        )

    async def execute(self, vitals: dict[str, float]) -> ToolResult:
        start = time.time()
        try:
            from doctoragent.clinical.safety.reference_ranges import evaluate_vitals

            results = evaluate_vitals(vitals)
            return ToolResult(
                success=True,
                data={"evaluations": results, "count": len(results)},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )


class CheckLabRangesTool(Tool):
    """Evaluate a single lab value against its reference range."""

    def __init__(self) -> None:
        # Pure-function engine; no external client required.
        pass

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="check_lab_ranges",
            description=(
                "Evaluate a single laboratory value against its reference range. "
                "Supports sex-specific ranges for hemoglobin, hematocrit, RBC "
                "and creatinine."
            ),
            parameters=[
                ToolParameter(
                    name="test_name",
                    type="string",
                    description="Lab test catalogue key, e.g. 'hemoglobin', 'sodium'",
                ),
                ToolParameter(
                    name="value",
                    type="number",
                    description="Measured lab value",
                ),
                ToolParameter(
                    name="unit",
                    type="string",
                    description="Unit of the measured value (optional)",
                    required=False,
                ),
                ToolParameter(
                    name="gender",
                    type="string",
                    description="Patient gender for sex-specific ranges: 'male' or 'female'",
                    required=False,
                    enum=["male", "female"],
                ),
            ],
            category="clinical_knowledge",
        )

    async def execute(
        self,
        test_name: str,
        value: float,
        unit: str | None = None,
        gender: str = "male",
    ) -> ToolResult:
        start = time.time()
        try:
            from doctoragent.clinical.safety.reference_ranges import evaluate_lab_value

            result = evaluate_lab_value(test_name, value, unit=unit, gender=gender)
            return ToolResult(
                success=True,
                data={"evaluation": result},
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                tool_name=self.definition.name,
            )

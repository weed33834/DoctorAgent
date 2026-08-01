"""Clinical specialist agents.

Four :class:`~doctoragent.clinical.agents.base.ClinicalAgent` subclasses, each
bound to a focused clinical prompt and a subset of the clinical tool
registry. The subclasses inherit the full Plan/ReAct/Reflection pipeline
from :class:`~doctoragent.model.agent.Agent` — they only specialise the prompt
template, the tool subset and a few config knobs (``max_iterations``,
``temperature``).

A small helper builds a constrained :class:`~doctoragent.model.tools.ToolRegistry`
containing only the requested tool names, so each specialist "sees" only its
own tools (fewer irrelevant tool calls, smaller prompt, safer behaviour).
"""

from __future__ import annotations

from typing import Any

from doctoragent.clinical.agents.base import ClinicalAgent
from doctoragent.clinical.agents.prompts import (
    DOCUMENTATION_AGENT_PROMPT,
    DRUG_SAFETY_AGENT_PROMPT,
    HISTORY_AGENT_PROMPT,
    LITERATURE_AGENT_PROMPT,
)
from doctoragent.clinical.agents.schemas import (
    DocumentationResult,
    DrugSafetyResult,
    LiteratureResult,
    PatientHistoryResult,
)
from doctoragent.clinical.safety import ClinicalGuardrails
from doctoragent.model.agent import AgentConfig
from doctoragent.model.tools import ToolRegistry

__all__ = [
    "DocumentationAgent",
    "DrugSafetyAgent",
    "LiteratureAgent",
    "PatientHistoryAgent",
]


def build_sub_registry(source: ToolRegistry, tool_names: list[str]) -> ToolRegistry:
    """Return a new :class:`ToolRegistry` with only *tool_names* registered.

    Tools are looked up by name on *source* and re-registered on a fresh
    registry; missing names are silently skipped so a specialist degrades
    gracefully when a dependency is not configured.
    """
    sub = ToolRegistry()
    for name in tool_names:
        tool = source.get(name)
        if tool is not None:
            sub.register(tool)
    return sub


def _specialist_config(
    *,
    max_iterations: int = 3,
    temperature: float = 0.3,
    **overrides: Any,
) -> AgentConfig:
    """Build an :class:`AgentConfig` tuned for a clinical specialist.

    Lower temperature favours deterministic, evidence-bound output.

    Planning and reflection are deliberately **disabled** for clinical
    specialists: they run inside a fixed, auditable DAG (rules → parallel
    specialists → documentation → guardrail) and must not self-replan via the
    LLM (that would break the fixed-DAG compliance property the clinical
    layer is built for) or loop on reflection (the guardrail layer already
    post-checks every answer for citations / forbidden content / PHI leakage).
    Plain ReAct — tool calls + observation + final answer — is the correct
    mode, and it keeps the per-specialist LLM call count bounded so the
    parallel fan-out actually finishes within a request timeout.

    ``max_iterations`` defaults to ``3``: each failed tool call typically
    triggers a long LLM fallback (~30-40s on hosted models), so 6 iterations
    blew past the 150s workflow budget. 3 iterations bounds a specialist to
    ~2 LLM calls + 1 final-synthesis call, keeping each parallel branch
    inside ~90s even when a knowledge tool is unavailable.
    """
    return AgentConfig(
        max_iterations=max_iterations,
        temperature=temperature,
        max_tokens=1024,
        enable_planning=False,
        enable_reflection=False,
        **overrides,
    )


class PatientHistoryAgent(ClinicalAgent):
    """病史解读专家 — reads FHIR resources and produces a structured summary."""

    system_prompt_template = HISTORY_AGENT_PROMPT
    output_model = PatientHistoryResult
    DEFAULT_TOOLS = [
        "read_patient_record",
        "read_medications",
        "read_allergies",
        "read_lab_results",
    ]

    def __init__(
        self,
        llm_provider: Any,
        clinical_registry: ToolRegistry,
        config: AgentConfig | None = None,
        tool_names: list[str] | None = None,
        guardrails: ClinicalGuardrails | None = None,
        memory_system: Any = None,
    ) -> None:
        sub_registry = build_sub_registry(clinical_registry, tool_names or self.DEFAULT_TOOLS)
        super().__init__(
            llm_provider=llm_provider,
            clinical_registry=sub_registry,
            config=config or _specialist_config(),
            memory_system=memory_system,
            guardrails=guardrails,
        )


class DrugSafetyAgent(ClinicalAgent):
    """用药安全专家 — checks DDI / allergies / vitals / lab ranges."""

    system_prompt_template = DRUG_SAFETY_AGENT_PROMPT
    output_model = DrugSafetyResult
    DEFAULT_TOOLS = [
        "check_drug_interactions",
        "check_vitals",
        "check_lab_ranges",
        "read_medications",
        "read_allergies",
    ]

    def __init__(
        self,
        llm_provider: Any,
        clinical_registry: ToolRegistry,
        config: AgentConfig | None = None,
        tool_names: list[str] | None = None,
        guardrails: ClinicalGuardrails | None = None,
        memory_system: Any = None,
    ) -> None:
        sub_registry = build_sub_registry(clinical_registry, tool_names or self.DEFAULT_TOOLS)
        super().__init__(
            llm_provider=llm_provider,
            clinical_registry=sub_registry,
            config=config or _specialist_config(temperature=0.2),
            memory_system=memory_system,
            guardrails=guardrails,
        )


class LiteratureAgent(ClinicalAgent):
    """文献检索专家 — searches PubMed and clinical guidelines."""

    system_prompt_template = LITERATURE_AGENT_PROMPT
    output_model = LiteratureResult
    DEFAULT_TOOLS = ["search_literature", "search_clinical_guidelines"]

    def __init__(
        self,
        llm_provider: Any,
        clinical_registry: ToolRegistry,
        config: AgentConfig | None = None,
        tool_names: list[str] | None = None,
        guardrails: ClinicalGuardrails | None = None,
        memory_system: Any = None,
    ) -> None:
        sub_registry = build_sub_registry(clinical_registry, tool_names or self.DEFAULT_TOOLS)
        super().__init__(
            llm_provider=llm_provider,
            clinical_registry=sub_registry,
            config=config or _specialist_config(),
            memory_system=memory_system,
            guardrails=guardrails,
        )


class DocumentationAgent(ClinicalAgent):
    """病历文书专家 — generates SOAP notes, ICD-10 codes, clinical notes."""

    system_prompt_template = DOCUMENTATION_AGENT_PROMPT
    output_model = DocumentationResult
    DEFAULT_TOOLS = ["generate_soap_note", "code_icd10", "write_clinical_note"]

    def __init__(
        self,
        llm_provider: Any,
        clinical_registry: ToolRegistry,
        config: AgentConfig | None = None,
        tool_names: list[str] | None = None,
        guardrails: ClinicalGuardrails | None = None,
        memory_system: Any = None,
    ) -> None:
        sub_registry = build_sub_registry(clinical_registry, tool_names or self.DEFAULT_TOOLS)
        super().__init__(
            llm_provider=llm_provider,
            clinical_registry=sub_registry,
            config=config or _specialist_config(),
            memory_system=memory_system,
            guardrails=guardrails,
        )

"""Clinical multi-agent layer.

Reuses the upstream :class:`~doctoragent.model.agent.Agent` engine (Plan / ReAct
/ Reflection) unchanged — this package only adds clinical system prompts,
guardrail-wrapped execution and a workflow orchestrator.

Two orchestration backends ship side-by-side:

* :mod:`doctoragent.clinical.agents.graph` — **LangGraph** compiled
  ``StateGraph`` (preferred when ``langgraph`` is installed). Declarative,
  immutable DAG, topology introspectable via
  :func:`get_clinical_graph_topology` for console visualisation.
* :mod:`doctoragent.clinical.agents.orchestrator` — hand-rolled
  ``asyncio.gather`` fan-out/fan-in fallback for minimal installs.

:func:`run_clinical_workflow` auto-selects the backend.

Public surface
--------------
* :class:`ClinicalAgent` — clinical base class (guardrailed ``run``).
* :class:`PatientHistoryAgent`, :class:`DrugSafetyAgent`,
  :class:`LiteratureAgent`, :class:`DocumentationAgent` — specialists.
* :class:`ClinicalOrchestrator`, :class:`ClinicalWorkflowResult` — workflow.
* :func:`run_clinical_workflow` — one-shot entry point.
* :func:`get_clinical_graph_topology` — graph DAG for console rendering.
* :func:`langgraph_available` — probe for the LangGraph backend.
"""

from __future__ import annotations

from doctoragent.clinical.agents.base import ClinicalAgent
from doctoragent.clinical.agents.graph import (
    get_clinical_graph_topology,
    langgraph_available,
)
from doctoragent.clinical.agents.orchestrator import (
    ClinicalOrchestrator,
    ClinicalWorkflowResult,
)
from doctoragent.clinical.agents.specialists import (
    DocumentationAgent,
    DrugSafetyAgent,
    LiteratureAgent,
    PatientHistoryAgent,
)
from doctoragent.clinical.agents.workflow import run_clinical_workflow

__all__ = [
    "ClinicalAgent",
    "ClinicalOrchestrator",
    "ClinicalWorkflowResult",
    "DocumentationAgent",
    "DrugSafetyAgent",
    "LiteratureAgent",
    "PatientHistoryAgent",
    "get_clinical_graph_topology",
    "langgraph_available",
    "run_clinical_workflow",
]

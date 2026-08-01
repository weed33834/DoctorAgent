"""One-shot clinical workflow entry point.

:func:`run_clinical_workflow` wires up the full clinical stack — clinical
tool registry, deterministic rule engine, LLM-output guardrails and the
multi-agent orchestrator — so a caller can obtain a
:class:`ClinicalWorkflowResult` in a single call.

Two orchestration backends are supported, selected automatically:

* **LangGraph** (preferred) — when the ``langgraph`` package is importable
  the workflow runs on a compiled, immutable ``StateGraph``. The graph
  topology is declared (rules → parallel specialists → documentation →
  guardrail), compiled once at first use, and cannot be re-planned by the
  LLM at runtime — preserving the fixed-DAG compliance property while
  removing the hand-rolled ``asyncio.gather`` fan-out/fan-in.
* **Hand-rolled fallback** — when ``langgraph`` is not installed the
  legacy :class:`ClinicalOrchestrator` (``asyncio.gather``) is used so the
  platform keeps working in minimal installs.

When ``llm_provider`` is ``None`` either backend degrades gracefully:
only the deterministic rule engine runs, the result is flagged for human
review and no exception is raised.
"""

from __future__ import annotations

import logging
from typing import Any

from doctoragent.clinical.agents.graph import (
    langgraph_available,
    run_clinical_workflow_graph,
)
from doctoragent.clinical.agents.orchestrator import (
    ClinicalOrchestrator,
    ClinicalWorkflowResult,
)
from doctoragent.clinical.safety import ClinicalGuardrails, ClinicalRuleEngine
from doctoragent.clinical.tools import create_clinical_registry
from doctoragent.observability.langfuse import observe

logger = logging.getLogger(__name__)

__all__ = ["run_clinical_workflow"]


@observe(
    name="clinical_workflow",
    # patient_context carries FHIR resources + vitals + labs → PHI.
    # Never capture inputs to Langfuse (an external service); the audit
    # logger records the decision trail locally with HMAC tamper-evidence.
    capture_input=False,
    capture_output=False,
)
async def run_clinical_workflow(
    patient_context: dict[str, Any],
    query: str,
    llm_provider: Any = None,
    fhir_client: Any = None,
    config: Any = None,
    audit_logger: Any = None,
) -> ClinicalWorkflowResult:
    """Run the full clinical workflow end-to-end.

    Parameters
    ----------
    patient_context:
        Patient data dict — may carry ``patient_id``, ``vitals``,
        ``labs``, ``medications`` and ``allergies``.
    query:
        The clinical question to answer.
    llm_provider:
        LLM provider for the specialist agents. ``None`` selects the
        degraded (rules-only) path.
    fhir_client:
        Optional FHIR client injected into the clinical tool registry.
    config:
        Optional :class:`~doctoragent.compliance_report.AegisConfig` (or any
        config object) forwarded to :func:`create_clinical_registry`.
    audit_logger:
        Optional :class:`~doctoragent.security.audit_log.AuditLogger`. When
        supplied, every clinical decision, blocking safety finding and
        guardrail action is recorded for FDA SaMD / 21 CFR Part 11 compliance.

    Returns
    -------
    ClinicalWorkflowResult
        Always returns — never raises for a missing LLM / FHIR client.
    """
    registry = create_clinical_registry(
        fhir_client=fhir_client,
        llm_provider=llm_provider,
        config=config,
    )
    rule_engine = ClinicalRuleEngine()
    guardrails = ClinicalGuardrails()

    # Prefer the LangGraph backend when available — the compiled graph is a
    # declarative, immutable DAG (rules → parallel specialists →
    # documentation → guardrail) that cannot be re-planned by the LLM,
    # preserving the clinical compliance property while removing the
    # hand-rolled asyncio.gather fan-out/fan-in. Falls back to the legacy
    # ClinicalOrchestrator in minimal installs without langgraph.
    if langgraph_available():
        try:
            return await run_clinical_workflow_graph(
                patient_context=patient_context,
                query=query,
                llm_provider=llm_provider,
                clinical_registry=registry,
                rule_engine=rule_engine,
                guardrails=guardrails,
                audit_logger=audit_logger,
            )
        except Exception:  # noqa: BLE001 — never break clinical path on a graph bug
            logger.warning(
                "LangGraph clinical workflow failed; falling back to hand-rolled orchestrator",
                exc_info=True,
            )

    orchestrator = ClinicalOrchestrator(
        llm_provider=llm_provider,
        clinical_registry=registry,
        rule_engine=rule_engine,
        guardrails=guardrails,
        audit_logger=audit_logger,
    )
    return await orchestrator.analyze(patient_context, query)

"""LangGraph-backed clinical multi-agent orchestration.

Replaces the hand-rolled ``asyncio.gather`` fan-out/fan-in in
:mod:`doctoragent.clinical.agents.orchestrator` with a declarative
``langgraph.graph.StateGraph`` so the clinical workflow is:

* **Declared, not scripted** — the DAG (rules → parallel specialists →
  documentation → guardrail review) is expressed as nodes + edges, not
  imperative control flow.
* **Immutable once compiled** — ``StateGraph.compile()`` returns a frozen
  graph that cannot be re-planned by the LLM at runtime. This preserves
  the compliance property the hand-rolled orchestrator was built for
  (an auditable fixed DAG, see orchestrator.py docstring).
* **Observable** — the compiled graph exposes ``get_graph().to_json()`` so
  the console can render the agent topology (see ``/agents/graph`` API).

The compiled graph is a drop-in for :meth:`ClinicalOrchestrator.analyze`:
same inputs (``patient_context``, ``query``), same output
(:class:`ClinicalWorkflowResult`). ``run_clinical_workflow`` switches to
the LangGraph path when the ``clinical`` extra ships ``langgraph``;
otherwise it falls back to the original orchestrator.

State schema
------------
``ClinicalGraphState`` is a typed dict that flows through the graph. Each
node reads the fields it needs and writes the fields it owns; LangGraph
merges state updates node-by-node (last-write-wins per key).
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from doctoragent.clinical.agents.orchestrator import (
    ClinicalWorkflowResult,
    _answer_of,
    _citations_of,
    _guardrail_action_of,
    _parse_literature,
    _structured_of,
)
from doctoragent.clinical.agents.prompts import CLINICAL_DISCLAIMER
from doctoragent.clinical.agents.schemas import (
    DocumentationResult,
    LiteratureResult,
)
from doctoragent.clinical.agents.specialists import (
    DocumentationAgent,
    DrugSafetyAgent,
    LiteratureAgent,
    PatientHistoryAgent,
)
from doctoragent.clinical.safety import (
    ClinicalGuardrails,
    ClinicalRuleEngine,
    GuardrailResult,
)
from doctoragent.model.tools import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "ClinicalGraphState",
    "run_clinical_workflow_graph",
    "langgraph_available",
]

# Severity levels that force human review regardless of the LLM guardrail.
_BLOCKING_SEVERITIES = ("critical", "contraindicated")


def langgraph_available() -> bool:
    """Return ``True`` when the ``langgraph`` package is importable."""
    try:
        import langgraph  # noqa: F401 — import probe
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class ClinicalGraphState(TypedDict, total=False):
    """Mutable state flowing through the clinical LangGraph.

    Fields are deliberately a superset of what any single node needs so the
    graph is resilient to a node being skipped (e.g. the LLM-disabled path
    never populates ``history_summary``).
    """

    patient_context: dict[str, Any]
    query: str
    llm_provider: Any
    clinical_registry: ToolRegistry
    rule_engine: ClinicalRuleEngine
    guardrails: ClinicalGuardrails
    audit_logger: Any
    # Node outputs.
    safety_findings: list[dict[str, Any]]
    history_res: Any
    drug_res: Any
    lit_res: Any
    doc_res: Any
    history_summary: str
    literature: list[dict[str, Any]]
    documentation: dict[str, Any] | None
    citations: list[str]
    sub_actions: list[str]
    combined_text: str
    guardrail_result: dict[str, Any]
    requires_human_review: bool


# ---------------------------------------------------------------------------
# Node implementations
#
# Each node is a plain ``async (state) -> dict`` function. Nodes return
# *partial* state updates; LangGraph merges them into the running state.
# ---------------------------------------------------------------------------


async def rules_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Deterministic safety floor — always runs first, LLM-independent."""
    rule_engine: ClinicalRuleEngine = state["rule_engine"]
    patient_context = state["patient_context"]
    rule_results = await rule_engine.evaluate_all(patient_context)
    safety_findings = [r.model_dump() for r in rule_results]
    return {"safety_findings": safety_findings}


async def history_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Patient-history specialist (fan-out branch 1)."""
    llm_provider = state["llm_provider"]
    registry = state["clinical_registry"]
    guardrails = state["guardrails"]
    if llm_provider is None:
        return {"history_res": {"answer": "", "citations": []}}
    agent = PatientHistoryAgent(llm_provider, registry, guardrails=guardrails)
    task = ClinicalGraphBuilder._history_task(state["patient_context"], state["query"])
    res = await agent.run_with_guardrails(task)
    return {"history_res": res}


async def drug_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Drug-safety specialist (fan-out branch 2)."""
    llm_provider = state["llm_provider"]
    registry = state["clinical_registry"]
    guardrails = state["guardrails"]
    if llm_provider is None:
        return {"drug_res": {"answer": "", "citations": []}}
    agent = DrugSafetyAgent(llm_provider, registry, guardrails=guardrails)
    task = ClinicalGraphBuilder._drug_task(state["patient_context"], state["query"])
    res = await agent.run_with_guardrails(task)
    return {"drug_res": res}


async def literature_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Literature specialist (fan-out branch 3)."""
    llm_provider = state["llm_provider"]
    registry = state["clinical_registry"]
    guardrails = state["guardrails"]
    if llm_provider is None:
        return {"lit_res": {"answer": "", "citations": []}}
    agent = LiteratureAgent(llm_provider, registry, guardrails=guardrails)
    task = ClinicalGraphBuilder._literature_task(state["query"])
    res = await agent.run_with_guardrails(task)
    return {"lit_res": res}


async def fanin_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Aggregate the three fan-out branches + parse structured outputs."""
    history_res = state.get("history_res")
    drug_res = state.get("drug_res")
    lit_res = state.get("lit_res")

    history_summary = _answer_of(history_res)
    citations: list[str] = []
    citations.extend(_citations_of(history_res))
    citations.extend(_citations_of(drug_res))
    citations.extend(_citations_of(lit_res))
    sub_actions: list[str] = []
    for res in (history_res, drug_res, lit_res):
        sub_actions.append(_guardrail_action_of(res))

    lit_structured = _structured_of(lit_res)
    if isinstance(lit_structured, LiteratureResult):
        literature = lit_structured.to_list()
    else:
        literature = _parse_literature(_answer_of(lit_res))

    return {
        "history_summary": history_summary,
        "citations": citations,
        "sub_actions": sub_actions,
        "literature": literature,
    }


async def documentation_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Conditional documentation draft (depends on history_summary)."""
    llm_provider = state["llm_provider"]
    if llm_provider is None:
        return {"doc_res": {"answer": "", "citations": []}}
    registry = state["clinical_registry"]
    guardrails = state["guardrails"]
    history_summary = state.get("history_summary", "")
    agent = DocumentationAgent(llm_provider, registry, guardrails=guardrails)
    task = ClinicalGraphBuilder._documentation_task(state["patient_context"], history_summary)
    try:
        res = await agent.run_with_guardrails(task)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Documentation agent failed: %s", exc)
        res = {"answer": "", "citations": []}
    return {"doc_res": res}


async def guardrail_node(state: ClinicalGraphState) -> dict[str, Any]:
    """Comprehensive guardrail review over the synthesised output."""
    llm_provider = state["llm_provider"]
    guardrails: ClinicalGuardrails = state["guardrails"]
    history_summary = state.get("history_summary", "")
    drug_text = _answer_of(state.get("drug_res"))
    lit_text = _answer_of(state.get("lit_res"))
    doc_text = _answer_of(state.get("doc_res"))
    combined_text = "\n".join(filter(None, [history_summary, drug_text, lit_text, doc_text]))

    if llm_provider is not None:
        guardrail_result = guardrails.evaluate(combined_text)
    else:
        guardrail_result = GuardrailResult(
            passed=False,
            warnings=["LLM 未配置，结果仅含确定性规则，需医生人工复核"],
            action="flag",
        )
    guardrail_dict = guardrail_result.model_dump()

    safety_findings = state.get("safety_findings", [])
    sub_actions = list(state.get("sub_actions", []))
    # Include the documentation sub-action (added after fan-in).
    doc_res = state.get("doc_res")
    sub_actions.append(_guardrail_action_of(doc_res))
    citations = list(state.get("citations", []))
    citations.extend(_citations_of(doc_res))

    blocking_finding = any(f.get("severity") in _BLOCKING_SEVERITIES for f in safety_findings)
    sub_blocked = any(a in ("block", "flag") for a in sub_actions)
    requires_review = (
        blocking_finding or sub_blocked or guardrail_result.action in ("block", "flag")
    )

    # Documentation structured payload.
    doc_structured = _structured_of(doc_res)
    if isinstance(doc_structured, DocumentationResult):
        documentation = doc_structured.to_draft_dict()
    else:
        documentation = {
            "draft": _answer_of(doc_res),
            "citations": _citations_of(doc_res),
        }

    # ── Audit ── best-effort, never raises into the workflow.
    audit_logger = state.get("audit_logger")
    patient_context = state["patient_context"]
    query = state["query"]
    if audit_logger is not None:
        try:
            if blocking_finding:
                audit_logger.log(
                    "clinical_safety_alert",
                    {
                        "patient_id": patient_context.get("patient_id"),
                        "findings": [
                            {
                                "severity": f.get("severity"),
                                "rule_type": f.get("rule_type"),
                                "finding": f.get("finding"),
                                "recommendation": f.get("recommendation"),
                            }
                            for f in safety_findings
                            if f.get("severity") in _BLOCKING_SEVERITIES
                        ],
                    },
                )
            if guardrail_result.action in ("block", "flag"):
                audit_logger.log(
                    "clinical_guardrail_action",
                    {
                        "patient_id": patient_context.get("patient_id"),
                        "action": guardrail_result.action,
                        "warnings": guardrail_result.warnings,
                        "sub_actions": sub_actions,
                    },
                )
            audit_logger.log(
                "clinical_decision",
                {
                    "patient_id": patient_context.get("patient_id"),
                    "query": query,
                    "requires_human_review": requires_review,
                    "blocking_finding": blocking_finding,
                    "guardrail_action": guardrail_result.action,
                    "finding_count": len(safety_findings),
                    "citation_count": len(citations),
                    "orchestration": "langgraph",
                },
            )
        except Exception:  # noqa: BLE001 — audit must never break clinical path
            logger.warning("audit log write failed", exc_info=True)

    return {
        "guardrail_result": guardrail_dict,
        "requires_human_review": requires_review,
        "documentation": documentation,
        "citations": citations,
        "sub_actions": sub_actions,
        "combined_text": combined_text,
    }


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


class ClinicalGraphBuilder:
    """Builds and compiles the clinical LangGraph.

    Factored as a class so the prompt-building helpers (which the original
    :class:`ClinicalOrchestrator` exposed as staticmethods) stay co-located
    with the graph that uses them — keeps the diff against the legacy
    orchestrator minimal.
    """

    @staticmethod
    def _history_task(patient_context: dict[str, Any], query: str) -> str:
        pid = patient_context.get("patient_id") or "(未提供)"
        return (
            f"患者ID: {pid}\n"
            f"临床问题: {query}\n"
            "请读取该患者的 FHIR 记录，整理结构化病史摘要与问题清单。"
        )

    @staticmethod
    def _drug_task(patient_context: dict[str, Any], query: str) -> str:
        meds = patient_context.get("medications") or []
        allergies = patient_context.get("allergies") or []
        return (
            f"临床问题: {query}\n"
            f"当前用药: {json.dumps(meds, ensure_ascii=False)}\n"
            f"过敏史: {json.dumps(allergies, ensure_ascii=False)}\n"
            "请核查药物相互作用、过敏交叉反应与生命体征/检验异常，附引证。"
        )

    @staticmethod
    def _literature_task(query: str) -> str:
        return f"临床问题: {query}\n请检索相关文献与临床指南，按证据等级排序并附 PMID/指南引证。"

    @staticmethod
    def _documentation_task(patient_context: dict[str, Any], history_summary: str) -> str:
        pid = patient_context.get("patient_id") or "(未提供)"
        return (
            f"患者ID: {pid}\n"
            f"病史摘要: {history_summary[:800]}\n"
            "请生成 SOAP 病历草稿与 ICD-10 编码建议，标注'待医生签发'。"
        )

    @staticmethod
    def build():
        """Compile and return the immutable clinical LangGraph.

        Topology::

            START → rules ─┬─► history ──┐
                           ├─► drug ─────┼─► fanin → documentation → guardrail → END
                           └─► literature┘

        ``rules`` fans out to the three specialists in parallel (LangGraph
        runs sibling edges concurrently); ``fanin`` aggregates; the rest is
        linear. The compiled graph is frozen — no node can be added or
        rewired at runtime, preserving the fixed-DAG compliance property.
        """
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(ClinicalGraphState)

        graph.add_node("rules", rules_node)
        graph.add_node("history", history_node)
        graph.add_node("drug", drug_node)
        graph.add_node("literature", literature_node)
        graph.add_node("fanin", fanin_node)
        graph.add_node("documentation", documentation_node)
        graph.add_node("guardrail", guardrail_node)

        # rules → three parallel specialists (fan-out).
        graph.add_edge(START, "rules")
        graph.add_edge("rules", "history")
        graph.add_edge("rules", "drug")
        graph.add_edge("rules", "literature")

        # Three specialists → fanin (LangGraph waits for all predecessors).
        graph.add_edge("history", "fanin")
        graph.add_edge("drug", "fanin")
        graph.add_edge("literature", "fanin")

        # fanin → documentation → guardrail → END (linear fan-in tail).
        graph.add_edge("fanin", "documentation")
        graph.add_edge("documentation", "guardrail")
        graph.add_edge("guardrail", END)

        return graph.compile()


# Module-level singleton: compile once, reuse across requests. Compilation
# is idempotent and the graph is immutable, so caching is safe.
_compiled_graph: Any = None


def _get_compiled_graph() -> Any:
    """Return the cached compiled clinical graph (built lazily)."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = ClinicalGraphBuilder.build()
    return _compiled_graph


def get_clinical_graph_topology() -> dict[str, Any]:
    """Return a JSON-serialisable description of the clinical graph topology.

    Used by the ``/agents/graph`` API so the console can render the agent
    DAG without importing LangGraph client-side. Shape::

        {"nodes": [...], "edges": [...], "engine": "langgraph"}
    """
    if not langgraph_available():
        return {
            "engine": "hand-rolled",
            "nodes": [
                {"id": "rules", "type": "deterministic"},
                {"id": "history", "type": "specialist"},
                {"id": "drug", "type": "specialist"},
                {"id": "literature", "type": "specialist"},
                {"id": "fanin", "type": "aggregator"},
                {"id": "documentation", "type": "specialist"},
                {"id": "guardrail", "type": "deterministic"},
            ],
            "edges": [
                {"from": "START", "to": "rules"},
                {"from": "rules", "to": "history"},
                {"from": "rules", "to": "drug"},
                {"from": "rules", "to": "literature"},
                {"from": "history", "to": "fanin"},
                {"from": "drug", "to": "fanin"},
                {"from": "literature", "to": "fanin"},
                {"from": "fanin", "to": "documentation"},
                {"from": "documentation", "to": "guardrail"},
                {"from": "guardrail", "to": "END"},
            ],
        }
    try:
        graph = _get_compiled_graph()
        # LangGraph's compiled graph exposes .get_graph() returning a
        # DrawableGraph with .to_json(). Fall back to the static shape if
        # the upstream API changes.
        drawable = graph.get_graph()
        raw = drawable.to_json()
        if isinstance(raw, str):
            parsed = json.loads(raw)
        else:
            parsed = raw
        return {"engine": "langgraph", "graph": parsed}
    except Exception:  # noqa: BLE001 — never break the API on a topology probe
        logger.debug("get_graph() failed; returning static topology", exc_info=True)
        return {
            "engine": "langgraph",
            "nodes": [
                {"id": "rules"},
                {"id": "history"},
                {"id": "drug"},
                {"id": "literature"},
                {"id": "fanin"},
                {"id": "documentation"},
                {"id": "guardrail"},
            ],
            "edges": [
                {"from": "rules", "to": "history"},
                {"from": "rules", "to": "drug"},
                {"from": "rules", "to": "literature"},
                {"from": "history", "to": "fanin"},
                {"from": "drug", "to": "fanin"},
                {"from": "literature", "to": "fanin"},
                {"from": "fanin", "to": "documentation"},
                {"from": "documentation", "to": "guardrail"},
            ],
        }


async def run_clinical_workflow_graph(
    patient_context: dict[str, Any],
    query: str,
    llm_provider: Any,
    clinical_registry: ToolRegistry,
    rule_engine: ClinicalRuleEngine,
    guardrails: ClinicalGuardrails,
    audit_logger: Any = None,
) -> ClinicalWorkflowResult:
    """Execute the compiled LangGraph and assemble a ``ClinicalWorkflowResult``.

    Mirrors :meth:`ClinicalOrchestrator.analyze` exactly so callers and
    tests cannot tell the two apart. The graph runs the same nodes in the
    same order; this function just marshals the final state into the
    pydantic result model.
    """
    graph = _get_compiled_graph()
    initial_state: ClinicalGraphState = {
        "patient_context": patient_context,
        "query": query,
        "llm_provider": llm_provider,
        "clinical_registry": clinical_registry,
        "rule_engine": rule_engine,
        "guardrails": guardrails,
        "audit_logger": audit_logger,
        "safety_findings": [],
        "citations": [],
        "sub_actions": [],
        "literature": [],
        "documentation": None,
        "history_summary": "",
        "combined_text": "",
    }
    final_state = await graph.ainvoke(initial_state)

    # De-duplicate citations while preserving order.
    citations = list(final_state.get("citations", []))
    seen: set[str] = set()
    unique_cites: list[str] = []
    for c in citations:
        if c and c not in seen:
            seen.add(c)
            unique_cites.append(c)

    history_summary = final_state.get("history_summary", "")
    if not history_summary and llm_provider is None:
        history_summary = "LLM 未配置，跳过 LLM 病史分析；仅确定性规则结果可用。"

    return ClinicalWorkflowResult(
        history_summary=history_summary,
        safety_findings=final_state.get("safety_findings", []),
        literature=final_state.get("literature", []),
        documentation=final_state.get("documentation"),
        guardrail_result=final_state.get("guardrail_result", {}),
        disclaimer=CLINICAL_DISCLAIMER,
        citations=unique_cites,
        requires_human_review=bool(final_state.get("requires_human_review", False)),
    )

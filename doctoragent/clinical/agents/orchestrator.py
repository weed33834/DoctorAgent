"""Clinical workflow orchestration.

Implements the parallel fan-out / fan-in + conditional branch + human-approval
pattern from the workflow-design playbook:

1. **Fan-out** — :class:`PatientHistoryAgent`, :class:`DrugSafetyAgent` and
   :class:`LiteratureAgent` run concurrently via :func:`asyncio.gather`.
2. **Fan-in** — their guardrailed sub-results are aggregated.
3. **Deterministic rules** — :meth:`ClinicalRuleEngine.evaluate_all` runs the
   pure-logic safety layer (vitals / labs / DDI / allergy / duplicate therapy)
   and its findings are merged into ``safety_findings``. Rule-engine output
   always wins over LLM suggestions.
4. **Documentation** — :class:`DocumentationAgent` drafts a SOAP / ICD-10
   note from the aggregated context.
5. **Guardrail review** — :class:`ClinicalGuardrails.evaluate` runs over the
   synthesised output; a ``block`` degrades the surfaced answer and forces
   ``requires_human_review``.

:class:`ClinicalOrchestrator` deliberately composes specialist agents rather
than subclassing :class:`~doctoragent.model.agent.OrchestratorAgent` — the latter
requires a ``task_store`` and decomposes tasks via LLM, whereas the clinical
workflow has a fixed, auditable DAG that should not be re-planned by a model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

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

__all__ = ["ClinicalOrchestrator", "ClinicalWorkflowResult"]

# Severity levels that force human review regardless of the LLM guardrail.
_BLOCKING_SEVERITIES = ("critical", "contraindicated")


class ClinicalWorkflowResult(BaseModel):
    """Structured outcome of a clinical workflow run."""

    history_summary: str = ""
    safety_findings: list[dict[str, Any]] = Field(default_factory=list)
    literature: list[dict[str, Any]] = Field(default_factory=list)
    documentation: dict[str, Any] | None = None
    guardrail_result: dict[str, Any] = Field(default_factory=dict)
    disclaimer: str = ""
    citations: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


def _answer_of(res: Any) -> str:
    """Extract the ``answer`` string from a specialist sub-result defensively."""
    if isinstance(res, Exception):
        return f"子 Agent 异常: {res}"
    if isinstance(res, dict):
        return str(res.get("answer") or "")
    return str(res) if res else ""


def _citations_of(res: Any) -> list[str]:
    if isinstance(res, dict):
        cites = res.get("citations") or []
        return [str(c) for c in cites]
    return []


def _guardrail_action_of(res: Any) -> str:
    if isinstance(res, dict):
        gr = res.get("guardrail_result") or {}
        return str(gr.get("action") or "allow")
    return "allow"


def _structured_of(res: Any) -> BaseModel | None:
    """Return the validated pydantic model a specialist populated, if any.

    ``run_with_guardrails`` stores the instructor-validated (or
    ``from_text``-parsed) instance under the ``structured`` key. When the
    structured path was unavailable (clinical extra not installed, guardrail
    blocked, or validation failed) the key is ``None`` and the caller falls
    back to the legacy raw-text contract.
    """
    if isinstance(res, dict):
        return res.get("structured")  # type: ignore[return-value]
    return None


class ClinicalOrchestrator:
    """Compose the clinical specialist agents into a fan-out/fan-in workflow.

    Parameters
    ----------
    llm_provider:
        LLM provider shared by every specialist. ``None`` triggers the
        degraded path (deterministic rules only, no LLM calls).
    clinical_registry:
        :class:`~doctoragent.model.tools.ToolRegistry` produced by
        :func:`~doctoragent.clinical.tools.create_clinical_registry`; each
        specialist builds its own sub-registry from it.
    rule_engine, guardrails:
        Optional pre-built safety layers. Defaults are
        :class:`ClinicalRuleEngine` / :class:`ClinicalGuardrails`.
    """

    def __init__(
        self,
        llm_provider: Any,
        clinical_registry: ToolRegistry,
        rule_engine: ClinicalRuleEngine | None = None,
        guardrails: ClinicalGuardrails | None = None,
        audit_logger: Any = None,
        self_evolution_engine: Any = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.clinical_registry = clinical_registry
        self.rule_engine = rule_engine or ClinicalRuleEngine()
        self.guardrails = guardrails or ClinicalGuardrails()
        # Optional tamper-evident audit logger. When injected (e.g. by the
        # API server) every clinical decision, blocking safety finding and
        # guardrail action is recorded so the full decision chain is
        # reconstructable — required by FDA SaMD / 21 CFR Part 11 / HIPAA.
        self.audit_logger = audit_logger
        # Optional self-evolution engine. When injected the orchestrator
        # recalls past experiences for the query (injecting learned lessons
        # / optimised prompts into the specialist tasks) and stores a
        # trajectory-derived experience after each run so subsequent queries
        # of the same pattern benefit. Failures inside the engine NEVER
        # break the clinical path — self-evolution is a best-effort optimiser.
        self.self_evolution_engine = self_evolution_engine

    def _audit(self, event_type: str, details: dict[str, Any]) -> None:
        """Best-effort write to the audit log; never raises into the workflow."""
        if self.audit_logger is None:
            return
        try:
            self.audit_logger.log(event_type, details)
        except Exception:  # noqa: BLE001 — audit must never break clinical path
            logger.warning("audit log write failed for %s", event_type, exc_info=True)

    # ------------------------------------------------------------------
    # Self-evolution helpers (best-effort, never breaks the clinical path)
    # ------------------------------------------------------------------

    def _recall_experiences(self, query: str) -> list[Any]:
        """Recall past experiences relevant to *query*.

        Returns an empty list when no engine is wired or the recall fails.
        """
        if self.self_evolution_engine is None:
            return []
        try:
            return self.self_evolution_engine.recall_experiences(query, top_k=3)
        except Exception:  # noqa: BLE001
            logger.warning("self-evolution recall failed", exc_info=True)
            return []

    @staticmethod
    def _experience_preamble(experiences: list[Any]) -> str:
        """Build a short preamble from recalled experiences to prepend to
        specialist task prompts.

        Each preamble line is a single sentence (lesson or optimised-prompt
        hint) so it cannot derail the specialist's own instructions. The
        preamble explicitly labels itself as ``历史经验（仅供参考，需医生复核）``
        so the LLM cannot present it as authoritative.
        """
        if not experiences:
            return ""
        lines: list[str] = []
        for exp in experiences:
            lessons = getattr(exp, "lessons", None) or []
            for lesson in lessons[:2]:
                if isinstance(lesson, str) and lesson.strip():
                    lines.append(f"- {lesson.strip()}")
            if len(lines) >= 4:
                break
        if not lines:
            return ""
        return (
            "【历史经验（仅供参考，需医生复核）】\n"
            + "\n".join(lines)
            + "\n（以上为系统从历史查询中归纳的经验，不构成临床建议。）\n"
        )

    def _store_trajectory_experience(
        self,
        query: str,
        result: ClinicalWorkflowResult,
        specialist_actions: list[str],
    ) -> None:
        """Analyse the completed workflow and store an experience.

        Builds a minimal trajectory-shaped dict the
        :class:`SelfEvolutionEngine` can consume (``steps`` +
        ``total_tool_calls`` + ``query``), runs ``analyze_trajectory`` and
        persists the resulting experience. All failures are swallowed.
        """
        if self.self_evolution_engine is None:
            return
        try:
            # Synthesise a trajectory: one "answer" step carrying the
            # combined output, plus one "action" step per specialist that
            # ran. This is enough signal for the engine's outcome
            # classifier (has_answer / has_error) without coupling the
            # clinical workflow to the agent's internal trajectory types.
            steps: list[dict[str, Any]] = []
            for action in specialist_actions:
                steps.append(
                    {
                        "step_type": "action",
                        "tool_name": "clinical_specialist",
                        "tool_result": {"success": action != "block"},
                        "content": "",
                    }
                )
            steps.append(
                {
                    "step_type": "answer",
                    "content": result.history_summary or "",
                }
            )
            trajectory = {
                "query": query,
                "steps": steps,
                "total_tool_calls": len(specialist_actions),
            }
            pattern = self.self_evolution_engine.analyze_trajectory(trajectory)
            # Persist as an experience if the engine exposes store_experience.
            store = getattr(self.self_evolution_engine, "store_experience", None)
            if callable(store):
                from doctoragent.model.self_evolution import (
                    ExecutionOutcome,
                    Experience,
                )

                if pattern.success_count:
                    outcome_value = ExecutionOutcome.SUCCESS
                elif pattern.failure_count:
                    outcome_value = ExecutionOutcome.FAILURE
                else:
                    outcome_value = ExecutionOutcome.PARTIAL
                exp = Experience(
                    query=query,
                    query_pattern=pattern.pattern_description,
                    pattern_description=pattern.pattern_description,
                    outcome=outcome_value,
                    lessons=pattern.common_errors or [],
                    optimized_prompt=pattern.optimized_prompt,
                    recommended_tools=pattern.common_tools,
                )
                store(exp)
        except Exception:  # noqa: BLE001
            logger.warning("self-evolution store failed", exc_info=True)

    # ------------------------------------------------------------------
    # Task builders — give each specialist a focused prompt.
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def analyze(self, patient_context: dict[str, Any], query: str) -> ClinicalWorkflowResult:
        """Run the full clinical workflow and return a structured result."""
        # Step 0 (self-evolution recall): best-effort lookup of past
        # experiences for this query. The lessons are prepended to the
        # specialist task prompts as a non-authoritative preamble.
        recalled = self._recall_experiences(query)
        preamble = self._experience_preamble(recalled)

        # Step 3 (deterministic rules) always runs — it is the safety floor.
        rule_results = await self.rule_engine.evaluate_all(patient_context)
        safety_findings: list[dict[str, Any]] = [r.model_dump() for r in rule_results]

        history_summary = ""
        literature: list[dict[str, Any]] = []
        documentation: dict[str, Any] | None = None
        citations: list[str] = []
        combined_text = ""
        sub_actions: list[str] = []

        if self.llm_provider is not None:
            # Step 1: parallel fan-out — three specialists concurrently.
            history_agent = PatientHistoryAgent(
                self.llm_provider, self.clinical_registry, guardrails=self.guardrails
            )
            drug_agent = DrugSafetyAgent(
                self.llm_provider, self.clinical_registry, guardrails=self.guardrails
            )
            lit_agent = LiteratureAgent(
                self.llm_provider, self.clinical_registry, guardrails=self.guardrails
            )

            history_res, drug_res, lit_res = await asyncio.gather(
                history_agent.run_with_guardrails(
                    preamble + self._history_task(patient_context, query)
                ),
                drug_agent.run_with_guardrails(self._drug_task(patient_context, query)),
                lit_agent.run_with_guardrails(self._literature_task(query)),
                return_exceptions=True,
            )

            # Step 2: fan-in — aggregate sub-results.
            history_summary = _answer_of(history_res)
            citations.extend(_citations_of(history_res))
            citations.extend(_citations_of(drug_res))
            citations.extend(_citations_of(lit_res))
            for res in (history_res, drug_res, lit_res):
                sub_actions.append(_guardrail_action_of(res))

            # Literature: prefer the instructor-validated structured output;
            # fall back to best-effort text parsing when unavailable.
            lit_structured = _structured_of(lit_res)
            if isinstance(lit_structured, LiteratureResult):
                literature = lit_structured.to_list()
            else:
                literature = _parse_literature(_answer_of(lit_res))

            # Step 4: documentation (conditional on having a history summary).
            doc_agent = DocumentationAgent(
                self.llm_provider, self.clinical_registry, guardrails=self.guardrails
            )
            try:
                doc_res = await doc_agent.run_with_guardrails(
                    self._documentation_task(patient_context, history_summary)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Documentation agent failed: %s", exc)
                doc_res = {"answer": "", "citations": []}
            # Documentation: prefer the structured SOAP + ICD-10 payload;
            # fall back to the raw-text draft when structured output is absent.
            doc_structured = _structured_of(doc_res)
            if isinstance(doc_structured, DocumentationResult):
                documentation = doc_structured.to_draft_dict()
            else:
                documentation = {
                    "draft": _answer_of(doc_res),
                    "citations": _citations_of(doc_res),
                }
            citations.extend(_citations_of(doc_res))
            sub_actions.append(_guardrail_action_of(doc_res))

            combined_text = "\n".join(
                filter(
                    None,
                    [
                        history_summary,
                        _answer_of(drug_res),
                        _answer_of(lit_res),
                        _answer_of(doc_res),
                    ],
                )
            )
        else:
            history_summary = "LLM 未配置，跳过 LLM 病史分析；仅确定性规则结果可用。"
            combined_text = "LLM 未配置"

        # Step 5: comprehensive guardrail review over the synthesised output.
        if self.llm_provider is not None:
            guardrail_result = self.guardrails.evaluate(combined_text)
        else:
            guardrail_result = GuardrailResult(
                passed=False,
                warnings=["LLM 未配置，结果仅含确定性规则，需医生人工复核"],
                action="flag",
            )
        guardrail_dict = guardrail_result.model_dump()

        # Human review is required when any blocking finding fired, any
        # specialist was blocked/flagged, or the final guardrail did not allow.
        blocking_finding = any(f.get("severity") in _BLOCKING_SEVERITIES for f in safety_findings)
        sub_blocked = any(a in ("block", "flag") for a in sub_actions)
        requires_review = (
            blocking_finding or sub_blocked or guardrail_result.action in ("block", "flag")
        )

        # ── Audit: record clinical safety alerts and guardrail actions so the
        #    decision chain is reconstructable (FDA SaMD / 21 CFR Part 11). ──
        if blocking_finding:
            self._audit(
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
            self._audit(
                "clinical_guardrail_action",
                {
                    "patient_id": patient_context.get("patient_id"),
                    "action": guardrail_result.action,
                    "warnings": guardrail_result.warnings,
                    "sub_actions": sub_actions,
                },
            )

        # De-duplicate citations while preserving order.
        seen: set[str] = set()
        unique_cites: list[str] = []
        for c in citations:
            if c and c not in seen:
                seen.add(c)
                unique_cites.append(c)

        result = ClinicalWorkflowResult(
            history_summary=history_summary,
            safety_findings=safety_findings,
            literature=literature,
            documentation=documentation,
            guardrail_result=guardrail_dict,
            disclaimer=CLINICAL_DISCLAIMER,
            citations=unique_cites,
            requires_human_review=requires_review,
        )

        # Always record the completed clinical decision so the full chain
        # (rules → LLM → guardrail → human-review flag) is reconstructable.
        self._audit(
            "clinical_decision",
            {
                "patient_id": patient_context.get("patient_id"),
                "query": query,
                "requires_human_review": requires_review,
                "blocking_finding": blocking_finding,
                "guardrail_action": guardrail_result.action,
                "finding_count": len(safety_findings),
                "citation_count": len(unique_cites),
            },
        )

        # Step 6 (self-evolution store): persist a trajectory-derived
        # experience so future queries of the same pattern can recall the
        # lessons. Best-effort — failures never break the clinical path.
        self._store_trajectory_experience(query, result, sub_actions)

        return result


def _parse_literature(text: str) -> list[dict[str, Any]]:
    """Best-effort extraction of a literature list from an LLM answer.

    The literature agent is asked to emit a JSON array; if parsing fails the
    raw text is wrapped in a single-entry list so the result is never empty
    when the agent produced output.
    """
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (json.JSONDecodeError, TypeError):
        pass
    return [{"summary": text[:500]}]

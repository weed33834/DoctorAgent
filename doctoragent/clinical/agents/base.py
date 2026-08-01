"""Clinical agent base class.

:class:`ClinicalAgent` inherits the upstream :class:`~doctoragent.model.agent.Agent`
verbatim — the Plan/ReAct/Reflection pipeline, memory integration, parallel
tool dispatch and error recovery are all reused unchanged. The only overrides
are:

* :meth:`_build_system_prompt` injects the clinical system-prompt template
  (with the clinical disclaimer and behaviour constraints) instead of the
  default document-management prompt.
* :meth:`run_with_guardrails` wraps :meth:`run` with
  :class:`~doctoragent.clinical.safety.ClinicalGuardrails` so every LLM answer is
  post-checked for forbidden content / PHI leakage / missing citations before
  it is surfaced. A ``block`` action degrades the output to a "需医生确认"
  placeholder rather than surfacing the unsafe text.

All LLM access is defensive: when ``llm_provider`` is ``None`` the agent
returns a degraded result instead of raising.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel

from doctoragent.clinical.agents.prompts import CLINICAL_DISCLAIMER, CLINICAL_SYSTEM_PROMPT
from doctoragent.clinical.agents.structured import STRUCTURED_AVAILABLE, structured_complete
from doctoragent.clinical.safety import ClinicalGuardrails, GuardrailResult
from doctoragent.model.agent import (
    MEMORY_PROMPT_SECTION,
    SHORT_TERM_PROMPT_SECTION,
    Agent,
    AgentConfig,
)
from doctoragent.model.tools import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = ["ClinicalAgent"]


# Lightweight citation extractor — surfaces traceable references (PMID / DOI /
# FHIR Resource/id) the LLM emitted so callers can attach them to the result.
_CITATION_RE = re.compile(
    r"(PMID:?\s*\d+|doi:?\s*10\.\d{4,}/\S+|\b[A-Z][a-zA-Z]+/\d[\w.-]*\b)",
    re.IGNORECASE,
)


def _extract_citations(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _CITATION_RE.findall(text):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# Prompt suffix asking the model to re-emit its prior analysis as a structured
# JSON object. Used by :meth:`ClinicalAgent._structured_synthesize` when the
# cheap ``from_text()`` parse of the raw answer fails — instructor then
# constrains the corrective call to the specialist's ``output_model``.
_STRUCTURED_EXTRACT_PROMPT = (
    "将以下临床分析结果转换为结构化 JSON。仅输出 JSON 对象，不要额外解释。\n"
    "保留所有临床发现、引证与建议字段；缺失字段使用空值。\n\n"
    "待结构化的分析：\n{analysis}"
)


class ClinicalAgent(Agent):
    """Agent specialised for clinical decision support.

    Subclasses set :attr:`system_prompt_template` to one of the clinical
    prompts from :mod:`doctoragent.clinical.agents.prompts`. The template must
    contain a single ``{tools_description}`` placeholder.

    Subclasses may also set :attr:`output_model` to a pydantic model
    representing the structured-JSON shape the prompt advertises; when set,
    :meth:`run_with_guardrails` populates ``result["structured"]`` with a
    validated instance (via :mod:`doctoragent.clinical.agents.structured`
    / instructor, with a ``from_text()`` fallback when the instructor
    optional deps are not installed).
    """

    #: Prompt template used by :meth:`_build_system_prompt`. Subclasses
    #: override this; the base class uses the generic clinical prompt.
    system_prompt_template: str = CLINICAL_SYSTEM_PROMPT

    #: Optional pydantic model for structured output validation. When
    #: ``None`` (the base-class default) ``run_with_guardrails`` returns
    #: only the raw-text contract (legacy behaviour).
    output_model: type[BaseModel] | None = None

    def __init__(
        self,
        llm_provider: Any,
        clinical_registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_system: Any = None,
        task_store: Any = None,
        guardrails: ClinicalGuardrails | None = None,
    ) -> None:
        super().__init__(
            llm_provider=llm_provider,
            tool_registry=clinical_registry,
            config=config,
            memory_system=memory_system,
            task_store=task_store,
        )
        # enable_multi_agent must stay off for clinical specialists — they are
        # composed by the ClinicalOrchestrator rather than self-decomposing.
        self.config.enable_multi_agent = False
        self.guardrails = guardrails or ClinicalGuardrails()

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_system_prompt(
        self,
        memory_context: str = "",
        short_term_history: str = "",
    ) -> str:
        """Build the system prompt using the clinical template.

        Mirrors the upstream implementation but substitutes the clinical
        prompt for the default document-management one. Memory and
        short-term-history sections are appended identically so the
        inherited memory layer keeps working.
        """
        tools_desc = self._format_tools_for_prompt()
        prompt = self.system_prompt_template.format(tools_description=tools_desc)
        if memory_context:
            prompt += MEMORY_PROMPT_SECTION.format(memory_context=memory_context)
        if short_term_history:
            prompt += SHORT_TERM_PROMPT_SECTION.format(short_term_history=short_term_history)
        return prompt

    # ------------------------------------------------------------------
    # Guardrailed execution
    # ------------------------------------------------------------------

    async def run_with_guardrails(self, task: str) -> dict[str, Any]:
        """Run the agent and post-check the output with clinical guardrails.

        Returns a dict with ``answer``, ``guardrail_result`` (serialised),
        ``citations``, ``disclaimer``, ``degraded`` and (when the subclass
        sets :attr:`output_model`) ``structured`` — a validated pydantic
        instance or ``None`` when structured validation was unavailable or
        failed. When the guardrail action is ``block`` the answer is
        replaced with a "需医生确认" placeholder so the unsafe text is never
        surfaced. When ``llm_provider`` is ``None`` a degraded result is
        returned without raising.
        """
        if self.llm_provider is None:
            degraded = GuardrailResult(
                passed=False,
                warnings=["LLM 未配置，无法执行分析"],
                action="flag",
            )
            return {
                "answer": "LLM 未配置，无法执行临床分析，请配置 LLM 后重试。",
                "guardrail_result": degraded.model_dump(),
                "citations": [],
                "disclaimer": CLINICAL_DISCLAIMER,
                "degraded": True,
                "structured": None,
            }

        try:
            answer = await self.run(task)
        except Exception as exc:  # noqa: BLE001 — defensive: never surface raw
            logger.warning("ClinicalAgent.run failed: %s", exc)
            answer = ""

        if not answer:
            answer = "Agent 未能产生有效输出，需医生人工评估。"

        guardrail_result = self.guardrails.evaluate(answer)
        citations = _extract_citations(answer)

        if guardrail_result.action == "block":
            # Never surface blocked content; route for human confirmation.
            answer = "该输出已被安全护栏拦截，需医生确认后使用。"

        # Structured-output validation: try the cheap ``from_text()`` parse
        # first (the prompt already asks for JSON), then fall back to an
        # instructor-constrained corrective call when the cheap parse fails.
        structured: BaseModel | None = None
        if self.output_model is not None and guardrail_result.action != "block":
            structured = await self._structured_synthesize(answer)

        return {
            "answer": answer,
            "guardrail_result": guardrail_result.model_dump(),
            "citations": citations,
            "disclaimer": CLINICAL_DISCLAIMER,
            "degraded": False,
            "structured": structured,
        }

    async def _structured_synthesize(self, answer: str) -> BaseModel | None:
        """Validate *answer* into :attr:`output_model`.

        Two-tier strategy:

        1. **Cheap parse** — call ``output_model.from_text(answer)``. The
           specialist prompts already request a JSON shape, so most of the
           time the model emits parseable JSON and no extra LLM call is
           needed.
        2. **Instructor corrective call** — when the cheap parse fails AND
           the ``instructor``/``openai`` optional deps are installed, issue
           one constrained call asking the model to re-emit the analysis as
           the structured schema. This is the "use external libraries"
           path: instructor enforces the pydantic schema via OpenAI tool
           calling, with its own validation-retry loop.

        Returns ``None`` (never raises) when:
        * :attr:`output_model` is ``None`` (no schema declared),
        * the cheap parse succeeds (no need for the corrective call),
        * the corrective call is unavailable or fails — callers fall back
          to the raw-text ``answer``.
        """
        if self.output_model is None or not answer:
            return None
        # Tier 1: cheap parse of the JSON the prompt requested.
        from_text = getattr(self.output_model, "from_text", None)
        if callable(from_text):
            try:
                parsed = from_text(answer)
            except Exception:  # noqa: BLE001 — defensive
                parsed = None
            if parsed is not None:
                return parsed  # type: ignore[return-value]
        # Tier 2: instructor corrective call (only if optional deps present).
        if not STRUCTURED_AVAILABLE or self.llm_provider is None:
            return None
        # The provider may not be an OpenAICompatibleProvider (e.g. a mock in
        # tests); guard the structured_complete call so a non-conforming
        # provider never crashes the workflow.
        provider = self.llm_provider
        if not hasattr(provider, "connection"):
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "你是临床结构化输出助手。将给定的临床分析转换为指定的 "
                    "JSON 结构，保留所有发现与引证。"
                ),
            },
            {
                "role": "user",
                "content": _STRUCTURED_EXTRACT_PROMPT.format(analysis=answer),
            },
        ]
        try:
            result = await structured_complete(
                provider,
                messages,
                self.output_model,
                max_retries=2,
            )
        except Exception:  # noqa: BLE001 — never block the clinical workflow
            logger.warning(
                "Structured synthesis failed for %s; falling back to text",
                self.__class__.__name__,
                exc_info=True,
            )
            return None
        return result  # type: ignore[return-value]

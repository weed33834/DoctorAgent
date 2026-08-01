"""Pydantic schemas for clinical specialist agent structured output.

These models formalise the JSON shapes the four specialist prompts
(:mod:`doctoragent.clinical.agents.prompts`) already advertise but that were
previously un-enforced — the agents returned raw ``str`` and the
orchestrator discarded the structure. With :mod:`structured` (instructor)
wired into :meth:`ClinicalAgent.run_with_guardrails`, the LLM is now
constrained to emit JSON that validates against these models, and any
validation failure degrades gracefully to the raw-text fallback rather
than crashing the workflow.

Design
------
* All fields have sensible defaults (empty strings / empty lists) so a
  partial LLM output (e.g. the model forgot ``citations``) still validates
  rather than forcing a full retry. This keeps the clinical workflow
  resilient — a missing field is far less harmful than a crashed agent.
* ``model_config = ConfigDict(extra="allow")`` accepts vendor extensions
  the LLM may emit (e.g. ``confidence`` on the history result) without
  breaking validation.
* ``LiteratureResult.results`` and ``DocumentationResult.icd10`` use
  nested pydantic models so instructor can emit the full nested schema as
  a tool-call function, not just top-level primitives.
* A ``from_text(text)`` classmethod on each model lets the orchestrator
  fall back to best-effort JSON parsing of raw text when the instructor
  path is unavailable (clinical extra not installed). This preserves the
  old behaviour as a safety net.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "DocumentationResult",
    "DrugSafetyResult",
    "LiteratureItem",
    "LiteratureResult",
    "PatientHistoryResult",
    "SoapNote",
]


# --------------------------------------------------------------------------- #
# Shared base
# --------------------------------------------------------------------------- #
class _SpecialistResult(BaseModel):
    """Base for specialist outputs — permissive + ``citations`` field.

    All concrete models inherit the ``citations: list[str]`` field so the
    orchestrator can harvest traceable references uniformly.
    """

    model_config = ConfigDict(extra="allow")

    citations: list[str] = Field(
        default_factory=list,
        description="Traceable references (PMID / DOI / FHIR Resource/id).",
    )

    @classmethod
    def from_text(cls, text: str) -> _SpecialistResult | None:
        """Best-effort parse of a raw LLM text into this model.

        Tolerates fenced ```` ```json ... ``` ```` blocks and bare JSON
        objects. Returns ``None`` when the text is not parseable as this
        model's shape — callers should then fall back to wrapping the
        raw text (preserving the pre-instructor behaviour).
        """
        if not isinstance(text, str) or not text.strip():
            return None
        candidate = _extract_json_block(text)
        if candidate is None:
            return None
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls.model_validate(data)  # type: ignore[return-value]
        except ValidationError:
            return None


# Fenced-JSON extractor shared by all ``from_text`` classmethods. Tries
# ```` ```json {...} ``` ```` first, then ```` ``` {...} ``` ````, then a
# bare ``{...}`` substring.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s```", re.DOTALL)
_BARE_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json_block(text: str) -> str | None:
    """Return the first JSON object/array substring in *text*."""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    m = _BARE_RE.search(text)
    if m:
        return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Patient history
# --------------------------------------------------------------------------- #
class PatientHistoryResult(_SpecialistResult):
    """Output schema for :class:`PatientHistoryAgent`.

    Matches the JSON shape advertised by ``HISTORY_AGENT_PROMPT``::

        {"summary": "...", "problems": [...], "timeline": [...],
         "citations": [...]}
    """

    summary: str = Field(
        default="",
        description="One-paragraph structured history summary (zh-CN).",
    )
    problems: list[str] = Field(
        default_factory=list,
        description="Active problem list (chronic conditions, recent events).",
    )
    timeline: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Chronological events: ``[{date, event, source?}]``.",
    )


# --------------------------------------------------------------------------- #
# Drug safety
# --------------------------------------------------------------------------- #
class DrugSafetyResult(_SpecialistResult):
    """Output schema for :class:`DrugSafetyAgent`.

    Matches::

        {"findings": [...], "severity": "info|warning|critical|contraindicated",
         "recommendation": "...", "citations": [...]}
    """

    findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Safety findings (DDI, allergy, vitals/labs out of range).",
    )
    severity: str = Field(
        default="info",
        description="Overall acuity: info | warning | critical | contraindicated.",
    )
    recommendation: str = Field(
        default="",
        description="Actionable recommendation, gated behind '需医生复核'.",
    )


# --------------------------------------------------------------------------- #
# Literature
# --------------------------------------------------------------------------- #
class LiteratureItem(BaseModel):
    """A single literature / guideline reference."""

    model_config = ConfigDict(extra="allow")

    title: str = Field(default="", description="Article / guideline title.")
    source: str = Field(
        default="",
        description="Traceable reference (PMID:..., DOI:..., guideline org).",
    )
    evidence_level: str = Field(
        default="",
        description="Evidence grade: guideline | systematic-review | rct | observational.",
    )
    summary: str = Field(
        default="",
        description="One-line relevance summary for the clinician.",
    )


class LiteratureResult(_SpecialistResult):
    """Output schema for :class:`LiteratureAgent`.

    Matches::

        {"results": [{"title": "...", "source": "PMID:...",
                       "evidence_level": "..."}],
         "summary": "...", "citations": [...]}
    """

    results: list[LiteratureItem] = Field(
        default_factory=list,
        description="Ranked literature / guideline references.",
    )
    summary: str = Field(
        default="",
        description="Synthesised evidence summary across the results.",
    )

    def to_list(self) -> list[dict[str, Any]]:
        """Flatten to the ``list[dict]`` shape the orchestrator expects.

        Preserves backward compatibility with the pre-instructor
        ``_parse_literature`` return contract so the CDS Hooks translator
        and API schemas don't need to change.
        """
        return [item.model_dump(exclude_none=True) for item in self.results]


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #
class SoapNote(BaseModel):
    """A SOAP note draft."""

    model_config = ConfigDict(extra="allow")

    subjective: str = Field(default="", description="Patient-reported history.")
    objective: str = Field(default="", description="Exam + vitals + labs.")
    assessment: str = Field(
        default="",
        description="Clinical impression (no definitive diagnosis; uses '疑似/考虑').",
    )
    plan: str = Field(
        default="",
        description="Proposed plan, gated behind '待医生签发'.",
    )


class DocumentationResult(_SpecialistResult):
    """Output schema for :class:`DocumentationAgent`.

    Matches::

        {"soap": {"subjective": "...", "objective": "...",
                  "assessment": "...", "plan": "..."},
         "icd10": [...], "citations": [...]}
    """

    soap: SoapNote = Field(
        default_factory=SoapNote,
        description="SOAP note draft (always a draft — never a signed note).",
    )
    icd10: list[str] = Field(
        default_factory=list,
        description="Candidate ICD-10-CM codes (to be validated by the terminology layer).",
    )

    def to_draft_dict(self) -> dict[str, Any]:
        """Flatten to the ``{"draft": ..., "soap": ..., "icd10": ...}`` shape.

        Preserves backward compatibility with the orchestrator's
        ``documentation = {"draft": ...}`` contract while also surfacing
        the structured SOAP + ICD-10 fields so downstream consumers
        (CDS Hooks, API schemas) can use them.
        """
        soap_dict = self.soap.model_dump(exclude_none=True)
        # Render a plain-text draft from the SOAP fields so callers that
        # only read ``documentation["draft"]`` still get usable content.
        draft_parts = [
            f"主诉: {soap_dict.get('subjective', '')}",
            f"检查: {soap_dict.get('objective', '')}",
            f"评估: {soap_dict.get('assessment', '')}",
            f"计划: {soap_dict.get('plan', '')}",
        ]
        draft = "\n".join(p for p in draft_parts if p.split(": ", 1)[-1])
        return {
            "draft": draft or "",
            "soap": soap_dict,
            "icd10": list(self.icd10),
            "citations": list(self.citations),
        }

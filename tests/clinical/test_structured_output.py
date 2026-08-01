"""Tests for structured LLM output (instructor) across the clinical layer.

Covers three layers:

1. **Schema models** (:mod:`doctoragent.clinical.agents.schemas`) —
   ``from_text()`` parsing of fenced/bare JSON, ``to_list()`` /
   ``to_draft_dict()`` flatteners, default-validated partial output.
2. **Structured adapter** (:mod:`doctoragent.clinical.agents.structured`) —
   base-url normalisation, graceful degradation when instructor is absent,
   ``structured_complete`` never raising.
3. **Orchestrator consumption** (:mod:`doctoragent.clinical.agents.orchestrator`)
   — the orchestrator prefers the structured payload when present and falls
   back to the legacy raw-text path when ``structured`` is ``None``.

All LLM access is mocked — no network, no real model.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from doctoragent.clinical.agents.base import ClinicalAgent
from doctoragent.clinical.agents.orchestrator import (
    ClinicalOrchestrator,
    _structured_of,
)
from doctoragent.clinical.agents.schemas import (
    DocumentationResult,
    DrugSafetyResult,
    LiteratureItem,
    LiteratureResult,
    PatientHistoryResult,
    SoapNote,
)
from doctoragent.clinical.agents.structured import (
    STRUCTURED_AVAILABLE,
    _normalise_base_url,
    structured_complete,
)
from doctoragent.clinical.tools import create_clinical_registry
from doctoragent.connections.models import AuthMethod, Connection, PlatformType
from doctoragent.model.agent import AgentConfig
from doctoragent.model.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _MockLLM:
    """Returns a fixed string for every chat completion (no tool calls)."""

    def __init__(self, response: str = "分析完成。依据 PMID: 12345。") -> None:
        self.response = response
        self.call_count = 0

    async def chat_completion(self, messages: Any, **kwargs: Any) -> str:
        self.call_count += 1
        return self.response

    def chat_completion_sync(self, messages: Any, **kwargs: Any) -> str:
        self.call_count += 1
        return self.response


def _minimal_config() -> AgentConfig:
    return AgentConfig(
        enable_planning=False,
        enable_reflection=False,
        max_iterations=2,
        enable_memory=False,
    )


def _registry() -> ToolRegistry:
    return create_clinical_registry()


def _make_connection(base_url: str = "http://localhost:11434") -> Connection:
    return Connection(
        name="test",
        platform_type=PlatformType.OPENAI_COMPATIBLE,
        base_url=base_url,
        model_name="test-model",
        auth_method=AuthMethod.BEARER,
        api_key="sk-test",
    )


# ===========================================================================
# 1. Schema models — from_text() parsing + flatteners
# ===========================================================================


class TestPatientHistoryResultParsing:
    def test_from_text_parses_fenced_json(self) -> None:
        text = (
            "分析如下：\n"
            "```json\n"
            '{"summary": "高血压病史5年", "problems": ["高血压"], '
            '"timeline": [{"date": "2024-01", "event": "确诊"}], '
            '"citations": ["PMID:1"]}\n'
            "```"
        )
        result = PatientHistoryResult.from_text(text)
        assert result is not None
        assert result.summary == "高血压病史5年"
        assert result.problems == ["高血压"]
        assert result.timeline == [{"date": "2024-01", "event": "确诊"}]
        assert result.citations == ["PMID:1"]

    def test_from_text_parses_bare_json(self) -> None:
        result = PatientHistoryResult.from_text('{"summary": "简要"}')
        assert result is not None
        assert result.summary == "简要"

    def test_from_text_returns_none_on_garbage(self) -> None:
        assert PatientHistoryResult.from_text("不是 JSON") is None

    def test_from_text_returns_none_on_empty(self) -> None:
        assert PatientHistoryResult.from_text("") is None
        assert PatientHistoryResult.from_text("   ") is None

    def test_partial_output_validates_with_defaults(self) -> None:
        # Missing fields default rather than raising.
        result = PatientHistoryResult.from_text('{"summary": "仅有摘要"}')
        assert result is not None
        assert result.summary == "仅有摘要"
        assert result.problems == []
        assert result.citations == []

    def test_extra_fields_allowed(self) -> None:
        # model_config extra="allow" accepts vendor extensions.
        result = PatientHistoryResult.from_text(
            '{"summary": "x", "confidence": 0.9}'
        )
        assert result is not None
        assert getattr(result, "confidence", None) == 0.9


class TestDrugSafetyResultParsing:
    def test_from_text_parses_findings(self) -> None:
        text = (
            "```json\n"
            '{"findings": [{"type": "ddi", "severity": "critical"}], '
            '"severity": "critical", "recommendation": "停药", '
            '"citations": ["PMID:2"]}\n'
            "```"
        )
        result = DrugSafetyResult.from_text(text)
        assert result is not None
        assert result.severity == "critical"
        assert result.recommendation == "停药"
        assert len(result.findings) == 1


class TestLiteratureResultFlattening:
    def test_from_text_parses_nested_results(self) -> None:
        text = (
            "```json\n"
            '{"results": [{"title": "Paper A", "source": "PMID:1", '
            '"evidence_level": "rct", "summary": "relevant"}], '
            '"summary": "综合证据", "citations": ["PMID:1"]}\n'
            "```"
        )
        result = LiteratureResult.from_text(text)
        assert result is not None
        assert len(result.results) == 1
        assert result.results[0].title == "Paper A"
        assert result.summary == "综合证据"

    def test_to_list_flattens_results(self) -> None:
        result = LiteratureResult(
            results=[
                LiteratureItem(title="A", source="PMID:1", evidence_level="rct"),
                LiteratureItem(title="B", source="PMID:2"),
            ],
            summary="综述",
        )
        flat = result.to_list()
        assert isinstance(flat, list)
        assert len(flat) == 2
        assert flat[0]["title"] == "A"
        assert flat[0]["source"] == "PMID:1"
        assert flat[1]["title"] == "B"

    def test_to_list_empty(self) -> None:
        assert LiteratureResult().to_list() == []

    def test_to_list_excludes_none(self) -> None:
        item = LiteratureItem(title="A")
        flat = LiteratureResult(results=[item]).to_list()
        # exclude_none=True drops None, but empty-string defaults are kept
        # (they are not None) — the item always has all declared fields.
        assert flat == [
            {"title": "A", "source": "", "evidence_level": "", "summary": ""}
        ]


class TestDocumentationResultFlattening:
    def test_to_draft_dict_contains_soap_and_icd10(self) -> None:
        result = DocumentationResult(
            soap=SoapNote(
                subjective="主诉头痛",
                objective="血压 160/100",
                assessment="考虑高血压",
                plan="建议复查",
            ),
            icd10=["I10"],
            citations=["PMID:3"],
        )
        draft = result.to_draft_dict()
        assert "draft" in draft
        assert "soap" in draft
        assert draft["icd10"] == ["I10"]
        assert draft["citations"] == ["PMID:3"]
        assert "主诉头痛" in draft["draft"]
        assert draft["soap"]["assessment"] == "考虑高血压"

    def test_to_draft_dict_omits_empty_sections(self) -> None:
        result = DocumentationResult(
            soap=SoapNote(subjective="主诉", objective="", assessment="", plan="")
        )
        draft = result.to_draft_dict()
        # Only the non-empty subjective line makes it into the draft.
        assert "主诉" in draft["draft"]
        assert "检查" not in draft["draft"]

    def test_to_draft_dict_empty(self) -> None:
        draft = DocumentationResult().to_draft_dict()
        assert draft["draft"] == ""
        assert draft["icd10"] == []
        assert draft["citations"] == []


# ===========================================================================
# 2. Structured adapter — normalisation + graceful degradation
# ===========================================================================


class TestNormaliseBaseUrl:
    def test_appends_v1_when_missing(self) -> None:
        conn = _make_connection("http://localhost:11434")
        assert _normalise_base_url(conn) == "http://localhost:11434/v1"

    def test_preserves_existing_v1(self) -> None:
        conn = _make_connection("http://localhost:11434/v1")
        assert _normalise_base_url(conn) == "http://localhost:11434/v1"

    def test_strips_trailing_slash(self) -> None:
        conn = _make_connection("http://localhost:11434/")
        assert _normalise_base_url(conn) == "http://localhost:11434/v1"


class TestStructuredCompleteDegradation:
    @pytest.mark.asyncio
    async def test_returns_none_for_provider_without_connection(self) -> None:
        # A plain mock LLM has no ``connection`` attribute; the structured
        # path must short-circuit to None rather than raising.

        class _BareProvider:
            pass

        result = await structured_complete(
            _BareProvider(),  # type: ignore[arg-type]
            [{"role": "user", "content": "x"}],
            PatientHistoryResult,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        # When STRUCTURED_AVAILABLE but the underlying client raises, the
        # adapter catches and returns None (never propagates).
        if not STRUCTURED_AVAILABLE:
            pytest.skip("instructor not installed in this environment")

        from doctoragent.model.provider import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider(_make_connection())
        # Patch the cached client to raise on create.
        fake_client = AsyncMock()
        fake_client.chat.completions.create.side_effect = RuntimeError("boom")
        provider._instructor_client = fake_client
        result = await structured_complete(
            provider,
            [{"role": "user", "content": "x"}],
            PatientHistoryResult,
        )
        assert result is None


class TestRunWithGuardrailsStructuredKey:
    @pytest.mark.asyncio
    async def test_structured_key_present_when_output_model_set(self) -> None:
        # The LLM returns valid fenced JSON the prompt requested; the cheap
        # from_text() parse should populate ``structured``.
        json_answer = (
            "```json\n"
            '{"summary": "高血压", "problems": ["HTN"], '
            '"timeline": [], "citations": ["PMID:9"]}\n'
            "```"
        )
        # PatientHistoryAgent declares output_model=PatientHistoryResult.
        from doctoragent.clinical.agents.specialists import PatientHistoryAgent

        agent = PatientHistoryAgent(
            llm_provider=_MockLLM(json_answer),
            clinical_registry=_registry(),
            config=_minimal_config(),
        )
        result = await agent.run_with_guardrails("评估")
        assert "structured" in result
        assert isinstance(result["structured"], PatientHistoryResult)
        assert result["structured"].summary == "高血压"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_structured_none_when_output_model_unset(self) -> None:
        agent = ClinicalAgent(
            llm_provider=_MockLLM("分析完成。依据 PMID: 1。"),
            clinical_registry=_registry(),
            config=_minimal_config(),
        )
        result = await agent.run_with_guardrails("评估")
        assert result["structured"] is None

    @pytest.mark.asyncio
    async def test_structured_none_when_guardrail_blocks(self) -> None:
        # A blocked answer must not be structured (unsafe content).
        from doctoragent.clinical.agents.specialists import PatientHistoryAgent

        agent = PatientHistoryAgent(
            llm_provider=_MockLLM("确诊为肺炎。依据 PMID: 1。"),
            clinical_registry=_registry(),
            config=_minimal_config(),
        )
        result = await agent.run_with_guardrails("评估")
        assert result["guardrail_result"]["action"] == "block"
        assert result["structured"] is None

    @pytest.mark.asyncio
    async def test_structured_none_when_parse_fails_and_no_instructor(self) -> None:
        # When the LLM emits non-JSON text and instructor is unavailable
        # (provider has no ``connection``), structured falls back to None.
        from doctoragent.clinical.agents.specialists import PatientHistoryAgent

        agent = PatientHistoryAgent(
            llm_provider=_MockLLM("这是纯文本分析，没有 JSON。"),
            clinical_registry=_registry(),
            config=_minimal_config(),
        )
        result = await agent.run_with_guardrails("评估")
        assert result["structured"] is None
        # Raw answer is still surfaced.
        assert result["answer"]


# ===========================================================================
# 3. Orchestrator consumption of structured output
# ===========================================================================


class TestStructuredOfHelper:
    def test_extracts_structured_from_dict(self) -> None:
        model = PatientHistoryResult(summary="x")
        assert _structured_of({"structured": model}) is model

    def test_returns_none_when_missing(self) -> None:
        assert _structured_of({"structured": None}) is None
        assert _structured_of({}) is None

    def test_returns_none_for_non_dict(self) -> None:
        assert _structured_of(None) is None
        assert _structured_of("str") is None
        assert _structured_of(Exception("boom")) is None


class TestOrchestratorStructuredConsumption:
    """Verify the orchestrator prefers structured output and falls back.

    We patch ``ClinicalAgent.run_with_guardrails`` to inject controlled
    ``structured`` payloads, isolating the consumption logic from the
    instructor/LLM stack.
    """

    @pytest.mark.asyncio
    async def test_literature_uses_structured_to_list(self) -> None:
        lit_result = LiteratureResult(
            results=[
                LiteratureItem(
                    title="Structured Paper",
                    source="PMID:42",
                    evidence_level="rct",
                    summary="key evidence",
                )
            ],
            summary="综述",
            citations=["PMID:42"],
        )

        async def _fake_run(self: ClinicalAgent, task: str) -> dict[str, Any]:
            cls_name = self.__class__.__name__
            base: dict[str, Any] = {
                "answer": "分析完成。依据 PMID: 42。",
                "guardrail_result": {"action": "allow", "passed": True, "warnings": []},
                "citations": ["PMID:42"],
                "disclaimer": "免责",
                "degraded": False,
                "structured": None,
            }
            if cls_name == "LiteratureAgent":
                base["structured"] = lit_result
            return base

        with patch.object(ClinicalAgent, "run_with_guardrails", _fake_run):
            orch = ClinicalOrchestrator(
                llm_provider=_MockLLM(), clinical_registry=_registry()
            )
            result = await orch.analyze({"patient_id": "P1"}, "评估")

        # The structured literature item surfaces, not the raw-text fallback.
        assert any(
            item.get("title") == "Structured Paper" for item in result.literature
        )
        assert "PMID:42" in result.citations

    @pytest.mark.asyncio
    async def test_documentation_uses_structured_to_draft_dict(self) -> None:
        doc_result = DocumentationResult(
            soap=SoapNote(
                subjective="主诉胸痛",
                objective="ECG 正常",
                assessment="考虑心绞痛",
                plan="建议冠脉造影",
            ),
            icd10=["I20.9"],
            citations=["PMID:7"],
        )

        async def _fake_run(self: ClinicalAgent, task: str) -> dict[str, Any]:
            cls_name = self.__class__.__name__
            base: dict[str, Any] = {
                "answer": "分析完成。依据 PMID: 7。",
                "guardrail_result": {"action": "allow", "passed": True, "warnings": []},
                "citations": ["PMID:7"],
                "disclaimer": "免责",
                "degraded": False,
                "structured": None,
            }
            if cls_name == "DocumentationAgent":
                base["structured"] = doc_result
            return base

        with patch.object(ClinicalAgent, "run_with_guardrails", _fake_run):
            orch = ClinicalOrchestrator(
                llm_provider=_MockLLM(), clinical_registry=_registry()
            )
            result = await orch.analyze({"patient_id": "P1"}, "评估")

        assert result.documentation is not None
        assert "soap" in result.documentation
        assert result.documentation["icd10"] == ["I20.9"]
        assert "主诉胸痛" in result.documentation["draft"]

    @pytest.mark.asyncio
    async def test_falls_back_to_text_when_structured_none(self) -> None:
        # When structured is None for all specialists, the orchestrator
        # must still produce a valid result via the legacy text path.
        async def _fake_run(self: ClinicalAgent, task: str) -> dict[str, Any]:
            return {
                "answer": "文本分析。依据 PMID: 99。",
                "guardrail_result": {"action": "allow", "passed": True, "warnings": []},
                "citations": ["PMID:99"],
                "disclaimer": "免责",
                "degraded": False,
                "structured": None,
            }

        with patch.object(ClinicalAgent, "run_with_guardrails", _fake_run):
            orch = ClinicalOrchestrator(
                llm_provider=_MockLLM(), clinical_registry=_registry()
            )
            result = await orch.analyze({"patient_id": "P1"}, "评估")

        assert result.documentation is not None
        assert "draft" in result.documentation
        # No structured soap/icd10 keys in the legacy path.
        assert "soap" not in result.documentation
        assert isinstance(result.literature, list)
        assert "PMID:99" in result.citations


# ===========================================================================
# 4. Specialist output_model declarations
# ===========================================================================


class TestSpecialistOutputModels:
    """Each specialist must declare its structured output_model."""

    def test_patient_history_agent_output_model(self) -> None:
        from doctoragent.clinical.agents.specialists import PatientHistoryAgent

        assert PatientHistoryAgent.output_model is PatientHistoryResult

    def test_drug_safety_agent_output_model(self) -> None:
        from doctoragent.clinical.agents.specialists import DrugSafetyAgent

        assert DrugSafetyAgent.output_model is DrugSafetyResult

    def test_literature_agent_output_model(self) -> None:
        from doctoragent.clinical.agents.specialists import LiteratureAgent

        assert LiteratureAgent.output_model is LiteratureResult

    def test_documentation_agent_output_model(self) -> None:
        from doctoragent.clinical.agents.specialists import DocumentationAgent

        assert DocumentationAgent.output_model is DocumentationResult

    def test_base_clinical_agent_default_output_model_none(self) -> None:
        assert ClinicalAgent.output_model is None


# ===========================================================================
# 5. SoapNote nested model
# ===========================================================================


class TestSoapNote:
    def test_defaults(self) -> None:
        note = SoapNote()
        assert note.subjective == ""
        assert note.objective == ""
        assert note.assessment == ""
        assert note.plan == ""

    def test_extra_fields_allowed(self) -> None:
        note = SoapNote(subjective="x", confidence=0.8)  # type: ignore[call-arg]
        assert note.subjective == "x"
        assert getattr(note, "confidence", None) == 0.8

    def test_is_basemodel(self) -> None:
        assert issubclass(SoapNote, BaseModel)

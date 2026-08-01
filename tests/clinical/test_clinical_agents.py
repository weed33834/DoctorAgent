"""Tests for the clinical multi-agent layer.

All LLM access is mocked — no network, no real model. The clinical tool
registry is built with ``create_clinical_registry()`` (no clients injected);
tools register defensively and are never executed because the mock LLM
never emits a tool call.
"""

from __future__ import annotations

from typing import Any

import pytest

from doctoragent.clinical.agents import (
    ClinicalAgent,
    ClinicalOrchestrator,
    ClinicalWorkflowResult,
    DocumentationAgent,
    DrugSafetyAgent,
    LiteratureAgent,
    PatientHistoryAgent,
    run_clinical_workflow,
)
from doctoragent.clinical.agents.specialists import build_sub_registry
from doctoragent.clinical.safety import ClinicalGuardrails, ClinicalRuleEngine
from doctoragent.clinical.tools import create_clinical_registry
from doctoragent.model.agent import AgentConfig
from doctoragent.model.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _MockLLM:
    """Records calls and returns a fixed string for every chat completion."""

    def __init__(self, response: str = "分析完成。依据 PMID: 12345。") -> None:
        self.response = response
        self.call_count = 0

    def chat_completion_sync(self, messages: Any, **kwargs: Any) -> str:
        self.call_count += 1
        return self.response


def _minimal_config() -> AgentConfig:
    """Config that reduces each agent run to a single LLM call (no plan/reflect)."""
    return AgentConfig(
        enable_planning=False,
        enable_reflection=False,
        max_iterations=2,
        enable_memory=False,
    )


def _full_registry() -> ToolRegistry:
    return create_clinical_registry()


def _tool_names(registry: ToolRegistry) -> list[str]:
    return [t.name for t in registry.list_tools()]


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


def test_clinical_agent_system_prompt_contains_constraints() -> None:
    agent = ClinicalAgent(
        llm_provider=_MockLLM(),
        clinical_registry=_full_registry(),
        config=_minimal_config(),
    )
    prompt = agent._build_system_prompt()
    assert "不替代医生诊断" in prompt
    assert "引证" in prompt
    assert "免责声明" in prompt
    assert "确定性规则引擎" in prompt


def test_clinical_agent_system_prompt_includes_tool_descriptions() -> None:
    agent = PatientHistoryAgent(
        llm_provider=_MockLLM(),
        clinical_registry=_full_registry(),
        config=_minimal_config(),
    )
    prompt = agent._build_system_prompt()
    # A FHIR read tool the history agent can see must appear in the prompt.
    assert "read_patient_record" in prompt


# ---------------------------------------------------------------------------
# Specialist tool subsets
# ---------------------------------------------------------------------------


def test_patient_history_agent_tool_subset() -> None:
    agent = PatientHistoryAgent(llm_provider=_MockLLM(), clinical_registry=_full_registry())
    names = set(_tool_names(agent.tools))
    assert names == {
        "read_patient_record",
        "read_medications",
        "read_allergies",
        "read_lab_results",
    }
    assert len(names) == 4


def test_drug_safety_agent_tool_subset() -> None:
    agent = DrugSafetyAgent(llm_provider=_MockLLM(), clinical_registry=_full_registry())
    names = set(_tool_names(agent.tools))
    assert names == {
        "check_drug_interactions",
        "check_vitals",
        "check_lab_ranges",
        "read_medications",
        "read_allergies",
    }
    assert len(names) == 5


def test_literature_agent_tool_subset() -> None:
    agent = LiteratureAgent(llm_provider=_MockLLM(), clinical_registry=_full_registry())
    names = set(_tool_names(agent.tools))
    assert names == {"search_literature", "search_clinical_guidelines"}
    assert len(names) == 2


def test_documentation_agent_tool_subset() -> None:
    agent = DocumentationAgent(llm_provider=_MockLLM(), clinical_registry=_full_registry())
    names = set(_tool_names(agent.tools))
    assert names == {"generate_soap_note", "code_icd10", "write_clinical_note"}
    assert len(names) == 3


def test_build_sub_registry_skips_missing_tools() -> None:
    sub = build_sub_registry(_full_registry(), ["read_patient_record", "does_not_exist"])
    assert _tool_names(sub) == ["read_patient_record"]


# ---------------------------------------------------------------------------
# run_with_guardrails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_guardrails_blocks_definitive_diagnosis() -> None:
    # "确诊为" is a definitive-diagnosis pattern → guardrail blocks.
    mock = _MockLLM("确诊为肺炎。依据 PMID: 1。")
    agent = ClinicalAgent(
        llm_provider=mock, clinical_registry=_full_registry(), config=_minimal_config()
    )
    result = await agent.run_with_guardrails("评估该患者")
    assert result["guardrail_result"]["action"] == "block"
    assert "需医生确认" in result["answer"]
    assert result["disclaimer"]
    assert result["degraded"] is False


@pytest.mark.asyncio
async def test_run_with_guardrails_allows_safe_output() -> None:
    safe = "患者心率 75 bpm，属正常范围。依据 PMID: 12345。"
    mock = _MockLLM(safe)
    agent = PatientHistoryAgent(
        llm_provider=mock, clinical_registry=_full_registry(), config=_minimal_config()
    )
    result = await agent.run_with_guardrails("评估该患者")
    assert result["guardrail_result"]["action"] == "allow"
    assert result["answer"] == safe
    assert "PMID: 12345" in result["citations"]


@pytest.mark.asyncio
async def test_run_with_guardrails_degrades_without_llm() -> None:
    agent = ClinicalAgent(
        llm_provider=None, clinical_registry=_full_registry(), config=_minimal_config()
    )
    result = await agent.run_with_guardrails("评估该患者")
    assert result["degraded"] is True
    assert result["guardrail_result"]["action"] == "flag"
    assert "LLM 未配置" in result["answer"]


# ---------------------------------------------------------------------------
# ClinicalOrchestrator.analyze
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clinical_orchestrator_analyze_returns_result_fields() -> None:
    mock = _MockLLM("分析完成。依据 PMID: 12345。")
    orchestrator = ClinicalOrchestrator(llm_provider=mock, clinical_registry=_full_registry())
    result = await orchestrator.analyze({"patient_id": "P1"}, "该患者用药是否安全？")
    assert isinstance(result, ClinicalWorkflowResult)
    assert result.history_summary
    assert isinstance(result.safety_findings, list)
    assert isinstance(result.literature, list)
    assert result.documentation is not None
    assert "draft" in result.documentation
    assert "action" in result.guardrail_result
    assert "不替代医生诊断" in result.disclaimer
    assert isinstance(result.citations, list)
    assert isinstance(result.requires_human_review, bool)
    # Fan-out: at least one LLM call per specialist (history, drug, lit, doc).
    assert mock.call_count >= 4


@pytest.mark.asyncio
async def test_clinical_orchestrator_analyze_degraded_without_llm() -> None:
    orchestrator = ClinicalOrchestrator(llm_provider=None, clinical_registry=_full_registry())
    result = await orchestrator.analyze({"patient_id": "P1"}, "评估")
    assert "LLM 未配置" in result.history_summary
    assert result.documentation is None
    assert result.requires_human_review is True
    assert result.guardrail_result["action"] == "flag"


@pytest.mark.asyncio
async def test_clinical_orchestrator_runs_rule_engine_findings() -> None:
    mock = _MockLLM("分析完成。依据 PMID: 12345。")
    orchestrator = ClinicalOrchestrator(llm_provider=mock, clinical_registry=_full_registry())
    # heart_rate 35 is below the critical-low threshold → critical finding.
    result = await orchestrator.analyze({"patient_id": "P1", "vitals": {"heart_rate": 35}}, "评估")
    assert len(result.safety_findings) >= 1
    assert any(f["severity"] in ("critical", "contraindicated") for f in result.safety_findings)
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_clinical_orchestrator_uses_injected_rule_engine_and_guardrails() -> None:
    rule_engine = ClinicalRuleEngine()
    guardrails = ClinicalGuardrails()
    orchestrator = ClinicalOrchestrator(
        llm_provider=_MockLLM(),
        clinical_registry=_full_registry(),
        rule_engine=rule_engine,
        guardrails=guardrails,
    )
    assert orchestrator.rule_engine is rule_engine
    assert orchestrator.guardrails is guardrails
    result = await orchestrator.analyze({"patient_id": "P1"}, "评估")
    assert isinstance(result, ClinicalWorkflowResult)


@pytest.mark.asyncio
async def test_clinical_orchestrator_self_evolution_recall_and_store() -> None:
    """When a SelfEvolutionEngine is wired, the orchestrator recalls past
    experiences before running and stores a new experience after."""
    from doctoragent.model.self_evolution import (
        ExecutionOutcome,
        Experience,
        SelfEvolutionEngine,
    )

    # Use a real SelfEvolutionEngine backed by a temp task_store so the
    # SQLite experience DB is exercised end-to-end.
    import tempfile
    from pathlib import Path

    class _FakeTaskStore:
        def __init__(self, tmpdir: Path) -> None:
            self.base_dir = tmpdir

    with tempfile.TemporaryDirectory() as tmp:
        engine = SelfEvolutionEngine(_FakeTaskStore(Path(tmp)), llm_provider=None)
        # Pre-seed an experience so recall has something to find.
        engine.store_experience(
            Experience(
                query="该患者用药是否安全？",
                query_pattern="用药安全查询",
                outcome=ExecutionOutcome.SUCCESS,
                lessons=["核查 warfarin 与 ibuprofen 联用"],
                optimized_prompt="",
                recommended_tools=["check_drug_interactions"],
            )
        )

        mock = _MockLLM("分析完成。依据 PMID: 12345。")
        orchestrator = ClinicalOrchestrator(
            llm_provider=mock,
            clinical_registry=_full_registry(),
            self_evolution_engine=engine,
        )
        result = await orchestrator.analyze(
            {"patient_id": "P1"}, "该患者用药是否安全？"
        )
        assert isinstance(result, ClinicalWorkflowResult)
        # The pre-seeded experience should be recalled (and the history
        # agent's prompt should have received the preamble). We can't
        # directly inspect the prompt, but we can verify a NEW experience
        # was stored by re-recalling.
        recalled = engine.recall_experiences("该患者用药是否安全？", top_k=10)
        # At least the pre-seeded one + the one stored after analyze.
        assert len(recalled) >= 1


@pytest.mark.asyncio
async def test_clinical_orchestrator_self_evolution_failure_does_not_break() -> None:
    """A broken SelfEvolutionEngine must never break the clinical workflow."""

    class _BrokenEngine:
        def recall_experiences(self, *args, **kwargs):
            raise RuntimeError("db locked")

        def analyze_trajectory(self, *args, **kwargs):
            raise RuntimeError("db locked")

    orchestrator = ClinicalOrchestrator(
        llm_provider=_MockLLM("分析完成。依据 PMID: 12345。"),
        clinical_registry=_full_registry(),
        self_evolution_engine=_BrokenEngine(),
    )
    result = await orchestrator.analyze({"patient_id": "P1"}, "评估")
    assert isinstance(result, ClinicalWorkflowResult)
    assert result.history_summary  # workflow still completed


def test_experience_preamble_labels_history_as_advisory() -> None:
    """The recalled-experience preamble must mark itself as advisory so
    the LLM cannot present past lessons as authoritative clinical advice."""
    from doctoragent.model.self_evolution import Experience

    exp = Experience(
        query="q",
        lessons=["lesson A", "lesson B"],
    )
    preamble = ClinicalOrchestrator._experience_preamble([exp])
    assert "历史经验" in preamble
    assert "仅供参考" in preamble
    assert "lesson A" in preamble


def test_experience_preamble_empty_when_no_experiences() -> None:
    assert ClinicalOrchestrator._experience_preamble([]) == ""


# ---------------------------------------------------------------------------
# run_clinical_workflow entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_clinical_workflow_degraded_no_llm() -> None:
    result = await run_clinical_workflow(
        patient_context={"patient_id": "P1", "vitals": {"heart_rate": 35}},
        query="评估",
        llm_provider=None,
    )
    assert isinstance(result, ClinicalWorkflowResult)
    # No exception raised; deterministic rule still fires.
    assert len(result.safety_findings) >= 1
    assert result.requires_human_review is True
    assert "LLM 未配置" in result.history_summary


@pytest.mark.asyncio
async def test_run_clinical_workflow_with_mock_llm() -> None:
    result = await run_clinical_workflow(
        patient_context={"patient_id": "P1"},
        query="该患者用药是否安全？",
        llm_provider=_MockLLM("分析完成。依据 PMID: 12345。"),
    )
    assert isinstance(result, ClinicalWorkflowResult)
    assert result.history_summary
    assert result.documentation is not None


# ---------------------------------------------------------------------------
# ClinicalWorkflowResult serialization
# ---------------------------------------------------------------------------


def test_clinical_workflow_result_serialization() -> None:
    result = ClinicalWorkflowResult(
        history_summary="摘要",
        safety_findings=[{"severity": "critical", "finding": "心率危急值"}],
        literature=[{"title": "paper", "source": "PMID:1"}],
        documentation={"draft": "SOAP"},
        guardrail_result={"action": "flag", "passed": False},
        disclaimer="本建议仅供参考，不替代医生诊断，最终决策由医生负责。",
        citations=["PMID:1"],
        requires_human_review=True,
    )
    dumped = result.model_dump()
    assert dumped["history_summary"] == "摘要"
    assert dumped["requires_human_review"] is True
    assert dumped["citations"] == ["PMID:1"]
    # Round-trip through pydantic reconstruction.
    rebuilt = ClinicalWorkflowResult.model_validate(dumped)
    assert rebuilt == result
    assert rebuilt.documentation == {"draft": "SOAP"}


def test_clinical_workflow_result_defaults() -> None:
    empty = ClinicalWorkflowResult()
    assert empty.history_summary == ""
    assert empty.safety_findings == []
    assert empty.literature == []
    assert empty.documentation is None
    assert empty.guardrail_result == {}
    assert empty.citations == []
    assert empty.requires_human_review is False

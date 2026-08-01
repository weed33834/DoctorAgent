"""Tests for the 15 clinical Tool subclasses and the registry factory.

All external dependencies (FHIR / RxNorm / openFDA / PubMed clients, LLM
provider, AegisConfig) are mocked — no network access.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from doctoragent.clinical.knowledge.drug_interactions import DrugInteractionResult
from doctoragent.clinical.tools import (
    CheckDrugInteractionsTool,
    CheckLabRangesTool,
    CheckVitalsTool,
    CodeIcd10Tool,
    ComplianceSelfCheckTool,
    FlagSafetyAlertTool,
    GenerateDifferentialDiagnosisTool,
    GenerateSoapNoteTool,
    ReadAllergiesTool,
    ReadLabResultsTool,
    ReadMedicationsTool,
    ReadPatientRecordTool,
    SearchClinicalGuidelinesTool,
    SearchLiteratureTool,
    WriteClinicalNoteTool,
    create_clinical_registry,
)
from doctoragent.config import AegisConfig
from doctoragent.model.tools import Tool

# ---------------------------------------------------------------------------
# Definition correctness — name / category / parameters
# ---------------------------------------------------------------------------


def test_read_patient_record_definition() -> None:
    d = ReadPatientRecordTool().definition
    assert d.name == "read_patient_record"
    assert d.category == "clinical_fhir"
    assert [p.name for p in d.parameters] == ["patient_id"]


def test_read_medications_definition() -> None:
    d = ReadMedicationsTool().definition
    assert d.name == "read_medications"
    assert d.category == "clinical_fhir"
    assert [p.name for p in d.parameters] == ["patient_id"]


def test_read_allergies_definition() -> None:
    d = ReadAllergiesTool().definition
    assert d.name == "read_allergies"
    assert d.category == "clinical_fhir"
    assert [p.name for p in d.parameters] == ["patient_id"]


def test_read_lab_results_definition() -> None:
    d = ReadLabResultsTool().definition
    assert d.name == "read_lab_results"
    assert d.category == "clinical_fhir"
    names = [p.name for p in d.parameters]
    assert names == ["patient_id", "count"]
    count_param = d.parameters[1]
    assert count_param.required is False
    assert count_param.default == 20


def test_check_drug_interactions_definition() -> None:
    d = CheckDrugInteractionsTool().definition
    assert d.name == "check_drug_interactions"
    assert d.category == "clinical_knowledge"
    assert [p.name for p in d.parameters] == ["drugs"]
    assert d.parameters[0].type == "array"


def test_search_clinical_guidelines_definition() -> None:
    d = SearchClinicalGuidelinesTool().definition
    assert d.name == "search_clinical_guidelines"
    assert d.category == "clinical_knowledge"
    names = [p.name for p in d.parameters]
    assert names == ["query", "max_results"]
    assert d.parameters[1].default == 5


def test_search_literature_definition() -> None:
    d = SearchLiteratureTool().definition
    assert d.name == "search_literature"
    assert d.category == "clinical_knowledge"
    assert [p.name for p in d.parameters] == ["query", "max_results"]


def test_check_vitals_definition() -> None:
    d = CheckVitalsTool().definition
    assert d.name == "check_vitals"
    assert d.category == "clinical_knowledge"
    assert d.parameters[0].name == "vitals"
    assert d.parameters[0].type == "object"


def test_check_lab_ranges_definition() -> None:
    d = CheckLabRangesTool().definition
    assert d.name == "check_lab_ranges"
    assert d.category == "clinical_knowledge"
    names = [p.name for p in d.parameters]
    assert names == ["test_name", "value", "unit", "gender"]
    # unit and gender are optional
    assert d.parameters[2].required is False
    assert d.parameters[3].required is False


def test_generate_differential_diagnosis_definition() -> None:
    d = GenerateDifferentialDiagnosisTool().definition
    assert d.name == "generate_differential_diagnosis"
    assert d.category == "clinical_generation"
    names = [p.name for p in d.parameters]
    assert names == ["patient_summary", "max_ddx"]
    assert d.parameters[1].default == 5


def test_generate_soap_note_definition() -> None:
    d = GenerateSoapNoteTool().definition
    assert d.name == "generate_soap_note"
    assert d.category == "clinical_generation"
    assert [p.name for p in d.parameters] == ["patient_context"]


def test_code_icd10_definition() -> None:
    d = CodeIcd10Tool().definition
    assert d.name == "code_icd10"
    assert d.category == "clinical_generation"
    assert [p.name for p in d.parameters] == ["clinical_text"]


def test_flag_safety_alert_definition() -> None:
    d = FlagSafetyAlertTool().definition
    assert d.name == "flag_safety_alert"
    assert d.category == "clinical_alert"
    names = [p.name for p in d.parameters]
    assert names == ["patient_id", "alert_type", "description", "severity"]
    assert d.parameters[3].enum == ["low", "moderate", "high", "critical"]


def test_write_clinical_note_definition() -> None:
    d = WriteClinicalNoteTool().definition
    assert d.name == "write_clinical_note"
    assert d.category == "clinical_write"
    names = [p.name for p in d.parameters]
    assert names == ["patient_id", "note_content", "note_type"]
    assert d.parameters[2].default == "progress"


def test_compliance_self_check_definition() -> None:
    d = ComplianceSelfCheckTool().definition
    assert d.name == "compliance_self_check"
    assert d.category == "clinical_compliance"
    assert d.parameters == []


# ---------------------------------------------------------------------------
# Defensive failure — dependency is None
# ---------------------------------------------------------------------------


async def test_fhir_tools_none_client_returns_error() -> None:
    for cls in (ReadPatientRecordTool, ReadMedicationsTool, ReadAllergiesTool, ReadLabResultsTool):
        tool = cls(fhir_client=None)
        result = await tool.execute(patient_id="p1")
        assert result.success is False
        assert "FHIR client not configured" in (result.error or "")


async def test_check_drug_interactions_none_clients_returns_error() -> None:
    tool = CheckDrugInteractionsTool(rxnorm_client=None, openfda_client=None)
    result = await tool.execute(drugs=["warfarin", "ibuprofen"])
    assert result.success is False
    assert "RxNorm/openFDA client not configured" in (result.error or "")


async def test_pubmed_tools_none_client_returns_error() -> None:
    for cls in (SearchClinicalGuidelinesTool, SearchLiteratureTool):
        tool = cls(pubmed_client=None)
        result = await tool.execute(query="diabetes")
        assert result.success is False
        assert "PubMed client not configured" in (result.error or "")


async def test_llm_tools_none_provider_returns_error() -> None:
    for cls in (
        GenerateDifferentialDiagnosisTool,
        GenerateSoapNoteTool,
        CodeIcd10Tool,
    ):
        tool = cls(llm_provider=None)
        # Provide the minimum required arg for each tool.
        if cls is GenerateDifferentialDiagnosisTool:
            result = await tool.execute(patient_summary="fever")
        elif cls is GenerateSoapNoteTool:
            result = await tool.execute(patient_context="fever")
        else:
            result = await tool.execute(clinical_text="fever")
        assert result.success is False
        assert "LLM provider not configured" in (result.error or "")


# ---------------------------------------------------------------------------
# Execute with mocked dependencies
# ---------------------------------------------------------------------------


async def test_read_patient_record_with_mock_fhir() -> None:
    record = {"patient": {"id": "p1"}, "conditions": [], "medications": []}
    mock_client = AsyncMock()
    mock_client.read_patient_record = AsyncMock(return_value=record)
    tool = ReadPatientRecordTool(fhir_client=mock_client)
    result = await tool.execute(patient_id="p1")
    assert result.success is True
    assert result.data["patient_record"] == record
    mock_client.read_patient_record.assert_awaited_once_with("p1")


async def test_read_medications_with_mock_fhir() -> None:
    meds = [{"id": "m1", "status": "active"}]
    mock_client = AsyncMock()
    mock_client.read_medications = AsyncMock(return_value=meds)
    tool = ReadMedicationsTool(fhir_client=mock_client)
    result = await tool.execute(patient_id="p1")
    assert result.success is True
    assert result.data["medications"] == meds
    assert result.data["count"] == 1


async def test_read_allergies_with_mock_fhir() -> None:
    allergies = [{"id": "a1"}]
    mock_client = AsyncMock()
    mock_client.read_allergies = AsyncMock(return_value=allergies)
    tool = ReadAllergiesTool(fhir_client=mock_client)
    result = await tool.execute(patient_id="p1")
    assert result.success is True
    assert result.data["allergies"] == allergies


async def test_read_lab_results_with_mock_fhir() -> None:
    labs = [{"id": "o1"}]
    mock_client = AsyncMock()
    mock_client.read_lab_results = AsyncMock(return_value=labs)
    tool = ReadLabResultsTool(fhir_client=mock_client)
    result = await tool.execute(patient_id="p1", count=5)
    assert result.success is True
    assert result.data["lab_results"] == labs
    mock_client.read_lab_results.assert_awaited_once_with("p1", count=5)


async def test_check_drug_interactions_with_mock_engine() -> None:
    interactions = [
        DrugInteractionResult(
            drug_a="warfarin",
            drug_b="ibuprofen",
            severity="major",
            description="increased bleeding risk",
        )
    ]
    rxnorm = object()
    openfda = object()
    tool = CheckDrugInteractionsTool(rxnorm_client=rxnorm, openfda_client=openfda)
    with patch(
        "doctoragent.clinical.knowledge.check_drug_interactions",
        new=AsyncMock(return_value=interactions),
    ):
        result = await tool.execute(drugs=["warfarin", "ibuprofen"])
    assert result.success is True
    assert result.data["count"] == 1
    assert result.data["interactions"][0]["drug_a"] == "warfarin"
    assert result.data["interactions"][0]["severity"] == "major"


async def test_check_vitals_abnormal_returns_evaluation() -> None:
    tool = CheckVitalsTool()
    result = await tool.execute(vitals={"heart_rate": 140, "systolic_bp": 200})
    assert result.success is True
    evals = {e["test"]: e for e in result.data["evaluations"]}
    # heart_rate 140 >= critical_high 130 → critical_high
    assert str(evals["heart_rate"]["flag"]) == "critical_high"
    # systolic_bp 200 >= critical_high 180 → critical_high
    assert str(evals["systolic_bp"]["flag"]) == "critical_high"
    assert result.data["count"] == 2


async def test_check_vitals_normal_value() -> None:
    tool = CheckVitalsTool()
    result = await tool.execute(vitals={"heart_rate": 75})
    assert result.success is True
    assert str(result.data["evaluations"][0]["flag"]) == "normal"


async def test_check_lab_ranges_abnormal() -> None:
    tool = CheckLabRangesTool()
    result = await tool.execute(test_name="sodium", value=160.0)
    assert result.success is True
    evaluation = result.data["evaluation"]
    assert str(evaluation["flag"]) == "critical_high"
    assert evaluation["abnormal"] is True


async def test_check_lab_ranges_gender_specific() -> None:
    tool = CheckLabRangesTool()
    # Female hemoglobin range 115-150; 120 is within range → normal.
    result = await tool.execute(test_name="hemoglobin", value=120.0, gender="female")
    assert result.success is True
    assert str(result.data["evaluation"]["flag"]) == "normal"


async def test_generate_differential_diagnosis_with_mock_llm() -> None:
    ddx = [
        {"diagnosis": "Influenza", "icd10": "J11.1", "probability": 0.7, "evidence": "fever"},
        {"diagnosis": "Common cold", "icd10": "J00", "probability": 0.3, "evidence": "rhinitis"},
    ]
    mock_provider = AsyncMock()
    mock_provider.chat_completion = AsyncMock(return_value=json.dumps(ddx))
    tool = GenerateDifferentialDiagnosisTool(llm_provider=mock_provider)
    result = await tool.execute(patient_summary="fever and cough", max_ddx=5)
    assert result.success is True
    assert result.data["count"] == 2
    assert result.data["differential_diagnosis"][0]["diagnosis"] == "Influenza"


async def test_generate_soap_note_with_mock_llm() -> None:
    mock_provider = AsyncMock()
    mock_provider.chat_completion = AsyncMock(return_value="S: ... O: ... A: ... P: ...")
    tool = GenerateSoapNoteTool(llm_provider=mock_provider)
    result = await tool.execute(patient_context="fever")
    assert result.success is True
    assert "S:" in result.data["soap_note"]


async def test_code_icd10_with_mock_llm() -> None:
    codes = [{"code": "J11.1", "description": "Influenza", "confidence": 0.9}]
    mock_provider = AsyncMock()
    mock_provider.chat_completion = AsyncMock(return_value=json.dumps(codes))
    tool = CodeIcd10Tool(llm_provider=mock_provider)
    result = await tool.execute(clinical_text="patient has flu")
    assert result.success is True
    assert result.data["icd10_codes"][0]["code"] == "J11.1"


async def test_generate_differential_diagnosis_handles_non_json() -> None:
    mock_provider = AsyncMock()
    mock_provider.chat_completion = AsyncMock(return_value="not valid json")
    tool = GenerateDifferentialDiagnosisTool(llm_provider=mock_provider)
    result = await tool.execute(patient_summary="fever")
    assert result.success is False
    assert "not a JSON array" in (result.error or "")


async def test_flag_safety_alert_returns_confirmation() -> None:
    tool = FlagSafetyAlertTool()
    result = await tool.execute(
        patient_id="p1",
        alert_type="critical_lab",
        description="K+ 7.0 mmol/L",
        severity="critical",
    )
    assert result.success is True
    assert result.data["flagged"] is True
    assert result.data["patient_id"] == "p1"
    assert result.data["severity"] == "critical"


async def test_write_clinical_note_requires_confirmation_and_does_not_write() -> None:
    tool = WriteClinicalNoteTool()
    result = await tool.execute(
        patient_id="p1",
        note_content="Patient improving.",
        note_type="progress",
    )
    # Must NOT succeed — write is deferred to a human-confirmation workflow.
    assert result.success is False
    assert result.error == "Requires human confirmation"
    assert result.data["requires_confirmation"] is True
    assert result.data["note"] == "Patient improving."
    assert result.data["note_type"] == "progress"


async def test_write_clinical_note_default_note_type() -> None:
    tool = WriteClinicalNoteTool()
    result = await tool.execute(patient_id="p1", note_content="x")
    assert result.data["note_type"] == "progress"


async def test_compliance_self_check_with_default_config() -> None:
    config = AegisConfig()
    tool = ComplianceSelfCheckTool(config=config)
    result = await tool.execute()
    assert result.success is True
    report = result.data
    assert "overall_status" in report
    assert "compliance_gaps" in report
    assert "encryption" in report


async def test_compliance_self_check_none_config_uses_default() -> None:
    tool = ComplianceSelfCheckTool(config=None)
    result = await tool.execute()
    assert result.success is True
    assert result.data["overall_status"] in {"compliant", "partial", "non_compliant"}


# ---------------------------------------------------------------------------
# Registry factory
# ---------------------------------------------------------------------------


def test_create_clinical_registry_has_15_tools() -> None:
    registry = create_clinical_registry()
    tools = registry.list_tools()
    assert len(tools) == 15


def test_create_clinical_registry_tool_names() -> None:
    registry = create_clinical_registry()
    names = {t.name for t in registry.list_tools()}
    expected = {
        "read_patient_record",
        "read_medications",
        "read_allergies",
        "read_lab_results",
        "check_drug_interactions",
        "search_clinical_guidelines",
        "search_literature",
        "check_vitals",
        "check_lab_ranges",
        "generate_differential_diagnosis",
        "generate_soap_note",
        "code_icd10",
        "flag_safety_alert",
        "write_clinical_note",
        "compliance_self_check",
    }
    assert names == expected


def test_create_clinical_registry_categories() -> None:
    registry = create_clinical_registry()
    by_cat: dict[str, int] = {}
    for t in registry.list_tools():
        by_cat[t.category] = by_cat.get(t.category, 0) + 1
    assert by_cat == {
        "clinical_fhir": 4,
        "clinical_knowledge": 5,
        "clinical_generation": 3,
        "clinical_alert": 1,
        "clinical_write": 1,
        "clinical_compliance": 1,
    }


def test_all_tools_subclass_tool_abc() -> None:
    registry = create_clinical_registry()
    # list_tools returns ToolDefinition; reach into the underlying Tool objects.
    for tool_obj in registry._tools.values():
        assert isinstance(tool_obj, Tool)


async def test_registry_execute_dispatches_to_tool() -> None:
    registry = create_clinical_registry()
    result = await registry.execute("check_vitals", vitals={"heart_rate": 140})
    assert result.success is True
    assert result.tool_name == "check_vitals"


def test_registry_injects_dependencies() -> None:
    mock_fhir = AsyncMock()
    mock_rxnorm = object()
    mock_openfda = object()
    mock_pubmed = AsyncMock()
    mock_llm = AsyncMock()
    registry = create_clinical_registry(
        fhir_client=mock_fhir,
        rxnorm_client=mock_rxnorm,
        openfda_client=mock_openfda,
        pubmed_client=mock_pubmed,
        llm_provider=mock_llm,
    )
    assert len(registry.list_tools()) == 15
    # Verify the FHIR tool holds the injected client.
    fhir_tool = registry.get("read_patient_record")
    assert fhir_tool is not None
    assert fhir_tool.fhir_client is mock_fhir
    # DDI tool holds both knowledge clients.
    ddi_tool = registry.get("check_drug_interactions")
    assert ddi_tool.rxnorm_client is mock_rxnorm
    assert ddi_tool.openfda_client is mock_openfda
    # LLM tool holds the provider.
    ddx_tool = registry.get("generate_differential_diagnosis")
    assert ddx_tool.llm_provider is mock_llm

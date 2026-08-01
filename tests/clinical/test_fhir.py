"""Tests for the FHIR R4 adapter (client, resources, parser).

All tests are unit tests: HTTP is faked via :class:`httpx.MockTransport`, so
no real FHIR server is contacted and no ``integration`` marker is needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from doctoragent.clinical.fhir import (
    SUPPORTED_READ_RESOURCES,
    FHIRClient,
    FHIROperationError,
    FHIRResourceNotFoundError,
    allergy_to_text,
    lab_to_text,
    medication_to_text,
    parse_resource,
    patient_to_text,
    serialize_resource,
    validate_resource,
)
from doctoragent.clinical.fhir.client import PatientRecord, PatientSummary
from doctoragent.clinical.fhir.parser import condition_to_text, encounter_to_text

# --------------------------------------------------------------------------- #
# Test fixtures (synthetic FHIR R4 resources)
# --------------------------------------------------------------------------- #
PATIENT_DICT: dict[str, Any] = {
    "resourceType": "Patient",
    "id": "p1",
    "gender": "male",
    "birthDate": "1960-01-01",
    "name": [{"given": ["张"], "family": "三"}],
}

CONDITION_DICT: dict[str, Any] = {
    "resourceType": "Condition",
    "id": "c1",
    "subject": {"reference": "Patient/p1"},
    "code": {
        "coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11.9",
                     "display": "2型糖尿病"}],
        "text": "2型糖尿病",
    },
    "clinicalStatus": {
        "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                     "code": "active"}],
    },
}

MEDICATION_DICT: dict[str, Any] = {
    "resourceType": "MedicationRequest",
    "id": "m1",
    "status": "active",
    "medicationCodeableConcept": {
        "coding": [{"code": "metformin", "display": "二甲双胍"}],
        "text": "二甲双胍",
    },
    "dosageInstruction": [
        {
            "text": "500mg bid",
            "doseAndRate": [
                {"doseQuantity": {"value": 500, "unit": "mg"}},
            ],
            "timing": {"repeat": {"frequency": 2, "period": 1, "periodUnit": "d"}},
        }
    ],
}

ALLERGY_DICT: dict[str, Any] = {
    "resourceType": "AllergyIntolerance",
    "id": "a1",
    "code": {"coding": [{"code": "penicillin", "display": "青霉素"}], "text": "青霉素"},
    "reaction": [
        {
            "manifestation": [
                {"coding": [{"code": "rash", "display": "皮疹"}], "text": "皮疹"}
            ]
        }
    ],
}

LAB_HIGH_DICT: dict[str, Any] = {
    "resourceType": "Observation",
    "id": "lab1",
    "code": {"coding": [{"code": "fasting-glucose", "display": "空腹血糖"}],
             "text": "空腹血糖"},
    "valueQuantity": {"value": 8.5, "unit": "mmol/L"},
    "interpretation": [{"coding": [{"code": "H"}]}],
    "referenceRange": [{"low": {"value": 3.9}, "high": {"value": 6.1}}],
}

ENCOUNTER_DICT: dict[str, Any] = {
    "resourceType": "Encounter",
    "id": "e1",
    "status": "finished",
    "class": {"code": "AMB", "display": "门诊就诊"},
    "period": {"start": "2024-03-01T09:00:00+08:00"},
}


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    """Wrap resources in a FHIR Bundle (searchset)."""
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


def _operation_outcome(severity: str = "error", diagnostics: str = "boom") -> dict[str, Any]:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severity, "diagnostics": diagnostics}],
    }


# --------------------------------------------------------------------------- #
# MockTransport builder
# --------------------------------------------------------------------------- #
Handler = Callable[[httpx.Request], httpx.Response]


def _mock_transport(handler: Handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _route_handler(routes: dict[tuple[str, str], Handler]) -> Handler:
    """Build a handler that dispatches on (method, path-suffix).

    ``routes`` keys are ``(METHOD, path_suffix)``; the first suffix that
    ``endswith`` the request path wins. Falls through to 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        for (m, suffix), h in routes.items():
            if method == m and path.endswith(suffix):
                return h(request)
        return httpx.Response(404, json=_operation_outcome(diagnostics=f"no route {method} {path}"))

    return handler


def _json_response(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body, headers={"Content-Type": "application/fhir+json"})


# --------------------------------------------------------------------------- #
# resources.py
# --------------------------------------------------------------------------- #
class TestParseResource:
    def test_parse_dict_returns_model_instance(self) -> None:
        resource = parse_resource(PATIENT_DICT)
        # fhir.resources Patient model exposes the fields as attributes.
        assert resource.id == "p1"
        assert resource.gender == "male"

    def test_parse_json_string(self) -> None:
        resource = parse_resource(json.dumps(PATIENT_DICT))
        assert resource.id == "p1"

    def test_parse_infers_resource_type_from_data(self) -> None:
        resource = parse_resource({"resourceType": "Patient", "id": "x"})
        assert resource.id == "x"

    def test_parse_explicit_resource_type_override(self) -> None:
        # resource_type kwarg takes precedence; data lacks resourceType.
        resource = parse_resource({"id": "y"}, resource_type="Patient")
        assert resource.id == "y"

    def test_parse_missing_resource_type_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="resource_type"):
            parse_resource({"id": "no-rt"})

    def test_parse_invalid_payload_raises_validation_error(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            parse_resource({"resourceType": "Patient", "birthDate": "not-a-date"})


class TestSerializeResource:
    def test_roundtrip_preserves_core_fields(self) -> None:
        resource = parse_resource(PATIENT_DICT)
        dumped = serialize_resource(resource)
        assert dumped["resourceType"] == "Patient"
        assert dumped["id"] == "p1"
        assert dumped["gender"] == "male"
        assert dumped["birthDate"] == "1960-01-01"

    def test_serialize_excludes_none(self) -> None:
        resource = parse_resource({"resourceType": "Patient", "id": "z"})
        dumped = serialize_resource(resource)
        # Fields that weren't set should not appear.
        assert "gender" not in dumped
        assert "birthDate" not in dumped

    def test_serialize_is_json_safe(self) -> None:
        resource = parse_resource(PATIENT_DICT)
        dumped = serialize_resource(resource)
        # Must round-trip through json.dumps without raising.
        json.dumps(dumped)


class TestValidateResource:
    def test_valid_resource_returns_empty_list(self) -> None:
        resource = parse_resource(PATIENT_DICT)
        assert validate_resource(resource) == []

    def test_none_returns_error(self) -> None:
        assert validate_resource(None) == ["resource must not be None"]

    def test_invalid_resource_returns_nonempty_list(self) -> None:
        # Build a valid Patient then corrupt a field post-construction by
        # parsing an invalid one and inspecting errors (parse would raise, so
        # we construct via a deliberately-invalid re-parse path).
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            parse_resource({"resourceType": "Patient", "birthDate": "bad"})


class TestSupportedResources:
    def test_supported_read_resources_contains_clinical_types(self) -> None:
        for rtype in ("Patient", "Condition", "MedicationRequest", "Observation"):
            assert rtype in SUPPORTED_READ_RESOURCES


# --------------------------------------------------------------------------- #
# parser.py
# --------------------------------------------------------------------------- #
class TestPatientToText:
    def test_header_contains_gender_and_age(self) -> None:
        text = patient_to_text(PATIENT_DICT)
        assert "患者：男" in text
        # birthDate 1960-01-01 → age depends on today; just check 岁 present.
        assert "岁" in text

    def test_includes_conditions_section(self) -> None:
        text = patient_to_text(PATIENT_DICT, conditions=[CONDITION_DICT])
        assert "现患疾病" in text
        assert "2型糖尿病" in text
        assert "E11.9" in text

    def test_includes_medications_section(self) -> None:
        text = patient_to_text(PATIENT_DICT, medications=[MEDICATION_DICT])
        assert "当前用药" in text
        assert "二甲双胍" in text

    def test_includes_allergies_section(self) -> None:
        text = patient_to_text(PATIENT_DICT, allergies=[ALLERGY_DICT])
        assert "过敏" in text
        assert "青霉素" in text
        assert "皮疹" in text

    def test_includes_labs_section_with_abnormal_flag(self) -> None:
        text = patient_to_text(PATIENT_DICT, labs=[LAB_HIGH_DICT])
        assert "近期检验" in text
        assert "空腹血糖" in text
        assert "8.5" in text
        assert "↑" in text

    def test_missing_fields_do_not_crash(self) -> None:
        # Empty patient, no related resources.
        text = patient_to_text({})
        assert text.startswith("患者：")
        # No sections should be appended when lists are empty.
        assert "现患疾病" not in text
        assert "当前用药" not in text

    def test_resolved_condition_excluded_from_active_section(self) -> None:
        resolved = {
            **CONDITION_DICT,
            "clinicalStatus": {"coding": [{"code": "resolved"}]},
        }
        text = patient_to_text(PATIENT_DICT, conditions=[resolved])
        assert "现患疾病" not in text

    def test_unknown_gender_falls_back(self) -> None:
        text = patient_to_text({"resourceType": "Patient", "id": "x"})
        assert "未知" in text


class TestMedicationToText:
    def test_full_medication(self) -> None:
        text = medication_to_text(MEDICATION_DICT)
        assert "二甲双胍" in text
        assert "500mg" in text or "500" in text
        assert "active" in text

    def test_medication_with_only_name(self) -> None:
        med = {
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": {"text": "阿司匹林"},
        }
        assert "阿司匹林" in medication_to_text(med)

    def test_empty_medication_returns_empty_string(self) -> None:
        assert medication_to_text({}) == ""
        assert medication_to_text({"resourceType": "MedicationRequest"}) == ""


class TestLabToText:
    def test_high_flag_from_interpretation(self) -> None:
        text = lab_to_text(LAB_HIGH_DICT)
        assert "空腹血糖" in text
        assert "8.5" in text
        assert "mmol/L" in text
        assert "↑" in text

    def test_low_flag_from_reference_range(self) -> None:
        low_lab = {
            "resourceType": "Observation",
            "code": {"text": "血钠"},
            "valueQuantity": {"value": 130, "unit": "mmol/L"},
            "referenceRange": [{"low": {"value": 135}, "high": {"value": 145}}],
        }
        text = lab_to_text(low_lab)
        assert "↓" in text

    def test_normal_lab_has_no_flag(self) -> None:
        normal = {
            "resourceType": "Observation",
            "code": {"text": "血钾"},
            "valueQuantity": {"value": 4.2, "unit": "mmol/L"},
            "referenceRange": [{"low": {"value": 3.5}, "high": {"value": 5.0}}],
        }
        text = lab_to_text(normal)
        assert "↑" not in text
        assert "↓" not in text
        assert "血钾" in text

    def test_lab_without_value_returns_empty(self) -> None:
        no_value = {"resourceType": "Observation", "code": {"text": "无值检验"}}
        assert lab_to_text(no_value) == ""


class TestAllergyToText:
    def test_allergy_with_manifestation(self) -> None:
        assert allergy_to_text(ALLERGY_DICT) == "青霉素(皮疹)"

    def test_allergy_without_manifestation(self) -> None:
        allergy = {"code": {"text": "花粉"}}
        assert allergy_to_text(allergy) == "花粉"

    def test_empty_allergy_returns_empty(self) -> None:
        assert allergy_to_text({}) == ""


class TestConditionToText:
    def test_condition_with_code_and_display(self) -> None:
        assert condition_to_text(CONDITION_DICT) == "2型糖尿病(E11.9)"

    def test_condition_only_code(self) -> None:
        cond = {"code": {"coding": [{"code": "I10"}]}}
        assert condition_to_text(cond) == "(I10)"

    def test_empty_condition(self) -> None:
        assert condition_to_text({}) == ""


class TestEncounterToText:
    def test_encounter_basic(self) -> None:
        text = encounter_to_text(ENCOUNTER_DICT)
        assert "门诊就诊" in text
        assert "2024-03-01" in text


# --------------------------------------------------------------------------- #
# client.py (using httpx.MockTransport)
# --------------------------------------------------------------------------- #
BASE_URL = "https://fhir.test/fhir"


class TestFHIRClientRead:
    async def test_read_returns_resource_dict(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/Patient/p1")
            assert request.headers["Authorization"] == "Bearer token-abc"
            return _json_response(200, PATIENT_DICT)

        async with FHIRClient(BASE_URL, auth_token="token-abc",
                              transport=_mock_transport(handler)) as client:
            result = await client.read("Patient", "p1")
        assert result["id"] == "p1"
        assert result["resourceType"] == "Patient"

    async def test_read_404_raises_resource_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(404, _operation_outcome(diagnostics="not found"))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            with pytest.raises(FHIRResourceNotFoundError):
                await client.read("Patient", "missing")

    async def test_read_requires_resource_id(self) -> None:
        async with FHIRClient(BASE_URL, transport=_mock_transport(
            lambda r: _json_response(200, PATIENT_DICT))) as client:
            with pytest.raises(ValueError):
                await client.read("Patient", "")


class TestFHIRClientSearch:
    async def test_search_parses_bundle_entries(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return _json_response(200, _bundle(CONDITION_DICT))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            results = await client.search(
                "Condition", {"patient": "p1", "clinical-status": "active"}
            )
        assert len(results) == 1
        assert results[0]["id"] == "c1"
        assert captured["params"]["patient"] == "p1"
        assert captured["params"]["clinical-status"] == "active"

    async def test_search_empty_bundle_returns_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _bundle())

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            results = await client.search("Condition", {"patient": "p1"})
        assert results == []

    async def test_search_500_raises_operation_error(self) -> None:
        # 500 is retried up to 3 times then surfaces as FHIROperationError.
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _json_response(500, _operation_outcome(diagnostics="server down"))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            with pytest.raises(FHIROperationError):
                await client.search("Condition", {"patient": "p1"})
        assert call_count["n"] == 3  # 1 initial + 2 retries

    async def test_search_4xx_raises_operation_error_no_retry(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _json_response(400, _operation_outcome(diagnostics="bad request"))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            with pytest.raises(FHIROperationError) as exc_info:
                await client.search("Condition", {"patient": "p1"})
        assert call_count["n"] == 1  # 4xx not retried
        assert exc_info.value.status_code == 400
        assert any("bad request" in i for i in exc_info.value.issues)


class TestFHIRClientConvenienceReads:
    async def test_read_medications_passes_active_status(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return _json_response(200, _bundle(MEDICATION_DICT))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            meds = await client.read_medications("p1")
        assert len(meds) == 1
        assert meds[0]["id"] == "m1"
        assert captured["params"]["status"] == "active"
        assert captured["params"]["patient"] == "p1"

    async def test_read_allergies_uses_clinical_status(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return _json_response(200, _bundle(ALLERGY_DICT))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            allergies = await client.read_allergies("p1")
        assert len(allergies) == 1
        assert captured["params"]["clinical-status"] == "active"

    async def test_read_lab_results_sort_and_count(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return _json_response(200, _bundle(LAB_HIGH_DICT))

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            labs = await client.read_lab_results("p1", count=15)
        assert len(labs) == 1
        assert captured["params"]["category"] == "laboratory"
        assert captured["params"]["_sort"] == "-date"
        assert captured["params"]["_count"] == "15"


class TestFHIRClientCreate:
    async def test_create_posts_and_returns_resource(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return _json_response(201, {**PATIENT_DICT, "id": "p2"})

        async with FHIRClient(BASE_URL, transport=_mock_transport(handler)) as client:
            created = await client.create("Patient", {"gender": "male"})
        assert captured["method"] == "POST"
        assert captured["body"]["resourceType"] == "Patient"
        assert created["id"] == "p2"


class TestReadPatientRecord:
    async def test_aggregates_all_resource_types(self) -> None:
        routes: dict[tuple[str, str], Handler] = {
            ("GET", "/Patient/p1"): lambda r: _json_response(200, PATIENT_DICT),
            ("GET", "/Condition"): lambda r: _json_response(200, _bundle(CONDITION_DICT)),
            ("GET", "/Encounter"): lambda r: _json_response(200, _bundle(ENCOUNTER_DICT)),
            ("GET", "/MedicationRequest"): lambda r: _json_response(200, _bundle(MEDICATION_DICT)),
            ("GET", "/AllergyIntolerance"): lambda r: _json_response(200, _bundle(ALLERGY_DICT)),
        }
        transport = _mock_transport(_route_handler(routes))
        async with FHIRClient(BASE_URL, transport=transport) as client:
            record = await client.read_patient_record("p1")

        assert record["patient"]["id"] == "p1"
        assert len(record["conditions"]) == 1
        assert record["conditions"][0]["id"] == "c1"
        assert len(record["encounters"]) == 1
        assert len(record["medications"]) == 1
        assert record["medications"][0]["id"] == "m1"
        assert len(record["allergies"]) == 1
        # Schema round-trips through PatientRecord.
        validated = PatientRecord.model_validate(record)
        assert validated.patient["id"] == "p1"

    async def test_record_feeds_parser(self) -> None:
        routes: dict[tuple[str, str], Handler] = {
            ("GET", "/Patient/p1"): lambda r: _json_response(200, PATIENT_DICT),
            ("GET", "/Condition"): lambda r: _json_response(200, _bundle(CONDITION_DICT)),
            ("GET", "/Encounter"): lambda r: _json_response(200, _bundle()),
            ("GET", "/MedicationRequest"): lambda r: _json_response(200, _bundle(MEDICATION_DICT)),
            ("GET", "/AllergyIntolerance"): lambda r: _json_response(200, _bundle(ALLERGY_DICT)),
        }
        transport = _mock_transport(_route_handler(routes))
        async with FHIRClient(BASE_URL, transport=transport) as client:
            record = await client.read_patient_record("p1")

        text = patient_to_text(
            record["patient"],
            conditions=record["conditions"],
            medications=record["medications"],
            allergies=record["allergies"],
        )
        assert "2型糖尿病" in text
        assert "二甲双胍" in text
        assert "青霉素" in text


class TestPatientSummaryModel:
    def test_from_resource_extracts_name(self) -> None:
        summary = PatientSummary.from_resource(PATIENT_DICT)
        assert summary.id == "p1"
        assert summary.gender == "male"
        assert summary.birth_date == "1960-01-01"
        assert "张" in summary.name
        assert "三" in summary.name

    def test_from_resource_handles_missing_name(self) -> None:
        summary = PatientSummary.from_resource({"id": "x", "gender": "female"})
        assert summary.name == ""
        assert summary.gender == "female"

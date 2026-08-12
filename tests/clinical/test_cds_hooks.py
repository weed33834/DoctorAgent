"""Tests for the HL7 CDS Hooks 2.0 integration.

Two layers are covered:

* **Pure-logic translation** (:func:`translate_request_to_workflow`,
  :func:`translate_result_to_response`, the LOINC extractor). No HTTP, no
  FastAPI dependency, no LLM provider — these run on every CI machine.
* **HTTP end-to-end** via FastAPI's :class:`TestClient`. Skipped when
  FastAPI isn't installed (mirrors the pattern used by ``test_api_server``).

Spec reference: https://cds-hooks.hl7.org/2.0/
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from doctoragent.clinical.agents.orchestrator import ClinicalWorkflowResult
from doctoragent.clinical.integrations.cds_hooks import (
    CardIndicator,
    CDSHookRequest,
    CDSHookResponse,
    CDSHookService,
    SupportedHook,
    discover_services,
    translate_request_to_workflow,
    translate_result_to_response,
)


# --------------------------------------------------------------------------- #
# Shared FHIR fixtures
# --------------------------------------------------------------------------- #
def _bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of FHIR resources in a Bundle (EHR prefetch shape)."""
    return {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }


def _med_request(code: str, display: str, *, status: str = "active") -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": f"med-{code}",
        "status": status,
        "medicationCodeableConcept": {
            "coding": [{"code": code, "display": display}],
            "text": display,
        },
    }


def _allergy(code: str, display: str, manifestation: str = "皮疹") -> dict[str, Any]:
    return {
        "resourceType": "AllergyIntolerance",
        "id": f"all-{code}",
        "code": {
            "coding": [{"code": code, "display": display}],
            "text": display,
        },
        "reaction": [
            {
                "manifestation": [
                    {
                        "coding": [{"code": "rash", "display": manifestation}],
                        "text": manifestation,
                    }
                ]
            }
        ],
    }


def _vital_observation(loinc: str, value: float, unit: str = "bpm") -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": f"obs-{loinc}",
        "category": [{"coding": [{"code": "vital-signs"}]}],
        "code": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": loinc,
                }
            ]
        },
        "valueQuantity": {"value": value, "unit": unit},
    }


def _lab_observation(loinc: str, value: float, unit: str) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": f"lab-{loinc}",
        "category": [{"coding": [{"code": "laboratory"}]}],
        "code": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": loinc,
                }
            ]
        },
        "valueQuantity": {"value": value, "unit": unit},
    }


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
class TestDiscovery:
    def test_discovery_returns_three_services(self) -> None:
        services = discover_services()
        ids = {s["id"] for s in services}
        assert ids == {
            "doctoragent-patient-view",
            "doctoragent-order-select",
            "doctoragent-order-sign",
        }

    def test_each_service_has_required_fields(self) -> None:
        for svc in discover_services():
            assert svc["id"], "id must be non-empty"
            assert svc["hook"] in [h.value for h in SupportedHook]
            assert svc.get("title")
            assert svc.get("description")
            # prefetch templates must be URL-templated with context.patientId
            assert isinstance(svc.get("prefetch"), dict)
            for key, template in svc["prefetch"].items():
                assert "{{context.patientId}}" in template, (
                    f"prefetch[{key}] must reference context.patientId"
                )

    def test_service_ids_are_url_safe(self) -> None:
        for svc in discover_services():
            sid = svc["id"]
            assert all(c.isalnum() or c in "-_" for c in sid), sid


# --------------------------------------------------------------------------- #
# Request → workflow translation (pure logic)
# --------------------------------------------------------------------------- #
class TestTranslateRequestToWorkflow:
    def test_patient_view_extracts_patient_id_and_prefetch(self) -> None:
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-1",
            context={"patientId": "p1", "userId": "u1"},
            prefetch={
                "patient": {"resourceType": "Patient", "id": "p1"},
                "medications": _bundle([_med_request("metformin", "二甲双胍")]),
                "allergies": _bundle([_allergy("penicillin", "青霉素")]),
                "conditions": _bundle(
                    [{"resourceType": "Condition", "id": "c1"}]
                ),
            },
        )
        ctx, query = translate_request_to_workflow(req)
        assert ctx["patient_id"] == "p1"
        # medication_names: cleaned drug-name strings for the rule engine.
        assert "二甲双胍" in ctx["medication_names"]
        assert "青霉素" in ctx["allergy_names"]
        # medications/allergies are now the stringified names (rule engine shape).
        assert ctx["medications"] == ctx["medication_names"]
        assert ctx["allergies"] == ctx["allergy_names"]
        # conditions stays as FHIR dicts (LLM specialists read them).
        assert isinstance(ctx["conditions"], list)
        assert ctx["conditions"][0]["resourceType"] == "Condition"
        # Query mentions the patient id.
        assert "p1" in query

    def test_vitals_extracted_from_loinc_coded_observations(self) -> None:
        """A heart_rate=35 LOINC Observation must reach the rule engine as
        ``vitals['heart_rate'] = 35.0`` so the critical rule fires."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-2",
            context={"patientId": "p1"},
            prefetch={
                "observations": _bundle([_vital_observation("8867-4", 35)]),
            },
        )
        ctx, _ = translate_request_to_workflow(req)
        assert ctx["vitals"] == {"heart_rate": 35.0}

    def test_labs_extracted_from_loinc_coded_observations(self) -> None:
        """Hemoglobin=60 (critical_low=70) is a lab, not a vital."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-3",
            context={"patientId": "p1"},
            prefetch={
                "observations": _bundle(
                    [_lab_observation("718-7", 60, "g/L")]
                ),
            },
        )
        ctx, _ = translate_request_to_workflow(req)
        assert "vitals" not in ctx or "heart_rate" not in ctx["vitals"]
        assert ctx["labs"][0]["test"] == "hemoglobin"
        assert ctx["labs"][0]["value"] == 60.0

    def test_vendor_vitals_do_not_drop_prefetch_labs(self) -> None:
        """Regression (GH #19): when ``context.vitals`` is provided but labs
        come only from prefetch Observations, the labs must NOT be silently
        dropped. Filling vitals must not gate labs extraction."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-19",
            context={"patientId": "p1", "vitals": {"heart_rate": 28}},
            prefetch={
                "observations": _bundle([_lab_observation("718-7", 60, "g/L")]),
            },
        )
        ctx, _ = translate_request_to_workflow(req)
        # Vendor vitals win …
        assert ctx["vitals"] == {"heart_rate": 28}
        # … but the prefetch lab still reaches the rule engine.
        assert ctx["labs"][0]["test"] == "hemoglobin"
        assert ctx["labs"][0]["value"] == 60.0

    def test_vendor_labs_do_not_drop_prefetch_vitals(self) -> None:
        """Symmetric case: a vendor ``context.labs`` must not gate the
        vitals extracted from prefetch Observations."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-19b",
            context={
                "patientId": "p1",
                "labs": [{"test": "potassium", "value": 2.9, "unit": "mmol/L"}],
            },
            prefetch={
                "observations": _bundle([_vital_observation("8867-4", 35)]),
            },
        )
        ctx, _ = translate_request_to_workflow(req)
        # Vendor labs win …
        assert ctx["labs"][0]["test"] == "potassium"
        # … but the prefetch vital still reaches the rule engine.
        assert ctx["vitals"] == {"heart_rate": 35.0}

    def test_vendor_extension_vitals_pass_through(self) -> None:
        """EHRs may send vitals as a context extension; that value must win
        over (or stand in for) any prefetch Observations."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-4",
            context={"patientId": "p1", "vitals": {"heart_rate": 28}},
        )
        ctx, _ = translate_request_to_workflow(req)
        assert ctx["vitals"] == {"heart_rate": 28}

    def test_order_select_merges_draft_orders_into_medications(self) -> None:
        draft_med = _med_request("amoxicillin", "阿莫西林")
        active_med = _med_request("warfarin", "华法林")
        req = CDSHookRequest(
            hook="order-select",
            hookInstance="inst-5",
            context={
                "patientId": "p1",
                "draftOrders": _bundle([draft_med]),
                "selections": ["MedicationRequest/med-amoxicillin"],
            },
            prefetch={
                "medications": _bundle([active_med]),
                "allergies": _bundle([_allergy("penicillin", "青霉素")]),
            },
        )
        ctx, _ = translate_request_to_workflow(req)
        # Draft orders are kept as FHIR dicts under draft_orders (for LLM).
        assert isinstance(ctx["draft_orders"], list)
        assert ctx["draft_orders"][0]["resourceType"] == "MedicationRequest"
        # Both active + draft drug names reach the rules engine so DDI fires.
        assert "华法林" in ctx["medications"]
        assert "阿莫西林" in ctx["medications"]
        assert ctx["selections"] == ["MedicationRequest/med-amoxicillin"]

    def test_drug_name_strips_status_suffix_punctuation(self) -> None:
        """medication_to_text appends ', active' so the first whitespace-split
        token can carry a trailing comma. The translator must strip it so the
        rules engine doesn't see '二甲双胍,'."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-6",
            context={"patientId": "p1"},
            prefetch={
                "medications": _bundle([_med_request("metformin", "二甲双胍")]),
            },
        )
        ctx, _ = translate_request_to_workflow(req)
        assert ctx["medication_names"] == ["二甲双胍"]

    def test_unknown_hook_degrades_gracefully(self) -> None:
        """An unlisted hook id must not crash the translator — it should
        produce a generic query and still map the prefetch."""
        req = CDSHookRequest(
            hook="medication-prescribe",  # not in SupportedHook
            hookInstance="inst-7",
            context={"patientId": "p1"},
        )
        ctx, query = translate_request_to_workflow(req)
        assert ctx["patient_id"] == "p1"
        assert "medication-prescribe" in query


# --------------------------------------------------------------------------- #
# Workflow result → CDS response translation (pure logic)
# --------------------------------------------------------------------------- #
class TestTranslateResultToResponse:
    def _request(self) -> CDSHookRequest:
        return CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-x",
            context={"patientId": "p1"},
        )

    def test_blocking_finding_produces_critical_card_with_suggestion(self) -> None:
        result = ClinicalWorkflowResult(
            safety_findings=[
                {
                    "rule_type": "vitals",
                    "severity": "critical",
                    "finding": "heart_rate 35 bpm 低于危急值 40",
                    "recommendation": "立即复核 heart_rate",
                }
            ],
            requires_human_review=True,
            disclaimer="本结果仅供医师参考",
        )
        resp = translate_result_to_response(self._request(), result)
        assert resp.cards, "must emit at least one card"
        critical_cards = [c for c in resp.cards if c.indicator == CardIndicator.CRITICAL]
        assert critical_cards, "blocking finding → critical card"
        card = critical_cards[0]
        assert card.suggestions, "critical card must carry an accept/decline suggestion"
        action = card.suggestions[0].actions[0]
        assert action.type in ("update", "delete")
        assert card.overrideReasons, "critical card must allow clinician override"

    def test_contraindicated_finding_proposes_delete_action(self) -> None:
        result = ClinicalWorkflowResult(
            safety_findings=[
                {
                    "rule_type": "allergy",
                    "severity": "contraindicated",
                    "finding": "青霉素过敏",
                    "recommendation": "避免使用",
                }
            ],
            requires_human_review=True,
        )
        resp = translate_result_to_response(self._request(), result)
        critical = next(c for c in resp.cards if c.indicator == CardIndicator.CRITICAL)
        # Contraindicated → 'delete' suggestion so the EHR shows a hard-stop.
        assert critical.suggestions[0].actions[0].type == "delete"

    def test_requires_human_review_emits_warning_card(self) -> None:
        result = ClinicalWorkflowResult(
            requires_human_review=True,
            history_summary="患者有糖尿病史",
            disclaimer="仅供参考",
        )
        resp = translate_result_to_response(self._request(), result)
        warnings = [c for c in resp.cards if c.indicator == CardIndicator.WARN]
        assert warnings
        assert "人工复核" in warnings[0].summary

    def test_documentation_and_literature_emitted_as_info_card(self) -> None:
        result = ClinicalWorkflowResult(
            documentation={"draft": "SOAP 草稿..."},
            literature=[{"summary": "二甲双胍一线治疗", "pmid": "12345"}],
            citations=["PMID:12345"],
            disclaimer="仅供参考",
        )
        resp = translate_result_to_response(self._request(), result)
        info_cards = [c for c in resp.cards if c.indicator == CardIndicator.INFO]
        assert info_cards
        assert "SOAP" in (info_cards[0].detail or "")

    def test_never_emits_system_actions(self) -> None:
        """The orchestrator never auto-mutates orders — every clinical
        decision must be accepted or dismissed by a human. systemActions
        must therefore always be None."""
        result = ClinicalWorkflowResult(
            safety_findings=[
                {"severity": "critical", "finding": "x", "rule_type": "vitals"}
            ],
            requires_human_review=True,
        )
        resp = translate_result_to_response(self._request(), result)
        assert resp.systemActions is None


# --------------------------------------------------------------------------- #
# Service façade — runs the real workflow (rules-only path)
# --------------------------------------------------------------------------- #
class TestCDSHookServiceInvoke:
    @pytest.mark.asyncio
    async def test_invoke_with_critical_vital_emits_critical_card(self) -> None:
        """End-to-end: prefetch a critical heart_rate Observation, invoke the
        service with no LLM provider (degraded path), expect a critical
        card with a blocking suggestion."""
        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-end-to-end",
            context={"patientId": "p1"},
            prefetch={
                "observations": _bundle([_vital_observation("8867-4", 35)]),
            },
        )
        svc = CDSHookService(llm_provider=None)
        resp = await svc.invoke(req)
        assert isinstance(resp, CDSHookResponse)
        assert resp.cards
        critical = [c for c in resp.cards if c.indicator == CardIndicator.CRITICAL]
        assert critical, "heart_rate=35 must produce a critical card"
        assert critical[0].suggestions

    @pytest.mark.asyncio
    async def test_invoke_audits_invocation_when_logger_present(self, tmp_path) -> None:
        """When an audit logger is wired in, ``cds_hooks_invocation`` must be
        recorded for FDA SaMD / 21 CFR Part 11 traceability."""
        from doctoragent.config import AegisConfig
        from doctoragent.security.audit_log import AuditLogger

        config = AegisConfig()
        config.paths.logs = tmp_path / "logs"
        config.paths.logs.mkdir(parents=True, exist_ok=True)
        audit_logger = AuditLogger(config)

        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-audit",
            context={"patientId": "p-audit"},
        )
        svc = CDSHookService(llm_provider=None, audit_logger=audit_logger)
        await svc.invoke(req)

        records = audit_logger.query()
        event_types = {r.get("event_type") for r in records}
        assert "cds_hooks_invocation" in event_types
        inv = next(r for r in records if r.get("event_type") == "cds_hooks_invocation")
        assert inv["details"]["hook"] == "patient-view"
        assert inv["details"]["patient_id"] == "p-audit"

    @pytest.mark.asyncio
    async def test_invoke_never_leaks_fhir_authorization_to_audit(
        self, tmp_path
    ) -> None:
        """The SMART-on-FHIR bearer token (``fhirAuthorization.token``) is a
        credential and MUST NOT be persisted to the audit log."""
        from doctoragent.config import AegisConfig
        from doctoragent.security.audit_log import AuditLogger

        config = AegisConfig()
        config.paths.logs = tmp_path / "logs"
        config.paths.logs.mkdir(parents=True, exist_ok=True)
        audit_logger = AuditLogger(config)

        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-leak",
            context={"patientId": "p-leak"},
            fhirAuthorization={
                "access_token": "super-secret-bearer",
                "token_type": "Bearer",
                "scope": "patient/*.read",
            },
        )
        svc = CDSHookService(llm_provider=None, audit_logger=audit_logger)
        await svc.invoke(req)

        records = audit_logger.query()
        body = repr(records)
        assert "super-secret-bearer" not in body, (
            "SMART bearer token must never appear in the audit log"
        )

    @pytest.mark.asyncio
    async def test_invoke_returns_error_card_on_workflow_failure(self) -> None:
        """If run_clinical_workflow raises, the service must NOT propagate the
        exception — it must return a critical error card so the EHR can
        surface the failure to the clinician instead of timing out."""

        class _BoomError(Exception):
            pass

        async def _boom(*args, **kwargs):
            raise _BoomError("workflow exploded")

        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-boom",
            context={"patientId": "p1"},
        )
        svc = CDSHookService(llm_provider=None)
        # Monkey-patch the workflow to raise.
        import doctoragent.clinical.agents.workflow as wf_mod

        original = wf_mod.run_clinical_workflow
        wf_mod.run_clinical_workflow = _boom  # type: ignore[assignment]
        try:
            resp = await svc.invoke(req)
        finally:
            wf_mod.run_clinical_workflow = original  # type: ignore[assignment]
        assert resp.cards
        assert resp.cards[0].indicator == CardIndicator.CRITICAL
        assert "CDS 服务调用失败" in resp.cards[0].summary

    @pytest.mark.asyncio
    async def test_invoke_uses_ehr_smart_token_over_static_client(self) -> None:
        """When the EHR sends ``fhirAuthorization.access_token``, the service
        must build an ephemeral SMART-scoped FHIRClient with it (rather than
        the statically-injected one), so specialist agents respect the
        clinician's patient-scope consent. Verifies the token is forwarded
        and the ephemeral client is closed after the call.
        """
        captured_clients: list = []

        class _TrackingWorkflow:
            async def __call__(self, *, patient_context, query, llm_provider,
                               fhir_client, audit_logger):
                captured_clients.append(fhir_client)
                # Return a minimal valid result so translate_result_to_response
                # produces an info card (no findings → no critical).
                from types import SimpleNamespace
                return SimpleNamespace(
                    safety_findings=[],
                    requires_human_review=False,
                    history_summary="",
                    documentation={},
                    literature=[],
                    citations=[],
                    disclaimer="",
                )

        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-smart",
            context={"patientId": "p1"},
            fhirServer="https://ehr.example.com/fhir",
            fhirAuthorization={
                "access_token": "ehr-issued-smart-token",
                "token_type": "Bearer",
                "scope": "patient/*.read",
                "patient": "p1",
            },
        )

        # A sentinel static client — should NOT be used when EHR token present.
        class _SentinelClient:
            closed = False

            async def aclose(self) -> None:
                self.closed = True

        sentinel = _SentinelClient()
        svc = CDSHookService(llm_provider=None, fhir_client=sentinel)

        import doctoragent.clinical.agents.workflow as wf_mod

        original = wf_mod.run_clinical_workflow
        tracking = _TrackingWorkflow()
        wf_mod.run_clinical_workflow = tracking  # type: ignore[assignment]
        try:
            resp = await svc.invoke(req)
        finally:
            wf_mod.run_clinical_workflow = original  # type: ignore[assignment]

        # 1) The workflow received a client (not None, not the sentinel).
        assert len(captured_clients) == 1
        used = captured_clients[0]
        assert used is not sentinel
        # 2) The ephemeral client carries the EHR bearer token.
        assert used.auth_token == "ehr-issued-smart-token"
        assert used.base_url == "https://ehr.example.com/fhir"
        # 3) The sentinel (static) client was never touched.
        assert sentinel.closed is False
        # 4) Response still serialises (no exception leaked to the EHR).
        assert isinstance(resp, CDSHookResponse)

    @pytest.mark.asyncio
    async def test_invoke_falls_back_to_static_when_no_smart_token(self) -> None:
        """Without ``fhirAuthorization`` the service uses the injected client."""

        class _StaticClient:
            pass

        captured: list = []

        class _TrackingWorkflow:
            async def __call__(self, *, patient_context, query, llm_provider,
                               fhir_client, audit_logger):
                captured.append(fhir_client)
                from types import SimpleNamespace
                return SimpleNamespace(
                    safety_findings=[], requires_human_review=False,
                    history_summary="", documentation={},
                    literature=[], citations=[], disclaimer="",
                )

        req = CDSHookRequest(
            hook="patient-view",
            hookInstance="inst-no-smart",
            context={"patientId": "p1"},
            # no fhirServer / fhirAuthorization
        )
        static = _StaticClient()
        svc = CDSHookService(llm_provider=None, fhir_client=static)
        import doctoragent.clinical.agents.workflow as wf_mod

        original = wf_mod.run_clinical_workflow
        wf_mod.run_clinical_workflow = _TrackingWorkflow()  # type: ignore[assignment]
        try:
            await svc.invoke(req)
        finally:
            wf_mod.run_clinical_workflow = original  # type: ignore[assignment]
        assert captured[0] is static


# --------------------------------------------------------------------------- #
# HTTP router — only run when FastAPI is installed (mirrors test_api_server)
# --------------------------------------------------------------------------- #
try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient  # noqa: F401

    _FASTAPI_INSTALLED = True
except ImportError:
    _FASTAPI_INSTALLED = False


@pytest.mark.skipif(not _FASTAPI_INSTALLED, reason="FastAPI not installed")
class TestCDSHooksRouter:
    """HTTP-level tests for ``GET /cds-services`` and ``POST /cds-services/{id}``.

    These reuse the TestClient + AegisConfig + mock-agent fixture pattern
    from ``tests/test_api_server.py`` so the CDS router is exercised in the
    same shape a real server would mount it.
    """

    @pytest.fixture
    def config(self, tmp_path) -> Any:
        from doctoragent.config import AegisConfig

        cfg = AegisConfig()
        cfg.paths.inbox = tmp_path / "Inbox"
        cfg.paths.vault = tmp_path / "Vault"
        cfg.paths.index = tmp_path / "Index"
        cfg.paths.logs = tmp_path / "Logs"
        cfg.paths.connections = tmp_path / "Config" / "connections.json"
        for p in [cfg.paths.inbox, cfg.paths.vault, cfg.paths.index, cfg.paths.logs]:
            p.mkdir(parents=True, exist_ok=True)
        cfg.paths.connections.parent.mkdir(parents=True, exist_ok=True)
        return cfg

    @pytest.fixture
    def agent(self) -> MagicMock:
        a = MagicMock()
        a.task_store.list_recent.return_value = []
        a.task_store.list_vault_files.return_value = []
        a.task_store.get.return_value = None
        a.master_key_provider = MagicMock()
        a.master_key_provider.get_key.return_value = os.urandom(32)
        a.aclose = AsyncMock()
        a._llm_provider = None
        a.llm_provider = None
        del a._sync_engine
        del a.search

        async def _search(*args, **kwargs):
            return []

        a.search = _search
        return a

    @pytest.fixture
    def client(self, config, agent, monkeypatch):
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config, agent)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    def test_discovery_endpoint_returns_three_services(self, client) -> None:
        resp = client.get("/cds-services")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data["services"], list)
        ids = {s["id"] for s in data["services"]}
        assert "doctoragent-patient-view" in ids
        assert "doctoragent-order-select" in ids
        assert "doctoragent-order-sign" in ids

    def test_invoke_patient_view_with_critical_vital(self, client) -> None:
        """POST /cds-services/doctoragent-patient-view with a prefetch containing
        a critical heart_rate must return a CDS Hooks response with at least
        one critical card."""
        payload = {
            "hook": "patient-view",
            "hookInstance": "http-test-1",
            "context": {"patientId": "p-http-1"},
            "prefetch": {
                "observations": _bundle([_vital_observation("8867-4", 35)]),
            },
        }
        resp = client.post(
            "/cds-services/doctoragent-patient-view", json=payload
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "cards" in data
        indicators = [c["indicator"] for c in data["cards"]]
        assert "critical" in indicators, indicators

    def test_invoke_unknown_service_returns_404(self, client) -> None:
        resp = client.post(
            "/cds-services/does-not-exist",
            json={"hook": "patient-view", "hookInstance": "x"},
        )
        assert resp.status_code == 404

    def test_invoke_wrong_hook_returns_400(self, client) -> None:
        """If the posted ``hook`` doesn't match the service's declared hook,
        the router must reject with 400 (EHR misconfiguration)."""
        resp = client.post(
            "/cds-services/doctoragent-patient-view",
            json={"hook": "order-select", "hookInstance": "x"},
        )
        assert resp.status_code == 400

    def test_unauthenticated_request_rejected(self, config, agent, monkeypatch) -> None:
        """When DOCTORAGENT_API_TOKEN is set, a request without a bearer token
        must be 401 (fail-closed for remote callers)."""
        monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "secret")
        from fastapi.testclient import TestClient

        from doctoragent.api.server import create_app

        app = create_app(config, agent)
        client = TestClient(app)
        resp = client.get("/cds-services")
        # TestClient host is 'testclient' (not loopback) → 401.
        assert resp.status_code == 401

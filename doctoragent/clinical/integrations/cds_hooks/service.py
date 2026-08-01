"""CDS Hooks service — bridges CDS Hook requests ↔ clinical workflow.

Pure logic: takes a parsed :class:`CDSHookRequest`, builds the
``patient_context`` dict the orchestrator expects, runs
:func:`~doctoragent.clinical.agents.workflow.run_clinical_workflow`, then maps
the :class:`~doctoragent.clinical.agents.orchestrator.ClinicalWorkflowResult`
into a :class:`CDSHookResponse` (cards with the right indicator + suggestions).

The translation rules follow CDS Hooks 2.0 §card-attributes and the project's
clinical safety contract:

* A ``critical``/``contraindicated`` rule finding → a ``critical`` card with
  a blocking suggestion (``update``/``delete`` on the draft order). When the
  finding is contraindicated the suggestion switches to ``delete`` so the EHR
  surfaces a hard-stop accept/decline.
* A ``warning`` rule finding → a ``warning`` card with the recommendation as
  the suggestion label.
* ``info`` findings + the disclaimer + the documentation draft + literature
  citations → an ``info`` card so the clinician sees supporting context.
* The ``requires_human_review`` flag is rendered as a ``warning`` card
  reminding the clinician to sign off before any action — never as a hard
  ``systemActions`` block (the orchestrator does not auto-block).

Keeping this layer pure-Python lets the same translation run inside FastAPI
(today), a worker queue (later) or unit tests (now).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from doctoragent.clinical.fhir.parser import extract_bundle_entries
from doctoragent.clinical.integrations.cds_hooks._models import (
    Action,
    Card,
    CardIndicator,
    CardSource,
    CDSHookRequest,
    CDSHookResponse,
    Suggestion,
    SupportedHook,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CDSHookService",
    "discover_services",
    "translate_request_to_workflow",
    "translate_result_to_response",
]

# Severity levels (per ClinicalRuleResult) that should become *critical* CDS
# cards — the EHR will show a hard-stop banner when present.
_BLOCKING_SEVERITIES = ("critical", "contraindicated")
_WARNING_SEVERITIES = ("warning", "high", "moderate")

# Default source attribution for every card we emit.
_DEFAULT_SOURCE = CardSource(
    label="DoctorAgent Clinical CDS",
    url=None,
    icon=None,
)

# Maximum chars of detail body — keep cards renderable in EHR side panels.
_MAX_DETAIL_CHARS = 4000


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_services() -> list[dict[str, Any]]:
    """Return the discovery-document entries for ``GET /cds-services``.

    Each entry declares the FHIR ``prefetch`` template the EHR should send so
    we can run the deterministic rule engine without an extra round-trip to
    the EHR (latency-sensitive for ``order-select``).
    """
    return [
        {
            "id": "doctoragent-patient-view",
            "hook": SupportedHook.PATIENT_VIEW.value,
            "title": "DoctorAgent Patient View CDS",
            "description": (
                "Runs the deterministic clinical rule engine (vitals / labs / "
                "DDI / allergy / duplicate therapy) plus LLM specialist "
                "review whenever a clinician opens the patient chart."
            ),
            "prefetch": {
                "patient": "Patient/{{context.patientId}}",
                "medications": ("MedicationRequest?patient={{context.patientId}}&status=active"),
                "allergies": (
                    "AllergyIntolerance?patient={{context.patientId}}&clinical-status=active"
                ),
                "conditions": ("Condition?patient={{context.patientId}}&clinical-status=active"),
                # Observations (vital-signs + laboratory) are required for the
                # deterministic rule engine to evaluate vitals / labs. Without
                # this prefetch template a conformant EHR would never send
                # Observations, leaving _extract_vitals_labs with an empty list
                # and the patient-view "vitals / labs" promise unfulfilled.
                "observations": (
                    "Observation?patient={{context.patientId}}&category=vital-signs,laboratory"
                ),
            },
        },
        {
            "id": "doctoragent-order-select",
            "hook": SupportedHook.ORDER_SELECT.value,
            "title": "DoctorAgent Order Select CDS",
            "description": (
                "Reviews the order being selected in the CPOE picker: drug "
                "interactions, allergies, dose checks and duplicate therapy "
                "against the active medication list."
            ),
            "prefetch": {
                "medications": ("MedicationRequest?patient={{context.patientId}}&status=active"),
                "allergies": (
                    "AllergyIntolerance?patient={{context.patientId}}&clinical-status=active"
                ),
            },
        },
        {
            "id": "doctoragent-order-sign",
            "hook": SupportedHook.ORDER_SIGN.value,
            "title": "DoctorAgent Order Sign CDS",
            "description": (
                "Final safety check before an order is signed. Surfaces any "
                "blocking findings as a critical accept/decline card."
            ),
            "prefetch": {
                "medications": ("MedicationRequest?patient={{context.patientId}}&status=active"),
                "allergies": (
                    "AllergyIntolerance?patient={{context.patientId}}&clinical-status=active"
                ),
            },
        },
    ]


# --------------------------------------------------------------------------- #
# Request → workflow input
# --------------------------------------------------------------------------- #
def translate_request_to_workflow(
    request: CDSHookRequest,
) -> tuple[dict[str, Any], str]:
    """Convert a :class:`CDSHookRequest` into ``(patient_context, query)``.

    Hook-specific context fields (per the CDS Hooks catalog) are mapped to the
    orchestrator's ``patient_context`` dict:

    * ``patient-view`` — ``context.patientId`` ⇒ ``patient_id``; prefetch
      resources are unpacked into ``medications`` / ``allergies`` /
      ``conditions`` (FHIR resource dicts, for the LLM specialists) AND into
      ``vitals`` / ``labs`` / ``medication_names`` / ``allergy_names`` for the
      deterministic rule engine.
    * ``order-select`` — adds ``context.draftOrders`` (a FHIR Bundle) under
      ``medications`` so DDI / duplicate-therapy rules run against the
      proposed order, plus the ``context.selections`` list so the rule engine
      can highlight the specific draft order.
    * ``order-sign`` — same as ``order-select`` but the query is framed as a
      final sign-off review.

    Vendor extensions ``context.vitals`` (dict[str, float]) and
    ``context.labs`` (list[dict]) are passed through verbatim — many EHRs
    include them in addition to (or instead of) prefetch Observations.

    The returned ``query`` is the clinical question handed to the workflow;
    it is localised (zh-CN) to match the prompt templates shipped by the
    specialist agents.
    """
    ctx = request.context or {}
    patient_id = str(ctx.get("patientId") or "")
    prefetch = request.prefetch or {}

    patient_context: dict[str, Any] = {"patient_id": patient_id or "(unknown)"}

    # ── Pass-through vendor extensions first (highest priority) ──
    # EHRs that already extract vitals/labs in context save us a round-trip.
    if isinstance(ctx.get("vitals"), dict):
        patient_context["vitals"] = dict(ctx["vitals"])
    if isinstance(ctx.get("labs"), list):
        patient_context["labs"] = list(ctx["labs"])
    # Some EHRs pass already-stringified drug/allergy lists in context.
    if isinstance(ctx.get("medications"), list):
        patient_context["medication_names"] = [str(m) for m in ctx["medications"] if m is not None]
    if isinstance(ctx.get("allergies"), list):
        patient_context["allergy_names"] = [str(a) for a in ctx["allergies"] if a is not None]

    # ── Unpack prefetch bundles ──
    # Keep raw FHIR resource dicts under the LLM-facing keys so specialist
    # agents can read the full record (dosage, encounter refs, …).
    if isinstance(prefetch.get("patient"), dict):
        patient_context["patient_resource"] = prefetch["patient"]
    observations: list[dict[str, Any]] = []
    for key, target in (
        ("medications", "medications"),
        ("allergies", "allergies"),
        ("conditions", "conditions"),
        ("observations", "observations"),
    ):
        bundle = prefetch.get(key)
        if isinstance(bundle, dict):
            entries = extract_bundle_entries(bundle)
            patient_context[target] = entries
            if key == "observations":
                observations = entries
        elif isinstance(bundle, list):
            patient_context[target] = bundle
            if key == "observations":
                observations = bundle

    # ── Extract rule-engine-ready inputs from the FHIR resources ──
    # The rule engine wants plain drug-name strings and {test, value, unit}
    # lab dicts; the LLM specialists want the full FHIR resources. Both
    # representations are needed: keep ``medications``/``allergies`` as
    # FHIR dicts for the LLM, and emit ``medication_names``/``allergy_names``
    # /``vitals``/``labs`` for the rules (only when not already provided by
    # a vendor extension above).
    med_resources = list(patient_context.get("medications") or [])
    allergy_resources = list(patient_context.get("allergies") or [])

    if "medication_names" not in patient_context:
        names = [_extract_drug_name(m) for m in med_resources]
        patient_context["medication_names"] = [n for n in names if n]
    if "allergy_names" not in patient_context:
        names = [_extract_allergen_name(a) for a in allergy_resources]
        patient_context["allergy_names"] = [n for n in names if n]
    if observations and "vitals" not in patient_context:
        vitals, labs = _extract_vitals_labs(observations)
        if vitals:
            patient_context["vitals"] = vitals
        if labs and "labs" not in patient_context:
            patient_context["labs"] = labs

    # Hook-specific enrichment.
    if request.hook in (
        SupportedHook.ORDER_SELECT.value,
        SupportedHook.ORDER_SIGN.value,
    ):
        draft = ctx.get("draftOrders")
        if isinstance(draft, dict):
            draft_meds = extract_bundle_entries(draft)
            if draft_meds:
                # Merge drafts into the active medication list so the rule
                # engine runs DDI / duplicate-therapy against the proposal.
                existing = list(patient_context.get("medications") or [])
                patient_context["medications"] = existing + draft_meds
                patient_context["draft_orders"] = draft_meds
                # And extend the rule-engine drug-name list.
                draft_names = [_extract_drug_name(m) for m in draft_meds]
                patient_context.setdefault("medication_names", []).extend(
                    n for n in draft_names if n
                )
        elif isinstance(draft, list):
            patient_context["draft_orders"] = draft
        selections = ctx.get("selections")
        if selections:
            patient_context["selections"] = selections

    # The orchestrator's rule engine reads ``patient_context['medications']``
    # / ``['allergies']`` directly (see ClinicalRuleEngine.evaluate_all) and
    # expects list[str] of drug / allergen names there. Override the FHIR-dict
    # lists with the stringified names so the rules engine gets what it wants;
    # the LLM specialists reach into the same dict via the FHIR client so they
    # don't depend on the dict shape here.
    if patient_context.get("medication_names"):
        patient_context["medications"] = list(patient_context["medication_names"])
    if patient_context.get("allergy_names"):
        patient_context["allergies"] = list(patient_context["allergy_names"])

    # Localised clinical question for the LLM specialists.
    if request.hook == SupportedHook.PATIENT_VIEW.value:
        query = (
            f"患者 {patient_id or ''} 打开就诊视图，请生成临床决策支持："
            "总结病史、识别安全风险并给出处置建议。"
        )
    elif request.hook == SupportedHook.ORDER_SELECT.value:
        query = (
            f"患者 {patient_id or ''} 正在选择医嘱，请核查药物相互作用、过敏与剂量风险，给出建议。"
        )
    elif request.hook == SupportedHook.ORDER_SIGN.value:
        query = (
            f"患者 {patient_id or ''} 即将签发医嘱，请做最终安全核查："
            "禁忌/相互作用/过敏/重复用药是否触发阻断。"
        )
    else:
        # Unknown hook — degrade gracefully with a generic question.
        query = f"临床决策支持请求 (hook={request.hook})"

    return patient_context, query


# --------------------------------------------------------------------------- #
# FHIR resource → rule-engine-input extractors
# --------------------------------------------------------------------------- #
# Reuses the parser helpers (medication_to_text / allergy_to_text) so we don't
# reinvent the FHIR→display logic. They are imported lazily inside the
# extractors so a partial clinical install (parser missing) never breaks
# the translator — we just degrade to empty strings.


def _extract_drug_name(med: dict[str, Any]) -> str:
    """Best-effort drug-name extraction from a FHIR MedicationRequest.

    Uses :func:`doctoragent.clinical.fhir.parser.medication_to_text` to render
    ``"二甲双胍 500mg bid, active"`` and returns just the leading drug name
    (everything before the first whitespace) so DDI / duplicate-therapy
    rules see a clean ``"二甲双胍"`` rather than a formatted dose string.
    """
    if not isinstance(med, dict):
        return ""
    try:
        from doctoragent.clinical.fhir.parser import medication_to_text
    except ImportError:  # pragma: no cover — defensive
        return ""
    text = medication_to_text(med)
    if not text:
        return ""
    # Take the leading token as the drug name. Multi-word names (e.g.
    # "对乙酰氨基酚") collapse to a single CJK token anyway; for ASCII names
    # like "metformin HCl" we keep only the first word to avoid confusing
    # the substring-matching rules engine.
    name = text.split()[0]
    # Strip trailing punctuation left by the status suffix
    # (medication_to_text appends ", active" so the first token can end up
    # as "二甲双胍,"). Use a small whitelist of safe chars instead of a
    # generic punctuation strip so we don't mangle drug names that contain
    # legitimate symbols.
    return name.rstrip(",;:.").strip()


def _extract_allergen_name(allergy: dict[str, Any]) -> str:
    """Best-effort allergen extraction from a FHIR AllergyIntolerance.

    Uses :func:`doctoragent.clinical.fhir.parser.allergy_to_text` to render
    ``"青霉素(皮疹)"`` and returns the allergen prefix (everything before the
    first ``(``) so cross-reactivity matching sees ``"青霉素"``.
    """
    if not isinstance(allergy, dict):
        return ""
    try:
        from doctoragent.clinical.fhir.parser import allergy_to_text
    except ImportError:  # pragma: no cover — defensive
        return ""
    text = allergy_to_text(allergy)
    if not text:
        return ""
    # Strip the reaction-manifestation suffix: "青霉素(皮疹)" → "青霉素".
    return text.split("(", 1)[0].strip()


# Terminology binding is delegated to the central terminology package so
# every layer (CDS Hooks, FHIR parser, rules engine, LLM prompts) shares
# one canonical LOINC → test-name map. See
# :mod:`doctoragent.clinical.terminology` for the curated map + bulk-table loader.
from doctoragent.clinical.terminology.loinc_map import (  # noqa: E402 — after stdlib import
    VITAL_LOINC_CODES,
    extract_first_loinc_code,
    lookup_loinc_test_name,
)


def _extract_vitals_labs(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Split a list of FHIR Observations into ``(vitals, labs)``.

    * ``vitals`` — ``{test_name: float_value}`` for vital-signs Observations
      whose LOINC code is in :data:`LOINC_TO_REFERENCE_RANGES_TEST`.
    * ``labs`` — ``[{test, value, unit, id?}]`` for laboratory Observations
      with a numeric ``valueQuantity``.

    Observations without a recognised LOINC code or without a numeric value
    are skipped. The category check uses ``Observation.category.coding.code``
    when present, falling back to the LOINC code whitelist for vitals.
    """
    vitals: dict[str, float] = {}
    labs: list[dict[str, Any]] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        code_info = obs.get("code") or {}
        loinc_code = extract_first_loinc_code(code_info)
        if not loinc_code:
            continue
        value_q = obs.get("valueQuantity")
        if not isinstance(value_q, dict):
            continue
        try:
            value = float(value_q.get("value"))
        except (TypeError, ValueError):
            continue
        unit = value_q.get("unit") or ""
        test_name = lookup_loinc_test_name(loinc_code)
        if test_name is None:
            continue
        is_vital = _is_vital_signs(obs) or loinc_code in VITAL_LOINC_CODES
        if is_vital:
            vitals[test_name] = value
        else:
            labs.append(
                {
                    "test": test_name,
                    "value": value,
                    "unit": unit,
                    "id": obs.get("id") or test_name,
                }
            )
    return vitals, labs


def _is_vital_signs(obs: dict[str, Any]) -> bool:
    """Return True if the Observation has ``category=vital-signs``."""
    categories = obs.get("category") or []
    if not isinstance(categories, list):
        return False
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        for coding in cat.get("coding") or []:
            if isinstance(coding, dict):
                code = coding.get("code")
                if code == "vital-signs":
                    return True
    return False


# --------------------------------------------------------------------------- #
# Workflow result → CDS response
# --------------------------------------------------------------------------- #
def translate_result_to_response(
    request: CDSHookRequest,
    result: Any,
) -> CDSHookResponse:
    """Map a :class:`ClinicalWorkflowResult` to a :class:`CDSHookResponse`.

    See module docstring for the indicator/suggestion mapping rules.
    """
    cards: list[Card] = []
    system_actions: list[Action] = []

    findings = list(getattr(result, "safety_findings", []) or [])
    requires_review = bool(getattr(result, "requires_human_review", False))
    history_summary = str(getattr(result, "history_summary", "") or "")
    documentation = getattr(result, "documentation", None) or {}
    literature = list(getattr(result, "literature", []) or [])
    citations = list(getattr(result, "citations", []) or [])
    disclaimer = str(getattr(result, "disclaimer", "") or "")

    # ── Blocking findings → critical cards with blocking suggestions ──
    blocking = [f for f in findings if f.get("severity") in _BLOCKING_SEVERITIES]
    warnings = [
        f for f in findings if f.get("severity") in _WARNING_SEVERITIES and f not in blocking
    ]
    info_findings = [f for f in findings if f not in blocking and f not in warnings]

    for finding in blocking:
        cards.append(_critical_finding_card(request, finding))

    for finding in warnings:
        cards.append(_warning_finding_card(request, finding))

    # ── requires_human_review flag → a prominent warning reminder ──
    if requires_review:
        cards.append(
            Card(
                uuid=_new_uuid(),
                summary="⚠ 临床人工复核必需 (requires_human_review)",
                detail=(
                    "本次决策需要执业医师签发后才能执行。\n\n"
                    f"**病史摘要**:\n{history_summary[:1200]}"
                    + (f"\n\n**免责声明**: {disclaimer}" if disclaimer else "")
                )[:_MAX_DETAIL_CHARS],
                indicator=CardIndicator.WARN,
                source=_DEFAULT_SOURCE,
            )
        )

    # ── Info card: documentation draft + literature citations ──
    doc_draft = ""
    if isinstance(documentation, dict):
        doc_draft = str(documentation.get("draft") or "")

    lit_lines: list[str] = []
    for idx, item in enumerate(literature[:5], start=1):
        if isinstance(item, dict):
            summary = item.get("summary") or item.get("title") or ""
            cite = item.get("citation") or item.get("pmid") or ""
            lit_lines.append(f"{idx}. {summary}" + (f" [{cite}]" if cite else ""))
    if citations:
        lit_lines.append("**引证**: " + "; ".join(str(c) for c in citations[:10]))

    if doc_draft or lit_lines or info_findings:
        detail_parts: list[str] = []
        if doc_draft:
            detail_parts.append(f"**病历草稿 (待医生签发)**:\n{doc_draft[:1500]}")
        if lit_lines:
            detail_parts.append("**文献/指南**:\n" + "\n".join(lit_lines))
        if info_findings:
            detail_parts.append(
                "**其他安全提示**:\n"
                + "\n".join(f"- {_fmt_finding(f, with_rec=True)}" for f in info_findings)
            )
        if disclaimer:
            detail_parts.append(f"**免责声明**: {disclaimer}")
        cards.append(
            Card(
                uuid=_new_uuid(),
                summary="临床决策支持参考信息",
                detail="\n\n".join(detail_parts)[:_MAX_DETAIL_CHARS] or None,
                indicator=CardIndicator.INFO,
                source=_DEFAULT_SOURCE,
            )
        )

    # No systemActions — the orchestrator never silently mutates the order;
    # every clinical decision must be accepted (or dismissed) by a human.
    return CDSHookResponse(cards=cards, systemActions=system_actions or None)


def _critical_finding_card(request: CDSHookRequest, finding: dict[str, Any]) -> Card:
    """Build a critical card for a blocking rule finding."""
    severity = finding.get("severity") or "critical"
    rule_type = finding.get("rule_type") or "safety"
    finding_text = finding.get("finding") or "阻断性安全发现"
    recommendation = finding.get("recommendation") or ""
    suggestion_label = recommendation or "阻断签发并复核患者"

    # Contraindicated findings propose deleting the draft order; critical
    # ones propose holding it for revision.
    is_contra = severity == "contraindicated"
    action_type = "delete" if is_contra else "update"
    action_desc = "删除该医嘱（禁忌用药）" if is_contra else "暂缓签发并修改医嘱"

    # When the request carries draft orders, attach the original draft
    # resource so the EHR can rollback / pre-fill the editor.
    draft_resource: dict[str, Any] | None = None
    draft_orders = request.context.get("draftOrders") if isinstance(request.context, dict) else None
    if isinstance(draft_orders, dict):
        entries = extract_bundle_entries(draft_orders)
        if entries:
            draft_resource = entries[0]

    return Card(
        uuid=_new_uuid(),
        summary=f"⛔ 阻断：{finding_text[:120]}",
        detail=(
            f"**规则类型**: {rule_type}\n"
            f"**严重程度**: {severity}\n"
            f"**发现**: {finding_text}\n"
            + (f"**建议**: {recommendation}\n" if recommendation else "")
            + "该发现由确定性规则引擎产生，必须由执业医师复核后才能继续。"
        )[:_MAX_DETAIL_CHARS],
        indicator=CardIndicator.CRITICAL,
        source=_DEFAULT_SOURCE,
        suggestions=[
            _block_suggestion(suggestion_label, action_type, action_desc, draft_resource),
        ],
        overrideReasons=[
            {"code": "clinician-override", "detail": "医师判断覆盖规则阻断"},
        ],
    )


def _warning_finding_card(request: CDSHookRequest, finding: dict[str, Any]) -> Card:
    """Build a warning card for a non-blocking safety finding."""
    rule_type = finding.get("rule_type") or "safety"
    finding_text = finding.get("finding") or "安全提示"
    recommendation = finding.get("recommendation") or ""

    return Card(
        uuid=_new_uuid(),
        summary=f"⚠ {finding_text[:120]}",
        detail=(
            f"**规则类型**: {rule_type}\n"
            f"**发现**: {finding_text}\n"
            + (f"**建议**: {recommendation}" if recommendation else "")
        )[:_MAX_DETAIL_CHARS],
        indicator=CardIndicator.WARN,
        source=_DEFAULT_SOURCE,
        suggestions=[
            Suggestion(
                label=recommendation or "已知悉并继续",
                uuid=_new_uuid(),
                actions=[],
            )
        ],
    )


def _block_suggestion(
    label: str,
    action_type: str,
    action_desc: str,
    resource: dict[str, Any] | None,
) -> Any:
    """Build the accept-or-decline suggestion that blocks the order."""
    return Suggestion(
        label=label[:140] or "阻断并复核",
        uuid=_new_uuid(),
        actions=[
            Action(
                type=action_type,
                description=action_desc,
                resource=resource,
            )
        ],
    )


def _fmt_finding(finding: dict[str, Any], *, with_rec: bool = False) -> str:
    """One-line summary of a finding for inclusion in an info card body."""
    rule_type = finding.get("rule_type") or ""
    text = finding.get("finding") or ""
    severity = finding.get("severity") or ""
    parts = [p for p in (severity, rule_type, text) if p]
    out = " ".join(parts)
    if with_rec:
        rec = finding.get("recommendation") or ""
        if rec:
            out = f"{out} — {rec}"
    return out


def _new_uuid() -> str:
    """Return a fresh UUID4 hex string for card / suggestion correlation."""
    return uuid4().hex


# --------------------------------------------------------------------------- #
# Top-level service façade
# --------------------------------------------------------------------------- #
class CDSHookService:
    """High-level façade combining discovery + workflow dispatch.

    Encapsulates the collaborators a FastAPI router needs:

    * ``llm_provider`` — optional; the workflow degrades to rules-only when
      ``None`` (matches the existing API server behaviour).
    * ``fhir_client`` — optional; forwarded to ``run_clinical_workflow`` so
      specialist agents can issue additional reads beyond the prefetch.
    * ``audit_logger`` — optional tamper-evident logger; every CDS invocation
      is audited as ``clinical_decision`` (already done inside the
      orchestrator) and additionally as ``cds_hooks_invocation`` here so
      EHR-side telemetry is reconstructable.

    The service is intentionally stateless — every ``invoke`` call is
    independent, so multiple EHR tenants can share one instance.
    """

    def __init__(
        self,
        llm_provider: Any = None,
        fhir_client: Any = None,
        audit_logger: Any = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.fhir_client = fhir_client
        self.audit_logger = audit_logger

    # -- discovery --------------------------------------------------------- #
    @staticmethod
    def services() -> list[dict[str, Any]]:
        """Return the discovery-document list (``GET /cds-services``)."""
        return discover_services()

    # -- invocation -------------------------------------------------------- #
    async def invoke(self, request: CDSHookRequest) -> CDSHookResponse:
        """Run the clinical workflow for *request* and return cards."""
        # Lazy import keeps the module importable when the clinical package
        # is partially broken (e.g. fhir.resources missing).
        from doctoragent.clinical.agents.workflow import run_clinical_workflow

        patient_context, query = translate_request_to_workflow(request)
        self._audit_invocation(request, patient_context)
        # SMART-on-FHIR: prefer the EHR-issued bearer token (in
        # ``fhirAuthorization.access_token``) over the server's statically
        # configured FHIR client. The EHR token carries the patient scope
        # the clinician's session was launched with, so specialist agents
        # issuing additional FHIR reads respect the same consent boundary.
        fhir_client = self._resolve_fhir_client(request)
        try:
            result = await run_clinical_workflow(
                patient_context=patient_context,
                query=query,
                llm_provider=self.llm_provider,
                fhir_client=fhir_client,
                audit_logger=self.audit_logger,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the EHR call
            logger.exception("CDS Hooks workflow invocation failed")
            return self._error_response(request, exc)
        finally:
            # Close any ephemeral SMART-token-scoped client we created so
            # the underlying httpx connection pool is released.
            if fhir_client is not self.fhir_client and fhir_client is not None:
                close = getattr(fhir_client, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        logger.debug("ephemeral fhir_client close failed", exc_info=True)
        return translate_result_to_response(request, result)

    def _resolve_fhir_client(self, request: CDSHookRequest) -> Any:
        """Return the FHIR client to use for this CDS invocation.

        Priority:
        1. EHR-issued ``fhirAuthorization.access_token`` (SMART-on-FHIR) →
           build an ephemeral :class:`FHIRClient` bound to
           ``request.fhirServer`` (when present) with that bearer token.
        2. The statically-injected ``self.fhir_client`` (server-configured).

        The ephemeral client is closed in :meth:`invoke`'s ``finally`` block
        so one token doesn't leak across invocations.
        """
        fhir_auth = request.fhirAuthorization
        if not isinstance(fhir_auth, dict):
            return self.fhir_client
        access_token = fhir_auth.get("access_token")
        if not access_token:
            return self.fhir_client
        base_url = request.fhirServer or ""
        if not base_url:
            # Without a fhirServer we cannot build a client; fall back.
            return self.fhir_client
        try:
            from doctoragent.clinical.fhir.client import FHIRClient
        except ImportError:  # pragma: no cover — defensive
            return self.fhir_client
        try:
            return FHIRClient(base_url=base_url, auth_token=str(access_token))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to build ephemeral SMART FHIRClient; falling back to static client: %s",
                exc,
            )
            return self.fhir_client

    # -- internals --------------------------------------------------------- #
    def _audit_invocation(self, request: CDSHookRequest, patient_context: dict[str, Any]) -> None:
        if self.audit_logger is None:
            return
        try:
            self.audit_logger.log(
                "cds_hooks_invocation",
                {
                    "hook": request.hook,
                    "hook_instance": request.hookInstance,
                    "patient_id": patient_context.get("patient_id"),
                    "fhir_server": request.fhirServer,
                    # ``fhirAuthorization`` deliberately NOT logged — it
                    # contains the SMART bearer token (PHI/credential).
                },
            )
        except Exception:  # noqa: BLE001 — audit must never break CDS path
            logger.warning("audit log write failed for cds_hooks_invocation", exc_info=True)

    @staticmethod
    def _error_response(request: CDSHookRequest, exc: BaseException) -> CDSHookResponse:
        """Build a critical card describing the failure so the EHR can surface it.

        EHRs are encouraged to treat a single critical card with a server-error
        summary as a non-blocking warning — we keep the action empty so the
        clinician is never auto-blocked by an internal failure.
        """
        return CDSHookResponse(
            cards=[
                Card(
                    uuid=_new_uuid(),
                    summary="⛔ CDS 服务调用失败，请人工核查",
                    detail=(
                        f"hook: {request.hook}\n"
                        f"hookInstance: {request.hookInstance}\n"
                        f"错误: {type(exc).__name__}: {exc}\n\n"
                        "建议：联系系统管理员并人工复核该决策。"
                    )[:_MAX_DETAIL_CHARS],
                    indicator=CardIndicator.CRITICAL,
                    source=_DEFAULT_SOURCE,
                )
            ]
        )

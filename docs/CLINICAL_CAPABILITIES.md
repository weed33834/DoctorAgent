# Clinical AI Capabilities

> Compliance-first, on-premise, auditable clinical AI agent.
> This document describes the clinical layer shipped on top of the DoctorAgent core
> (encryption / audit / RBAC / RAG / agent framework) and is the authoritative
> reference for what the agent does — and does **not** — claim to do.

The clinical layer lives under `doctoragent/clinical/` and is gated behind the
optional `clinical` extra:

```bash
pip install -e ".[clinical]"
```

All clinical code imports defensively — `import doctoragent.clinical` always
succeeds; helpers raise a clear `ImportError` with install instructions only
when actually invoked without their backing library.

---

## 1. What it is

DoctorAgent Clinical is **not** a diagnostic product. It is a clinical decision
support and documentation framework that can be deployed on its own
infrastructure, with PHI never leaving the trust boundary unless explicitly
authorized. Every output carries a disclaimer: **clinical suggestions do not
replace physician judgement**.

Three design pillars:

1. **Compliance is a feature, not a slide** — encryption, audit log, RBAC,
   OIDC SSO, KMS, DLP and PHI de-identification are built in and testable.
2. **Deterministic safety beats LLM reasoning** — drug interactions, allergy
   cross-reactivity, duplicate therapy, vital/lab critical values are decided
   by pure-logic rules; the LLM only reasons *on top of* those findings.
3. **Citations are mandatory** — every clinical suggestion must reference a
   FHIR resource ID, a PMID, or a guideline entry; uncited answers are
   downgraded to "needs clinician confirmation".

---

## 2. Architecture

```
                    clinical request
                          │
                          ▼
            ┌──────────────────────────────┐
            │   ClinicalOrchestrator        │  fan-out / fan-in
            └──────────────────────────────┘
                │     │     │     │
                ▼     ▼     ▼     ▼
            Patient Drug  Lit-  Docu-
            History Safety erat- ment
            Agent   Agent  ure   Agent
                │     │     │     │
                └─────┴──┬──┴─────┘
                         ▼
            ┌──────────────────────────────┐
            │   ClinicalRuleEngine          │  deterministic
            │   (vitals / labs / DDI /      │  safety layer
            │    allergy / duplicate)       │
            └──────────────────────────────┘
                         │
                         ▼
            ┌──────────────────────────────┐
            │   ClinicalGuardrails          │  LLM-output
            │   (citations / forbidden /    │  validator
            │    PHI / prompt injection)    │
            └──────────────────────────────┘
                         │
                         ▼
            ClinicalWorkflowResult  (+ disclaimer + citations +
                                      requires_human_review flag)
```

The orchestrator deliberately composes specialist agents rather than
re-planning via the LLM — the clinical workflow is a fixed, auditable DAG.

---

## 3. Module map

| Module | Purpose |
|---|---|
| `clinical/fhir/client.py` | Async FHIR R4 REST client with SMART-on-FHIR bearer auth, retry + OperationOutcome translation |
| `clinical/fhir/resources.py` | HL7-official `fhir.resources` pydantic parse / serialize / validate |
| `clinical/fhir/parser.py` | EHR → text serialization for the LLM context window |
| `clinical/knowledge/openfda.py` | openFDA drug label / adverse-event client |
| `clinical/knowledge/rxnorm.py` | RxNorm drug-name normalization client |
| `clinical/knowledge/drug_interactions.py` | DDI engine built on openFDA + RxNorm |
| `clinical/knowledge/pubmed.py` | PubMed E-utilities literature search client |
| `clinical/safety/reference_ranges.py` | Vital / lab reference + critical ranges |
| `clinical/safety/rules.py` | Deterministic rule engine: vitals / labs / DDI / allergy / duplicate therapy |
| `clinical/safety/guardrails.py` | LLM-output guardrails: citation / forbidden content / PHI leakage / prompt injection |
| `clinical/tools/registry.py` | 15-tool clinical registry factory |
| `clinical/agents/` | 4 specialist agents + orchestrator + workflow entry point |
| `clinical/deidentification.py` | HIPAA Safe Harbor PHI de-identification pipeline |
| `clinical/compliance_report.py` | HIPAA compliance self-check report |

---

## 4. The 15 clinical tools

Every tool registers with a five-level side-effect annotation so the
orchestrator can decide what needs human confirmation:

| Tool | Side-effect | Purpose |
|---|---|---|
| `read_patient_record` | read-only | FHIR Patient / Condition / Encounter |
| `read_medications` | read-only | FHIR MedicationRequest / Dispense |
| `read_allergies` | read-only | FHIR AllergyIntolerance |
| `read_lab_results` | read-only | FHIR Observation (lab) |
| `check_drug_interactions` | read-only (network) | openFDA + RxNorm DDI |
| `search_clinical_guidelines` | read-only | PubMed / guideline search |
| `search_literature` | read-only | PubMed literature search |
| `check_vitals` | read-only | Vital-sign rule evaluation |
| `check_lab_ranges` | read-only | Lab-value abnormality evaluation |
| `generate_differential_diagnosis` | safe-write | LLM differential diagnosis with confidence + evidence |
| `generate_soap_note` | safe-write | SOAP note draft |
| `code_icd10` | safe-write | ICD-10 auto-coding suggestion |
| `write_clinical_note` | **destructive-write** (human-in-loop) | FHIR DocumentReference write |
| `flag_safety_alert` | safe-write | Clinical safety alert flag |
| `compliance_self_check` | read-only | HIPAA compliance self-check report |

---

## 5. Safety model

### 5.1 Deterministic rule engine

`ClinicalRuleEngine.evaluate_all(patient_context)` runs pure-logic rules and
returns `ClinicalRuleResult` objects. Severities:

- `info` — informational
- `warning` — advisory
- `critical` — **blocking**; clinician must acknowledge before downstream automation
- `contraindicated` — **blocking**; do not proceed without specialist review

Blocking severities always set `requires_human_review = True` on the workflow
result, regardless of what the LLM suggested.

### 5.2 LLM-output guardrails

`ClinicalGuardrails.evaluate(text)` runs four detectors and takes the
strictest action:

| Detector | Triggers |
|---|---|
| `check_citations` | Missing PMID / FHIR reference → `flag` |
| `check_forbidden_content` | Definitive diagnosis / overdose / unsupervised disposition → `block` |
| `check_phi_leakage` | Phone / MRN / SSN in output → `block` |
| `check_prompt_injection` | "Ignore previous instructions" / role override → `block` |

### 5.3 Human-in-the-loop writes

`write_clinical_note` and any FHIR write require explicit human confirmation
via the existing `human_in_loop` + checkpoint machinery. The orchestrator
never writes back to the EHR autonomously.

---

## 6. External dependencies (no wheel reinvention)

| Capability | Library / API | Source |
|---|---|---|
| FHIR R4 resource model | `fhir.resources` (pydantic) | HL7 official open-source list |
| Structured LLM output | `instructor` (wraps OpenAI SDK) | validates clinical specialist pydantic models |
| LLM backend | `openai` SDK (OpenAI-compatible /v1) | required by instructor ≥1.13 |
| Declarative agent DAG | `langgraph` (compiled immutable StateGraph) | rules → parallel specialists → documentation → guardrail |
| SMART-on-FHIR launch | `authlib` (PKCE S256 code challenge) | also satisfied by the `auth` extra |
| Drug labels / adverse events | openFDA REST API | FDA official |
| Drug-name normalization | RxNorm REST API | NLM official |
| Drug-drug interactions | openFDA + RxNorm | RxCUI-based equivalence detection |
| Literature search | PubMed E-utilities | NLM official |
| HTTP client | `httpx` (core dep) | already used |
| Retry policy | `tenacity` (core dep) | already used |
| Embedding / vector store | `sentence-transformers` / SQLite / Chroma | already used |
| LLM provider | Ollama (local) / OpenAI-compatible | already used |

The clinical layer is gated behind the optional `clinical` extra, which
adds `fhir.resources`, `instructor`, `openai`, `langgraph` and `authlib`
on top of the core stack. openFDA / RxNorm / PubMed are consumed via the
core `httpx` dependency — no dedicated SDK is required. See
`[project.optional-dependencies] clinical` in `pyproject.toml` for the
exact pin ranges.

---

## 7. Compliance posture

The clinical layer inherits the DoctorAgent compliance stack and adds
healthcare-specific controls:

- **Encryption at rest + in transit** — AES-256-GCM field-level encryption;
  TLS for FHIR / openFDA / RxNorm / PubMed calls.
- **Tamper-evident audit log** — HMAC-SHA256 per entry, key rotation;
  every FHIR read / DDI query / safety rule firing is traceable.
- **RBAC + OIDC SSO** — clinical operations are scoped to roles
  (`clinician`, `pharmacist`, `auditor`, `admin`).
- **KMS integration** — AWS / Azure / GCP KMS for master-key custody.
- **DLP + PHI de-identification** — 10 core clinical HIPAA Safe Harbor
  identifier categories (patient name, MRN, DOB, phone, email, SSN,
  address, medical record, dates, IP address) are detected and
  redacted / pseudonymized / masked before content leaves the trust
  boundary. The remaining 9 Safe Harbor classes (fax, account/license
  numbers, vehicle/device IDs, URLs, biometric, full-face photos,
  Chinese ID card numbers) are handled by the de-identification module
  and the upstream DLP scanner where relevant patterns exist.
- **Compliance self-check** — `compliance_self_check` tool produces a
  one-shot HIPAA posture report that an auditor can export as evidence.

---

## 8. Evaluation suite

`tests/clinical/test_clinical_evaluation.py` ships **22 golden cases**
across five categories:

| Category | Count | Examples |
|---|---|---|
| Safety | 6 | Critical vitals, critical labs, allergy cross-reaction, duplicate therapy, safe patient, warfarin + fluconazole |
| Compliance | 4 | Cross-patient record retrieval, PHI dump, guardrail-skip attempt, disclaimer-removal attempt |
| Citation | 3 | Missing citation → flag, PMID → allow, FHIR reference → allow |
| Adversarial — prompt injection | 4 | Chinese injection, English injection + controlled-substance request, `[UNTRUSTED]` payload, role override |
| Adversarial — PII extraction | 2 | Phone + SSN extraction, raw MRN extraction |
| Workflow | 3 | Orchestrator on safe patient, critical-vitals review trigger, degraded no-LLM path |

Each case is judged on **three layers**:

1. **Regex blacklist** — patterns in `must_not_contain` must not appear.
2. **Semantic must-have** — substrings in `must_contain` must all appear.
3. **Guardrail action + review flag** — must match the expected values.

The judge never uses the same model family as the system under test.

Synthetic FHIR R4 fixtures live in `tests/fixtures/clinical/` (six scenarios:
safe, drug-interaction, allergy-alert, critical-vitals, critical-labs,
duplicate-therapy). All names, MRNs and clinical values are fake.

### 8.1 Clinical QA benchmark (Phase-C3)

`doctoragent/clinical/evaluation/` ships a dependency-light benchmark
engine that measures the model-under-test against standard clinical QA
datasets (**MedQA** — USMLE-style MCQ, **PubMedQA** — yes/no/maybe
classification) and reports a multi-dimensional score card:

| Dimension | Metric |
|---|---|
| Accuracy | exact-match / letter-match (MCQ), macro-F1 + per-class (classification) |
| Calibration | Expected Calibration Error (ECE) + Brier score |
| Safety | guardrail survival rate + correct-refusal rate on unsafe queries |
| Citation | fraction of answers carrying a verifiable citation |
| Latency | p50 / p95 wall-clock per case |
| LLM-judge | cross-family LLM-as-judge score (judge MUST differ from the MUT family) |

Loaders resolve MedQA/PubMedQA from a local JSONL file → HuggingFace
`datasets` → built-in sample data (5+5 hand-curated cases, zero-network).
The `BenchmarkRunner` accepts any `Predictor` — raw LLM, the full
`run_clinical_workflow` stack, or a deterministic stub — and computes the
post-hoc guardrail action for raw-LLM predictors so the safety metric is
meaningful even without workflow integration. When no judge LLM is
configured the judge degrades to a token-Jaccard fallback scorer so the
benchmark is fully runnable offline. **PHI note:** the LLM judge sends
case context to an external LLM — only safe for the public datasets;
real patient data must be de-identified first.

---

## 9. What it does **not** do

- **No autonomous diagnosis.** Every output is advisory and ships with a
  disclaimer; definitive diagnosis and disposition are deferred to a
  licensed clinician.
- **No bypass of guardrails.** Even with an LLM configured, blocking
  findings cannot be overridden by the model.
- **No PHI exfiltration.** External API calls (openFDA / RxNorm / PubMed)
  run after de-identification; raw PHI never leaves the trust boundary.
- **No closed-loop prescribing or order entry.** `write_clinical_note` and
  any FHIR write are gated behind human confirmation.

---

## 10. Quick start (clinical)

```python
import asyncio
from doctoragent.clinical.agents import run_clinical_workflow

patient_context = {
    "patient_id": "synthetic-001",
    "medications": ["Warfarin 5mg PO", "Fluconazole 200mg PO"],
    "allergies": ["Penicillin"],
    "vitals": {"heart_rate": 78, "systolic_bp": 130, "diastolic_bp": 85},
    "labs": [{"test": "potassium", "value": 4.2, "unit": "mmol/L"}],
}

result = asyncio.run(run_clinical_workflow(
    patient_context=patient_context,
    query="该患者用药是否安全？",
    llm_provider=None,           # set to your local Ollama provider for full path
))
print(result.requires_human_review)
print(result.safety_findings)
print(result.disclaimer)
```

With `llm_provider=None` the agent still runs — only the deterministic
rule engine and the guardrails fire, and the result carries
`requires_human_review = True`. This is the safe default for environments
where no LLM is approved for clinical use.

---

## 11. Roadmap

- **Stage 1 (shipped)** — FHIR R4 adapter, deterministic rule engine,
  LLM-output guardrails, 15 clinical tools, 4 specialist agents + workflow
  orchestration, PHI de-identification (10 core clinical categories),
  compliance self-check, 22-case evaluation suite, 6 synthetic FHIR fixtures,
  SMART-on-FHIR v2 launch flow (OAuth2 + PKCE, scope verification), CDS
  Hooks 2.0 router (patient-view / order-select / order-sign),
  **LangGraph multi-agent orchestration** (compiled immutable StateGraph;
  rules → parallel specialists → documentation → guardrail), **DeepEval
  RAG evaluation** (Faithfulness / Answer Relevancy / Context Precision /
  Context Recall with deterministic fallback), **full console visibility**
  for every differentiator (PHI de-identification, rule inspection, citation
  verification, agent-graph topology, SMART-on-FHIR launch, MCP tool
  exposure).
- **Stage 2 (pilot)** — connect to an open-source FHIR server (HAPI FHIR /
  IBM FHIR Server) via the operator-configured `clinical.fhir_base_url`;
  validate RAG against MIMIC-IV public notes; one-clinic pilot deployment.
  (Code-ready: `FHIRClient` + `run_clinical_workflow` accept a live FHIR
  endpoint out of the box; pilot verification pending.)
- **Stage 3 (advanced)** — HIPAA / 等保三级 certification; commercial EMR
  (Epic / Cerner) FHIR adapter & vendor-specific SMART profiles.

---

## 12. Disclaimer

DoctorAgent Clinical is a clinical decision support and documentation framework.
It is **not** a medical device and does not provide medical advice, diagnosis
or treatment. All outputs require review and approval by a licensed clinician
before any clinical action is taken.

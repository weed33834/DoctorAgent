# DoctorAgent

Open-source clinical decision support platform for healthcare teams who want their AI to behave like a colleague, not a chatbot.

[中文](README.zh.md) · [日本語](README.ja.md)

---

## What this is

DoctorAgent is a self-hostable platform that combines a deterministic safety engine with multi-agent LLM reasoning to help clinicians with medication review, literature search, PHI handling, and documentation.

Three things make it different from the other 47 "medical AI" projects on GitHub:

1. **Safety rules run offline.** Five deterministic checks (critical vitals, lab values, drug-drug interactions, allergy cross-reactivity, duplicate therapy) execute locally without any LLM call. When the rule engine and the LLM disagree, the rule engine wins. Always.
2. **Your data stays on your hardware.** Vault content is encrypted at rest with AES-256-GCM (key derived via PBKDF2-SHA256/Argon2id). Audit log entries are HMAC-SHA256 signed and chained. The server can run air-gapped.
3. **Standards integration is real, not aspirational.** CDS Hooks 2.0 endpoints, FHIR R4 resource handlers, SMART-on-FHIR auth, SNOMED CT / LOINC / ICD-10-CM terminology binding are wired through the code path, not stuck in a roadmap slide.

The current release is v0.3.1, marked as Beta. It's stable enough for pilot deployments but the project is honest about what it isn't: there's no FDA 510(k), no HIPAA certification, no 等保三级 attestation. Those are roadmap goals, written down so you can plan against them.

---

## Demo

[Watch the 40-second walkthrough →](assets/demo/demo.mp4)

Or browse the screenshots below. They're from the actual console running against a local SQLite + Ollama instance — no demo data was faked for the README.

| Clinical workstation | Safety rules engine |
|:---:|:---:|
| ![Clinical workstation with vitals, allergies and medications](assets/screenshots/02-clinical.png) | ![Deterministic safety rules with 5 categories and severity tags](assets/screenshots/03-safety-rules.png) |

| PHI de-identification | Multi-tenant management |
|:---:|:---:|
| ![PHI redaction with original/highlighted/redacted comparison and type distribution](assets/screenshots/04-phi-deidentify.png) | ![Tenant cards with isolation providers and active accounts](assets/screenshots/06-tenants.png) |

| System dashboard |
|:---:|
| ![System status with running state, model config and resource pool](assets/screenshots/05-system-status.png) |

---

## Why we built it

LLM-powered medical assistants are everywhere. Most of them:

- Hallucinate drug doses when the prompt is slightly out of distribution.
- Send patient notes to a third-party API by default with no opt-out.
- Lose their safety reasoning the moment you turn off "creative mode".
- Have a single point of audit (the prompt log) that anyone with shell access can rewrite.

We wanted a system where:

- A clinician can verify "did the model check drug interactions for this patient?" by clicking one button and reading an immutable record.
- An IT admin can point the system at a local Ollama and turn off all outbound traffic.
- A compliance officer can prove what the model saw and decided during a particular encounter on a particular date.

None of that is groundbreaking engineering. It's mostly boring discipline — FHIR resources, HMAC chains, audit logs, role-based access. We did it anyway because the alternatives we tried kept breaking under audit.

---

## Architecture in one paragraph

A FastAPI server hosts both an API and a static console (the single-page web app under `doctoragent/api/static/console`). Inbound clinical documents go through a classifier → an AES-256-GCM encrypted store → a SQLite-backed FTS5 index + a vector index → a retrieval pipeline (HyDE + RRF + cross-encoder rerank) → an LLM agent with a fixed DAG of specialists (病史 → 用药 → 文献 → 文书). Every step writes to an HMAC-chained audit log. A scheduler retries timeouts with priority escalation. UI ships as 27 modules: chat, clinical workspace, PHI tools, vault, RAG, knowledge graph, memory, prompts, DAG, eval, self-evolution, RL, multi-agent collab, config, connections, tenants, system status, audit, compliance, ops, settings, hooks, observability, plugins, A/B experiments, plus two view toggles (clinician vs admin).

The fixed-DAG agent design is non-negotiable for us — once a clinical workflow is compiled, the LLM cannot reroute itself around a safety step. This is what most "agent frameworks" deliberately allow and what we explicitly prevent in the clinical pipeline.

---

## Quick start

You need Python 3.10+ and about 200MB of disk.

```bash
git clone https://gitcode.com/badhope/DoctorAgent.git
cd DoctorAgent
pip install -e ".[server]"
bash start.sh
```

Then open <http://127.0.0.1:8000/console/> in a browser. No LLM key required for the safety rules engine and the document vault — they work fully offline. Add an Ollama or cloud LLM at `/console/` → "连接" to unlock the agent features.

### Verification that you didn't get a broken checkout

```bash
pytest tests/ -q
```

We expect `2314 passed`. If the number drops, something broke. Don't ship that build.

---

## What you can build on it

The platform is MIT licensed and explicitly designed to be extended. The hooks system has 15 trigger points (request_received, before_tool_call, after_llm_response, before_audit_write, etc.) where you can attach Python scripts. The plugin manager registers additional entry points at startup.

A few directions people are already exploring in the issue tracker:

- Connecting CDS Hooks to Epic or Cerner (we ship a FHIR R4 adapter; the EHR-side glue is yours)
- Replacing the default LLM with a fine-tuned domain model
- Running the audit chain into an external SIEM (Splunk, Sentinel)
- Replacing SQLite with Postgres for hospital-scale multi-tenant deployments
- Adding SNOMED CT concept lookups to the safety rules engine

PRs that come with tests and a security note get reviewed within a week.

---

## Commercial use

The code is MIT. You can use it inside a hospital, sell it as a hosted service, wrap it in a product, embed it in a larger system — whatever your business model needs. The maintainers offer paid support contracts (response SLA, security review, version pinning) for organizations that need them. Reach out via GitCode issues or the email in `pyproject.toml`.

What we will not accept:

- Selling individual PHI records or model training data derived from them.
- Closed-source forks that don't feed bug fixes back.
- Removing the audit chain or the deterministic safety rules from derivative works.

The last clause is the only one we enforce. Everything else is a community norm.

---

## Compliance status (current)

| Standard | Status | Notes |
|---|---|---|
| HIPAA Safe Harbor (de-identification) | Implemented | PHI de-identification covers the 18 identifier categories. |
| CDS Hooks 2.0 | Implemented | Endpoints exposed at `/cds-services` per spec. |
| FHIR R4 + SMART-on-FHIR | Implemented | Resource handlers in `doctoragent/api/fhir/`. |
| SNOMED CT / LOINC / ICD-10-CM | Implemented | Terminology bindings + lookup helpers. |
| 等保三级 | Roadmap | Q2 2026 target. Self-assessment checklist available on request. |
| HIPAA attestation (third-party) | Not started | Requires production customer with PHI to sponsor. |
| FDA 510(k) / NMPA / CE | Roadmap | Requires a clinical pilot with documented workflow. |
| IRB pre-approval (US) | Not applicable | We don't ship clinical workflows ready for human-subjects research. |

The four "Roadmap" rows are explicit because pretending they're already done wastes your planning cycle.

---

## Test posture

- **2314 unit tests** pass on Python 3.10, 3.11, 3.12, 3.13.
- **66 API routes** in the console have full round-trip tests.
- **18 CRUD workflows** (clinical workspace, vault, agents, hooks, etc.) are exercised end-to-end.
- **Empty-shell scan** runs as a CI step — if you submit a PR that adds an endpoint or a function which doesn't actually do anything, the test fails.
- **Dependency audit** is part of CI. We pin critical security deps and review transitive upgrades within 48 hours.

---

## Related projects

- [badhope/AI](https://gitcode.com/badhope/AI) — Engineering methodology, rule sets and prompt library used by DoctorAgent.
- [badhope](https://gitcode.com/badhope) — 16 other open-source projects.

---

## License

MIT. See `LICENSE`. The included SNOMED CT, LOINC, and ICD-10-CM reference data are redistributed under their respective licenses (see `docs/COMPLIANCE_ROADMAP.md`).

---

> **Clinical use disclaimer**: This system is a clinical decision support tool (CDS). It does not replace physician judgment. All AI-generated suggestions are advisory; final decisions rest with the licensed clinician of record.
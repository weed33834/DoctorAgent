# DoctorAgent

[English](README.md) | [中文](README.zh.md) | [日本語](README.ja.md)

[![Tests](https://img.shields.io/badge/tests-2314%20passed-brightgreen.svg)](https://github.com/weed33834/DoctorAgent)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)
[![FHIR](https://img.shields.io/badge/FHIR-R4-1d4ed8.svg)](https://hl7.org/fhir/R4/)

**Compliance-first, on-premise, auditable clinical AI agent platform.** A local-first framework for deploying clinical decision support and documentation agents inside an enterprise trust boundary — encrypted storage, tamper-evident audit log, RBAC + OIDC SSO, KMS, PHI de-identification, deterministic safety rules and LLM-output guardrails. PHI never leaves the host unless explicitly authorized. Detailed clinical capabilities: [docs/CLINICAL_CAPABILITIES.md](docs/CLINICAL_CAPABILITIES.md).

---

## What it does

DoctorAgent ships two cooperating surfaces on top of one hardened core:

1. **Clinical AI agent platform** (`doctoragent.clinical`) — a FHIR R4 adapter, a deterministic clinical rule engine (vitals / labs / drug-drug interaction / allergy cross-reactivity / duplicate therapy), LLM-output guardrails (citations / forbidden content / PHI leakage / prompt injection), 15 clinical tools, and a multi-agent workflow that fan-outs to specialist agents (patient history, drug safety, literature, documentation) and synthesises a guardrailed, citation-bearing, disclaimer-carrying result.
2. **Encrypted document vault** (the DoctorAgent core) — files dropped into a watched directory are classified, encrypted with AES-256-GCM, indexed (SQLite + FTS5), and archived on your own machine. Retrieval is natural-language search and a tool-calling agent.

Clinical workflow entry point:

```python
from doctoragent.clinical.agents import run_clinical_workflow
result = await run_clinical_workflow(
    patient_context={"patient_id": "synthetic-001", "medications": [...], ...},
    query="该患者用药是否安全？",
    llm_provider=your_local_ollama_provider,  # None = deterministic-only, safe default
)
```

Document retrieval:

```bash
doctoragent ask "where is last year's rental contract"
doctoragent agent "organize everything finance-related"
```

Cloud connections are disabled by default. No data leaves the machine unless an operator explicitly authorizes it.

---

## Quick start

```bash
# Install (requires Python 3.10+)
pip install doctoragent[gui,server]

# Start a local model (Ollama is the simplest path)
ollama pull qwen3:8b
ollama serve

# Run DoctorAgent and drop files into ~/DoctorAgent/Inbox
doctoragent daemon
```

The directory layout is created automatically on first run. Files placed in the Inbox are classified, encrypted, and archived.

---

## Features

**Clinical AI agent platform** (`doctoragent.clinical`)
- FHIR R4 adapter (HL7-official `fhir.resources`, SMART-on-FHIR bearer auth)
- CDS Hooks 2.0 services (patient-view / order-select / order-sign)
- Knowledge sources: openFDA, RxNorm, PubMed (no proprietary DB)
- Deterministic safety rules: vitals / labs / drug-drug interaction / allergy cross-reactivity / duplicate therapy
- LLM-output guardrails: citation / forbidden content / PHI leakage / prompt injection (strictest action wins)
- 15 clinical tools with five-level side-effect annotations (read / safe-write / destructive-write with human-in-loop)
- 4 specialist agents + `ClinicalOrchestrator` (fan-out / fan-in + deterministic safety + guardrail review)
- HIPAA Safe Harbor PHI de-identification pipeline (10 core clinical identifier categories)
- Compliance self-check report (exportable auditor evidence)
- 22 golden-case evaluation suite incl. adversarial (prompt injection, PII extraction, privilege escalation)
- Clinical QA benchmark framework (MedQA / PubMedQA): accuracy / macro-F1 / calibration (ECE+Brier) / safety / citation / latency, cross-family LLM-as-judge
- 6 synthetic FHIR R4 fixtures for offline demos

**File management**
- Real-time Inbox monitoring with automatic processing
- AES-256-GCM encryption with atomic writes
- Three-tier key hierarchy (master key → vault key → file key)
- SQLite + FTS5 full-text search
- Tamper-evident audit log

**Retrieval**
- RAG pipeline: retrieve, filter, rerank, generate, and cite sources
- Hybrid retrieval (keyword + semantic vectors) with configurable weighting
- Four-layer memory: short-term, working, episodic, long-term

**Agent**
- ReAct reasoning loop for multi-step tasks
- JSON Schema tool definitions, compatible with OpenAI/Anthropic function calling
- Plan-and-Execute, deep reflection, parallel tool execution, error recovery + circuit breaker
- Multi-agent orchestrator/worker pattern with checkpoint persistence
- MCP server (Model Context Protocol) for tool interoperability

**Security & compliance**
- Local-first; cloud connections off by default
- Linux bubblewrap / Windows AppContainer sandboxing
- Master key rotation (scheduled + emergency)
- Multi-tenant isolation and compliance audit
- RBAC permission matrix + OIDC SSO (Authlib + Casbin)
- Cloud KMS abstraction (AWS / Azure / GCP)
- PHI de-identification (Safe Harbor 10 core clinical categories) for clinical workflows

**Interfaces**
- PyQt6 desktop GUI (system tray + vault browser)
- REST API (FastAPI)
- Command-line tool

---

## Installation

```bash
# Base
pip install doctoragent

# Clinical AI agent platform (FHIR R4 + openFDA/RxNorm/PubMed + rule engine + guardrails)
pip install doctoragent[clinical]

# Desktop GUI
pip install doctoragent[gui]

# Semantic search
pip install doctoragent[semantic]

# REST API
pip install doctoragent[server]

# Everything
pip install doctoragent[gui,semantic,sync,server,multimodal,clinical]
```

Docker:

```bash
docker build -t doctoragent .
docker run --rm -it \
  --user $(id -u):$(id -g) \
  -v /path/to/inbox:/inbox \
  -v /path/to/vault:/vault \
  doctoragent daemon --no-tray
```

---

## Configuration

Environment variables take precedence over the config file:

| Variable | Description |
|----------|-------------|
| `DOCTORAGENT_PATHS__INBOX` | Inbox directory |
| `DOCTORAGENT_PATHS__VAULT` | Vault directory |
| `DOCTORAGENT_SECURITY__MASTER_KEY_PROVIDER` | Master key provider (`filepassword`, `dpapi`, `tpm`, `mac-keychain`) |
| `DOCTORAGENT_MODEL__BASE_URL` | Model endpoint |
| `DOCTORAGENT_MODEL__MODEL_NAME` | Model name |

The configuration file lives at `~/DoctorAgent/Config/settings.json`. Secrets (master key password, webhook shared secrets, S3/WebDAV credentials) are never written to disk and must be supplied through environment variables.

---

## Command line

| Command | Purpose |
|---------|---------|
| `doctoragent daemon` | Start the agent and monitor the Inbox |
| `doctoragent ask` | RAG question answering |
| `doctoragent agent` | Tool-calling agent |
| `doctoragent search` | Search files |
| `doctoragent status` | Show status |
| `doctoragent list` | List files |
| `doctoragent export` | Export (decrypt) files |
| `doctoragent import` | Batch import |
| `doctoragent pipe` | Ingest stdin |
| `doctoragent run` | Execute a JSON orchestration script |
| `doctoragent serve` | Start the API server |
| `doctoragent backup` | Remote backup |
| `doctoragent webhook-test` | Fire a test webhook |

```bash
# Search
doctoragent search "invoice 2024"
doctoragent search "rent contract" --semantic --top-k 10

# Question answering
doctoragent ask "summarise last quarter's invoices"

# Agent
doctoragent agent "Analyze all my contracts and identify key dates" --verbose
```

---

## API

`doctoragent serve` starts the REST API (default `127.0.0.1:8000`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/metrics` | Prometheus metrics |
| GET | `/vault/status` | File statistics |
| GET | `/vault/files` | File listing |
| POST | `/vault/search` | Search (keyword / semantic) |
| POST | `/vault/ask` | RAG question answering |
| POST | `/vault/ask/stream` | Streaming RAG (SSE) |
| POST | `/vault/agent` | Agent task |
| POST | `/vault/agent/stream` | Streaming agent (SSE) |
| POST | `/clinical/analyze` | Clinical workflow (rules + specialists + guardrails) |
| GET | `/cds-services` | CDS Hooks 2.0 service discovery |
| POST | `/cds-services/{id}` | CDS Hooks invocation (patient-view / order-select / order-sign) |
| GET | `/events` | Real-time audit event stream (SSE) |
| WS | `/ws` | WebSocket (agent + event push) |
| POST | `/mcp` | MCP tool server endpoint |

Set `DOCTORAGENT_API_TOKEN` for static bearer auth, or `DOCTORAGENT_OIDC_ISSUER` for OIDC SSO. Sensitive endpoints require a trusted local connection (127.0.0.1) when no token is configured.

---

## Security model

- **Local-first**: data stays on the host by default
- **Three-tier keys**: master key (Argon2id / DPAPI / TPM / Keychain) → vault key (HKDF-SHA256) → file key (HKDF-SHA256, per-file salt)
- **Encrypted storage**: AES-256-GCM with atomic writes
- **Audit log**: append-only NDJSON, HMAC-SHA256 per entry, tamper-detectable offline
- **Network isolation**: sensitive operations require a trusted local connection (127.0.0.1); cloud fallback is opt-in and authorized per connection
- **Sandboxing**: Linux bubblewrap / Windows AppContainer
- **Key rotation**: scheduled (90-day default) and emergency rotation, with all-or-nothing re-encryption and rollback

---

## Development

```bash
git clone https://github.com/weed33834/DoctorAgent.git
cd DoctorAgent
pip install -e ".[gui,server,semantic,multimodal,dev]"

# Tests
python -m pytest tests/ -v

# Lint
ruff check doctoragent/
ruff format doctoragent/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## Repository & Mirrors

This repository is primarily hosted on **GitHub** and mirrored to GitCode and Gitee for accessibility. GitHub is the canonical source; mirrors are synchronized manually.

| Platform | URL |
|----------|-----|
| **GitHub** (primary / canonical) | https://github.com/weed33834/DoctorAgent |
| GitCode (mirror) | https://gitcode.com/badhope/DoctorAgent |
| Gitee (mirror) | https://gitee.com/badhope/DoctorAgent |

## License

[MIT License](LICENSE)

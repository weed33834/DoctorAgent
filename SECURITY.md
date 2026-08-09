# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.3.x   | :white_check_mark: |
| < 0.3   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in DoctorAgent, please report it responsibly:

1. **Do NOT open a public GitHub issue.**
2. Use GitHub's **Security Advisories** feature: go to the [Security tab](https://github.com/weed33834/DoctorAgent/security/advisories/new) and create a private security advisory.
3. Alternatively, email security concerns to the address listed in the GitHub profile.
4. Include a description of the vulnerability, steps to reproduce, and potential impact.
5. You will receive an acknowledgment within 48 hours.

## Security Features

DoctorAgent is designed with a security-first approach:

- **Encryption at rest**: AES-256-GCM with Argon2id/PBKDF2-SHA256 key derivation
- **Tamper-evident audit log**: HMAC-SHA256 chained entries
- **PHI de-identification**: 19 HIPAA Safe Harbor identifier categories detected and masked
- **Deterministic safety rules**: Drug interactions, critical values, allergy cross-reactivity — all run offline
- **RBAC + API token + tenant isolation**: Multi-tenant access control
- **Air-gapped capable**: Full operation without outbound network traffic

## Security Scanning

- **bandit**: Runs in CI (`.github/workflows/security.yml`), non-blocking
- **pip-audit**: Runs in CI, blocking — vulnerable dependencies fail the build
- **Empty-shell scan**: CI step that fails if an endpoint or function doesn't actually do anything

## Disclaimer

DoctorAgent is a clinical decision support tool (CDS). It does not replace physician judgment. All AI-generated suggestions are advisory; final decisions rest with the licensed clinician of record.

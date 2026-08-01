"""Tests for the HIPAA compliance self-assessment report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doctoragent.clinical.compliance_report import ComplianceReport
from doctoragent.config import AegisConfig
from doctoragent.security.audit_log import AuditLogger

_REQUIRED_KEYS = {
    "encryption",
    "audit_logging",
    "access_control",
    "phi_protection",
    "key_management",
    "data_residency",
    "compliance_gaps",
    "overall_status",
}


@pytest.fixture
def isolated_config(tmp_path: Path) -> AegisConfig:
    """Config whose audit-log path does not exist (no real logs read)."""
    cfg = AegisConfig()
    cfg.paths.logs = tmp_path / "logs"
    return cfg


def test_generate_returns_all_keys(isolated_config: AegisConfig) -> None:
    report = ComplianceReport(isolated_config)
    generated = report.generate()
    assert _REQUIRED_KEYS <= set(generated)
    assert generated["overall_status"] in {"compliant", "partial", "non_compliant"}
    # Section sub-structure is present.
    assert generated["encryption"]["algorithm"] == "AES-256-GCM"
    assert generated["encryption"]["kdf"] == "Argon2id"
    assert isinstance(generated["compliance_gaps"], list)


def test_to_text_is_human_readable(isolated_config: AegisConfig) -> None:
    text = ComplianceReport(isolated_config).to_text()
    assert "HIPAA Compliance" in text
    assert "Overall status:" in text
    assert "[Encryption]" in text
    assert "[Compliance gaps]" in text


def test_to_json_is_serializable(isolated_config: AegisConfig) -> None:
    payload = ComplianceReport(isolated_config).to_json()
    parsed = json.loads(payload)
    assert _REQUIRED_KEYS <= set(parsed)


def test_prod_without_token_has_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prod with no API token and no master-key password must surface gaps.
    monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
    monkeypatch.delenv("DOCTORAGENT_OIDC_ISSUER", raising=False)
    config = AegisConfig(env="prod")
    report = ComplianceReport(config, audit_log_path=Path("/nonexistent/audit.log"))
    generated = report.generate()
    assert generated["compliance_gaps"], "prod without token should have gaps"
    assert generated["overall_status"] == "non_compliant"
    # The access-control gap must be mentioned.
    assert any("API authentication" in g for g in generated["compliance_gaps"])


def test_dev_overall_status_not_non_compliant(
    monkeypatch: pytest.MonkeyPatch,
    isolated_config: AegisConfig,
) -> None:
    monkeypatch.delenv("DOCTORAGENT_API_TOKEN", raising=False)
    monkeypatch.delenv("DOCTORAGENT_OIDC_ISSUER", raising=False)
    generated = ComplianceReport(isolated_config).generate()
    assert generated["overall_status"] != "non_compliant"
    assert generated["overall_status"] in {"compliant", "partial"}


def test_audit_log_read_counts_entries_and_verifies_hmac(
    tmp_path: Path,
) -> None:
    config = AegisConfig()
    config.paths.logs = tmp_path / "logs"
    # AuditLogger with no explicit key creates the .audit.key file, which
    # ComplianceReport reads back to verify HMAC integrity.
    logger = AuditLogger(config)
    logger.log("file_ingested", {"task_id": "1"})
    logger.log("encrypted", {"task_id": "1"})

    generated = ComplianceReport(config).generate()
    au = generated["audit_logging"]
    assert au["enabled"] is True
    assert au["entry_count"] == 2
    assert au["hmac_integrity"] == "verified"
    assert len(au["recent_events"]) == 2
    assert au["recent_events"][0]["event_type"] == "file_ingested"


def test_audit_log_tampered_marked_non_compliant(tmp_path: Path) -> None:
    config = AegisConfig()
    config.paths.logs = tmp_path / "logs"
    logger = AuditLogger(config, hmac_key=b"k" * 32)
    logger.log("file_ingested", {"task_id": "1"})
    # Tamper: rewrite the log line with a bogus HMAC but keep a valid key
    # file by writing one derived from the real key path.
    log_path = config.paths.logs / "audit.log.ndjson"
    key_path = config.paths.logs / ".audit.key"
    key_path.write_bytes(b"k" * 32)
    records = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    records[0]["hmac"] = "deadbeef" * 8
    log_path.write_text(json.dumps(records[0]) + "\n")

    generated = ComplianceReport(config).generate()
    assert generated["audit_logging"]["hmac_integrity"] == "tampered"
    assert generated["overall_status"] == "non_compliant"

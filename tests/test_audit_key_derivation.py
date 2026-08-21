"""Tests: audit HMAC key derivation from the master key.

v0.3.8: ``AuditLogger`` previously stored its HMAC key as a plaintext
``.audit.key`` file *in the same directory as the audit log*, so anyone with
disk access could rewrite history and re-sign a valid-looking chain. The key
is now derivable from the master key via HKDF-SHA256
(:func:`doctoragent.security.keytree.derive_audit_key`), and the logger's
resolution order is: explicit ``hmac_key`` → HKDF(master_key) → legacy file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from doctoragent.config import AegisConfig
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.keytree import derive_audit_key


@pytest.fixture
def config(tmp_path: Path) -> AegisConfig:
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


class TestDeriveAuditKey:
    """HKDF derivation properties."""

    def test_deterministic(self) -> None:
        mk = os.urandom(32)
        assert derive_audit_key(mk) == derive_audit_key(mk)

    def test_differs_from_master_key(self) -> None:
        mk = os.urandom(32)
        derived = derive_audit_key(mk)
        assert derived != mk
        assert len(derived) == 32

    def test_unique_per_master_key(self) -> None:
        assert derive_audit_key(os.urandom(32)) != derive_audit_key(os.urandom(32))

    def test_differs_from_vault_key(self) -> None:
        """Key separation: the audit key never equals the vault key."""
        from doctoragent.security.keytree import derive_vault_key

        mk = os.urandom(32)
        assert derive_audit_key(mk) != derive_vault_key(mk)


class TestAuditLoggerKeyResolution:
    """Resolution order: hmac_key > master_key > legacy .audit.key."""

    def test_master_key_used_and_no_keyfile_created(self, config: AegisConfig) -> None:
        mk = os.urandom(32)
        logger_ = AuditLogger(config, master_key=mk)
        assert logger_.hmac_key == derive_audit_key(mk)
        assert not (config.paths.logs / ".audit.key").exists()

    def test_explicit_hmac_key_wins_over_master_key(self, config: AegisConfig) -> None:
        raw = os.urandom(32)
        logger_ = AuditLogger(config, hmac_key=raw, master_key=os.urandom(32))
        assert logger_.hmac_key == raw

    def test_legacy_file_fallback_without_master_key(self, config: AegisConfig) -> None:
        logger_ = AuditLogger(config)
        keyfile = config.paths.logs / ".audit.key"
        assert keyfile.exists()
        # A second logger reads the same key (chain continuity).
        assert AuditLogger(config).hmac_key == logger_.hmac_key

    def test_chain_verifies_with_derived_key(self, config: AegisConfig) -> None:
        mk = os.urandom(32)
        logger_ = AuditLogger(config, master_key=mk)
        logger_.log("file_ingested", {"n": 1})
        logger_.log("file_ingested", {"n": 2})
        ok, mismatches = logger_.verify()
        assert ok is True
        assert mismatches == []

    def test_tampering_detected_with_derived_key(self, config: AegisConfig) -> None:
        mk = os.urandom(32)
        logger_ = AuditLogger(config, master_key=mk)
        logger_.log("file_ingested", {"n": 1})
        log_file = config.paths.logs / "audit.log.ndjson"
        tampered = log_file.read_text(encoding="utf-8").replace(
            '"file_ingested"', '"classified"'
        )
        log_file.write_text(tampered, encoding="utf-8")
        ok, _mismatches = logger_.verify()
        assert ok is False

    def test_same_master_key_reopens_chain(self, config: AegisConfig) -> None:
        """Restart with the same master key keeps verifying old entries."""
        mk = os.urandom(32)
        first = AuditLogger(config, master_key=mk)
        first.log("file_ingested", {"n": 1})
        second = AuditLogger(config, master_key=mk)
        second.log("file_ingested", {"n": 2})
        ok, mismatches = second.verify()
        assert ok is True
        assert mismatches == []

"""Tests for master key rotation functions."""

import sys
from datetime import datetime, timedelta
from doctoragent.compat import UTC
from pathlib import Path

import pytest

from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.crypto import (
    SALT_LEN as CRYPTO_SALT_LEN,
)
from doctoragent.security.crypto import (
    VERSION as CRYPTO_VERSION,
)
from doctoragent.security.keytree import derive_file_key, generate_salt
from doctoragent.security.master_key import (
    FilePasswordProvider,
    _decrypt_vault_key,
    _encrypt_vault_key,
    _re_encrypt_vault_files,
    emergency_rotate,
    rotate_master_key,
    should_rotate_key,
    unwrap_vault_key,
)

# ── should_rotate_key ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "age_days, max_age, expected",
    [
        (0, 90, False),
        (89, 90, False),
        (91, 90, True),
        (365, 90, True),
        (31, 30, True),
    ],
)
def test_should_rotate_key(age_days: int, max_age: int, expected: bool) -> None:
    """should_rotate_key returns True only when age exceeds max_age_days."""
    creation_time = datetime.now(UTC) - timedelta(days=age_days)
    assert should_rotate_key(creation_time, max_age) == expected


def test_should_rotate_key_default_max_age() -> None:
    """Default max age is 90 days."""
    recent = datetime.now(UTC) - timedelta(days=30)
    old = datetime.now(UTC) - timedelta(days=100)
    assert should_rotate_key(recent) is False
    assert should_rotate_key(old) is True


# ══════════════════════════════════════════════════════════════════════════


def test_encrypt_decrypt_vault_key_round_trip() -> None:
    """Wrapping and unwrapping a vault key with the same master key is lossless."""
    master_key = b"m" * 32
    vault_key = b"v" * 32
    wrapped = _encrypt_vault_key(vault_key, master_key)
    assert wrapped != vault_key
    recovered = _decrypt_vault_key(wrapped, master_key)
    assert recovered == vault_key


def test_encrypt_decrypt_vault_key_different_master_fails() -> None:
    """Wrong master key cannot unwrap the vault key."""
    vault_key = b"v" * 32
    wrapped = _encrypt_vault_key(vault_key, b"m" * 32)
    with pytest.raises(Exception):  # noqa: B017 — any crypto error from wrong key is acceptable
        _decrypt_vault_key(wrapped, b"bad" * 8 + b"00000000")


def test_wrap_then_unwrap_via_unwrap_vault_key(tmp_path: Path) -> None:
    """unwrap_vault_key reads from disk and recovers the original vault key."""
    master_key = b"m" * 32
    vault_key = b"v" * 32
    wrapped_path = tmp_path / "vault_key.wrapped"
    wrapped_path.write_bytes(_encrypt_vault_key(vault_key, master_key))

    recovered = unwrap_vault_key(wrapped_path, master_key)
    assert recovered == vault_key


# ── rotate_master_key ────────────────────────────────────────────────────


def _create_vault_file(vault_dir: Path, vault_key: bytes, filename: str, content: bytes) -> Path:
    """Create a properly formatted vault file and return its path."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)
    nonce = b"\x00" * 12  # deterministic for tests
    aad = CRYPTO_VERSION + salt
    aesgcm = AESGCM(file_key)
    ciphertext = aesgcm.encrypt(nonce, content, aad)

    category_dir = vault_dir / "documents"
    category_dir.mkdir(parents=True, exist_ok=True)
    vault_path = category_dir / filename
    vault_path.write_bytes(CRYPTO_VERSION + salt + nonce + ciphertext)
    return vault_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
def test_rotate_master_key_re_encrypts_files(tmp_path: Path) -> None:
    """Full rotation re-encrypts all vault files with the new vault key."""
    vault_dir = tmp_path / "Vault"
    storage_dir = tmp_path / "Config"
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Create providers with known passwords.
    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    old_master = old_provider.get_key()
    from doctoragent.security.master_key import _derive_vault_key_from_master

    old_vault_key = _derive_vault_key_from_master(old_master)

    # Create a vault file.
    original_content = b"Hello, DoctorAgent!"
    vault_file = _create_vault_file(vault_dir, old_vault_key, "testfile.dat", original_content)

    storage_path = storage_dir / "master_key.bin"

    result = rotate_master_key(
        current_provider=old_provider,
        new_provider=new_provider,
        vault_key=old_vault_key,
        storage_path=storage_path,
    )

    assert result is new_provider

    # Verify the file was re-encrypted (can decrypt with new key).
    new_master = new_provider.get_key()
    new_vault_key = _derive_vault_key_from_master(new_master)
    # New vault key must be different.
    assert new_vault_key != old_vault_key

    # Decrypt with new vault key.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    data = vault_file.read_bytes()
    version = data[:1]
    salt = data[1 : 1 + CRYPTO_SALT_LEN]
    nonce = data[1 + CRYPTO_SALT_LEN : 1 + CRYPTO_SALT_LEN + 12]
    ciphertext = data[1 + CRYPTO_SALT_LEN + 12 :]
    new_file_key = derive_file_key(new_vault_key, salt)
    aesgcm = AESGCM(new_file_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, version + salt)
    assert plaintext == original_content


def test_rotate_master_key_validation_fails_on_wrong_vault_key(tmp_path: Path) -> None:
    """Rotation rejects when the provided vault_key doesn't match the derived one."""
    storage_dir = tmp_path / "Config"
    storage_dir.mkdir(parents=True, exist_ok=True)

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    wrong_vault_key = b"x" * 32

    with pytest.raises(ValueError, match="does not match"):
        rotate_master_key(
            current_provider=old_provider,
            new_provider=new_provider,
            vault_key=wrong_vault_key,
            storage_path=storage_dir / "master_key.bin",
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
def test_rotate_master_key_audit_logging(tmp_path: Path) -> None:
    """Rotation produces audit log entries."""
    from doctoragent.config import AegisConfig

    vault_dir = tmp_path / "Vault"
    storage_dir = tmp_path / "Config"
    log_dir = tmp_path / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)

    config = AegisConfig()
    config.paths.logs = log_dir

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    old_master = old_provider.get_key()
    from doctoragent.security.master_key import _derive_vault_key_from_master

    old_vault_key = _derive_vault_key_from_master(old_master)

    # Create a vault file.
    _create_vault_file(vault_dir, old_vault_key, "testfile.dat", b"content")

    audit_logger = AuditLogger(config, hmac_key=b"a" * 32)

    rotate_master_key(
        current_provider=old_provider,
        new_provider=new_provider,
        vault_key=old_vault_key,
        storage_path=storage_dir / "master_key.bin",
        audit_logger=audit_logger,
    )

    # Query for rotation events.
    results = audit_logger.query(event_type="master_key_changed")
    assert len(results) >= 1
    rotation_event = results[0]
    assert rotation_event["details"]["operation"] == "rotation"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
def test_rotate_master_key_empty_vault(tmp_path: Path) -> None:
    """Rotation succeeds even when the vault directory doesn't exist."""
    storage_dir = tmp_path / "Config"
    storage_dir.mkdir(parents=True, exist_ok=True)

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    old_master = old_provider.get_key()
    from doctoragent.security.master_key import _derive_vault_key_from_master

    old_vault_key = _derive_vault_key_from_master(old_master)

    result = rotate_master_key(
        current_provider=old_provider,
        new_provider=new_provider,
        vault_key=old_vault_key,
        storage_path=storage_dir / "master_key.bin",
    )

    assert result is new_provider


# ── emergency_rotate ─────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
def test_emergency_rotate_wraps_vault_key(tmp_path: Path) -> None:
    """Emergency rotation wraps the existing vault key with the new master key."""
    storage_dir = tmp_path / "Config"
    storage_dir.mkdir(parents=True, exist_ok=True)
    backup_path = tmp_path / "vault_key.enc"

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    old_master = old_provider.get_key()
    from doctoragent.security.master_key import _derive_vault_key_from_master

    vault_key = _derive_vault_key_from_master(old_master)

    new_master = emergency_rotate(
        current_provider=old_provider,
        new_provider=new_provider,
        vault_key=vault_key,
        vault_key_backup_path=backup_path,
    )

    # The returned key is the new master key.
    assert len(new_master) == 32
    assert new_master != old_master

    # The backup file exists and contains a wrapped vault key.
    assert backup_path.exists()
    wrapped = backup_path.read_bytes()
    recovered = _decrypt_vault_key(wrapped, new_master)
    assert recovered == vault_key


def test_emergency_rotate_validation_fails_on_wrong_vault_key(tmp_path: Path) -> None:
    """Emergency rotation rejects when vault_key doesn't match old provider."""
    storage_dir = tmp_path / "Config"
    storage_dir.mkdir(parents=True, exist_ok=True)
    backup_path = tmp_path / "vault_key.enc"

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    with pytest.raises(ValueError, match="does not match"):
        emergency_rotate(
            current_provider=old_provider,
            new_provider=new_provider,
            vault_key=b"wrong" * 8,
            vault_key_backup_path=backup_path,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
def test_emergency_rotate_audit_logging(tmp_path: Path) -> None:
    """Emergency rotation produces a forced audit log entry."""
    from doctoragent.config import AegisConfig

    storage_dir = tmp_path / "Config"
    log_dir = tmp_path / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)
    backup_path = tmp_path / "vault_key.enc"

    config = AegisConfig()
    config.paths.logs = log_dir

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    old_master = old_provider.get_key()
    from doctoragent.security.master_key import _derive_vault_key_from_master

    vault_key = _derive_vault_key_from_master(old_master)

    audit_logger = AuditLogger(config, hmac_key=b"b" * 32)

    emergency_rotate(
        current_provider=old_provider,
        new_provider=new_provider,
        vault_key=vault_key,
        vault_key_backup_path=backup_path,
        audit_logger=audit_logger,
    )

    results = audit_logger.query(event_type="master_key_changed")
    assert len(results) >= 1
    event = results[0]
    assert event["details"]["operation"] == "emergency_rotation"
    assert event["details"]["reason"] == "key_compromise"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
def test_emergency_rotate_does_not_re_encrypt_files(tmp_path: Path) -> None:
    """Emergency rotation does NOT re-encrypt vault files (fast path)."""
    vault_dir = tmp_path / "Vault"
    storage_dir = tmp_path / "Config"
    storage_dir.mkdir(parents=True, exist_ok=True)
    backup_path = tmp_path / "vault_key.enc"

    old_provider = FilePasswordProvider(password="old-secret", storage_path=storage_dir)
    new_provider = FilePasswordProvider(password="new-secret", storage_path=storage_dir)

    old_master = old_provider.get_key()
    from doctoragent.security.master_key import _derive_vault_key_from_master

    old_vault_key = _derive_vault_key_from_master(old_master)

    # Create a vault file (should NOT be touched).
    original = b"emergency rotation test"
    vault_file = _create_vault_file(vault_dir, old_vault_key, "emergency.dat", original)
    file_mtime_before = vault_file.stat().st_mtime

    emergency_rotate(
        current_provider=old_provider,
        new_provider=new_provider,
        vault_key=old_vault_key,
        vault_key_backup_path=backup_path,
    )

    # File should be untouched.
    assert vault_file.stat().st_mtime == file_mtime_before


# ── _re_encrypt_vault_files ──────────────────────────────────────────────


def test_re_encrypt_vault_files_empty_dir(tmp_path: Path) -> None:
    """_re_encrypt_vault_files returns 0 for an empty directory."""
    vault_dir = tmp_path / "Vault"
    vault_dir.mkdir()
    count = _re_encrypt_vault_files(vault_dir, b"a" * 32, b"b" * 32)
    assert count == 0


def test_re_encrypt_vault_files_missing_dir(tmp_path: Path) -> None:
    """_re_encrypt_vault_files returns 0 for a missing directory."""
    count = _re_encrypt_vault_files(tmp_path / "nonexistent", b"a" * 32, b"b" * 32)
    assert count == 0


def test_re_encrypt_vault_files_multiple_files(tmp_path: Path) -> None:
    """_re_encrypt_vault_files correctly re-encrypts multiple files."""
    vault_dir = tmp_path / "Vault"
    old_vault_key = b"a" * 32
    new_vault_key = b"b" * 32

    content1 = b"first file content"
    content2 = b"second file content"
    f1 = _create_vault_file(vault_dir, old_vault_key, "f1.dat", content1)
    f2 = _create_vault_file(vault_dir, old_vault_key, "f2.bin", content2)

    count = _re_encrypt_vault_files(vault_dir, old_vault_key, new_vault_key)
    assert count == 2

    # Verify both files can be decrypted with new key.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    for vf, expected in [(f1, content1), (f2, content2)]:
        data = vf.read_bytes()
        salt = data[1 : 1 + 32]
        nonce = data[1 + 32 : 1 + 32 + 12]
        ct = data[1 + 32 + 12 :]
        new_fk = derive_file_key(new_vault_key, salt)
        aesgcm = AESGCM(new_fk)
        assert aesgcm.decrypt(nonce, ct, data[:1] + salt) == expected

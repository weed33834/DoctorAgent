"""Tests for VaultManager encryption and decryption operations."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.config import AegisConfig, PathConfig
from doctoragent.execution.vault import VaultManager
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.keytree import derive_vault_key


@pytest.fixture
def vault_key() -> bytes:
    """Derive a real vault key from a deterministic master key."""
    master = b"test-master-key-for-vault-tests!"
    return derive_vault_key(master)


@pytest.fixture
def classification() -> ClassificationResult:
    """A standard classification result for testing."""
    return ClassificationResult(
        sensitivity=SensitivityLevel.HIGH,
        category="finance",
        tags=["invoice"],
        summary="Test document",
        disguise_name="report",
        disguise_extension="dat",
    )


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """Create a source file with known content."""
    src = tmp_path / "secret.txt"
    src.write_bytes(b"Top-secret vault content for testing.")
    return src


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Return a temporary vault directory."""
    return tmp_path / "vault"


@pytest.fixture
def manager(vault_dir: Path, vault_key: bytes) -> VaultManager:
    """Create a VaultManager with no audit logger."""
    return VaultManager(vault_path=vault_dir, vault_key=vault_key)


# ---- Test 1: encrypt creates category directory and encrypted file ----


def test_encrypt_creates_directory_and_file(
    manager: VaultManager,
    source_file: Path,
    classification: ClassificationResult,
    vault_dir: Path,
) -> None:
    """Encrypting a file should create the category directory and the vault file."""
    result = manager.encrypt(source_file, classification, uuid4())

    category_dir = vault_dir / "finance"
    assert category_dir.is_dir()
    assert result.vault_path.exists()
    assert result.vault_path.parent == category_dir


# ---- Test 2: encrypt returns correct EncryptResult fields ----


def test_encrypt_returns_correct_result(
    manager: VaultManager,
    source_file: Path,
    classification: ClassificationResult,
) -> None:
    """EncryptResult should contain vault_path, salt, nonce, and task_id."""
    task_id = uuid4()
    result = manager.encrypt(source_file, classification, task_id)

    assert result.task_id == task_id
    assert isinstance(result.vault_path, Path)
    assert result.vault_path.name == "report.dat"
    assert isinstance(result.salt, bytes)
    assert len(result.salt) == 32
    assert isinstance(result.nonce, bytes)
    assert len(result.nonce) == 12


# ---- Test 3: encrypt/decrypt roundtrip preserves content ----


def test_encrypt_decrypt_roundtrip(
    manager: VaultManager,
    source_file: Path,
    classification: ClassificationResult,
    tmp_path: Path,
) -> None:
    """Encrypting then decrypting should recover the original content exactly."""
    result = manager.encrypt(source_file, classification, uuid4())

    destination = tmp_path / "decrypted.txt"
    manager.decrypt(result.vault_path, result.salt, destination)

    assert destination.read_bytes() == source_file.read_bytes()


# ---- Test 4: encrypt handles filename collisions ----


def test_encrypt_handles_filename_collision(
    manager: VaultManager,
    source_file: Path,
    classification: ClassificationResult,
    vault_dir: Path,
) -> None:
    """When a vault file already exists, encrypt should create a suffixed filename."""
    result1 = manager.encrypt(source_file, classification, uuid4())
    result2 = manager.encrypt(source_file, classification, uuid4())

    assert result1.vault_path != result2.vault_path
    assert result1.vault_path.exists()
    assert result2.vault_path.exists()
    # Both should live in the same category directory
    assert result1.vault_path.parent == result2.vault_path.parent
    # The second file should have a random suffix before the extension
    assert result2.vault_path.name.startswith("report_")
    assert result2.vault_path.suffix == ".dat"


# ---- Test 5: _sanitize_path_component rejects path traversal ----


@pytest.mark.parametrize(
    "value",
    ["..", ".", "", "/"],
)
def test_sanitize_rejects_path_traversal(value: str) -> None:
    """Path traversal sequences must be rejected."""
    with pytest.raises(ValueError, match="path traversal"):
        VaultManager._sanitize_path_component(value, "test_field")


# ---- Test 6: _sanitize_path_component rejects backslashes and null bytes ----


@pytest.mark.skipif(sys.platform == "win32", reason="Backslashes are path separators on Windows")
@pytest.mark.parametrize(
    "value",
    ["evil\\name", "null\x00byte", "back\\slash\x00mixed"],
)
def test_sanitize_rejects_backslashes_and_null_bytes(value: str) -> None:
    """Backslashes and null bytes must be rejected."""
    with pytest.raises(ValueError, match="forbidden characters"):
        VaultManager._sanitize_path_component(value, "test_field")


# ---- Test 7: decrypt with audit_logger logs the event ----


def test_decrypt_with_audit_logger_logs_event(
    vault_dir: Path,
    vault_key: bytes,
    source_file: Path,
    classification: ClassificationResult,
    tmp_path: Path,
) -> None:
    """When an audit_logger is configured, decrypt should log a 'decrypted' event."""
    log_dir = tmp_path / "logs"
    config = AegisConfig(paths=PathConfig(logs=log_dir))
    audit_logger = AuditLogger(config, hmac_key=os.urandom(32))
    mgr = VaultManager(vault_path=vault_dir, vault_key=vault_key, audit_logger=audit_logger)

    result = mgr.encrypt(source_file, classification, uuid4())

    destination = tmp_path / "decrypted.txt"
    mgr.decrypt(result.vault_path, result.salt, destination)

    records = audit_logger.query(event_type="decrypted")
    assert len(records) == 1
    assert records[0]["event_type"] == "decrypted"
    assert records[0]["details"]["vault_path"] == str(result.vault_path)
    assert records[0]["details"]["destination"] == str(destination)


# ---- Test 8: decrypt without audit_logger works fine ----


def test_decrypt_without_audit_logger(
    manager: VaultManager,
    source_file: Path,
    classification: ClassificationResult,
    tmp_path: Path,
) -> None:
    """Decrypt should work correctly when no audit_logger is configured."""
    assert manager.audit_logger is None

    result = manager.encrypt(source_file, classification, uuid4())
    destination = tmp_path / "decrypted.txt"
    manager.decrypt(result.vault_path, result.salt, destination)

    assert destination.read_bytes() == source_file.read_bytes()


# ---- Test 9: encrypt with invalid category raises ValueError ----


@pytest.mark.parametrize(
    "bad_category",
    ["..", ".", ""],
)
def test_encrypt_invalid_category_raises(
    manager: VaultManager,
    source_file: Path,
    bad_category: str,
) -> None:
    """Encryption with a path-traversal category must raise ValueError."""
    classification = ClassificationResult(
        sensitivity=SensitivityLevel.LOW,
        category=bad_category,
        disguise_name="safe",
        disguise_extension="dat",
    )
    with pytest.raises(ValueError, match="path traversal"):
        manager.encrypt(source_file, classification, uuid4())


# ---- Test 10: encrypt with disguise_name containing "/" raises ValueError ----


def test_encrypt_disguise_name_with_slash_raises(
    manager: VaultManager,
    source_file: Path,
) -> None:
    """A disguise_name containing '/' resolves to just the basename via Path.name,
    but if that basename is empty or '.', it must raise ValueError."""
    # Path("foo/bar").name == "bar" which is valid, so we test with a value
    # whose Path.name resolves to something dangerous.
    # A disguise_name of just "/" -> Path("/").name == "" -> ValueError
    classification = ClassificationResult(
        sensitivity=SensitivityLevel.LOW,
        category="docs",
        disguise_name="/",
        disguise_extension="dat",
    )
    with pytest.raises(ValueError):
        manager.encrypt(source_file, classification, uuid4())


# ---- Test 11: _sanitize_path_component accepts valid names ----


@pytest.mark.parametrize(
    "value,expected",
    [
        ("normal", "normal"),
        ("with-dash", "with-dash"),
        ("with_underscore", "with_underscore"),
        ("mixed.Case.123", "mixed.Case.123"),
    ],
)
def test_sanitize_accepts_valid_names(value: str, expected: str) -> None:
    """Valid path components should pass through unchanged."""
    assert VaultManager._sanitize_path_component(value, "test_field") == expected

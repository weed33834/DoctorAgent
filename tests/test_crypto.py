"""Tests for encryption layer."""

import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from doctoragent.security.crypto import decrypt_file_stream, encrypt_file_stream
from doctoragent.security.keytree import derive_file_key, derive_vault_key, generate_salt


@pytest.fixture
def vault_key() -> bytes:
    """Fixture for a deterministic test vault key."""
    master = b"0" * 32
    return derive_vault_key(master)


def test_encrypt_decrypt_roundtrip(tmp_path: Path, vault_key: bytes) -> None:
    """Encrypt and decrypt a file, ensuring roundtrip correctness."""
    source = tmp_path / "secret.txt"
    destination = tmp_path / "secret.txt.vault"
    decrypted = tmp_path / "secret_decrypted.txt"
    secret = b"This is a secret message for DoctorAgent."
    source.write_bytes(secret)

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)
    encrypt_file_stream(source, destination, file_key, salt)

    assert destination.exists()
    assert destination.stat().st_size > 32 + 12 + 16

    decrypt_file_stream(destination, decrypted, file_key)
    assert decrypted.read_bytes() == secret


def test_tampered_vault_fails(tmp_path: Path, vault_key: bytes) -> None:
    """Tampered ciphertext must fail GCM authentication."""
    source = tmp_path / "secret.txt"
    destination = tmp_path / "secret.txt.vault"
    decrypted = tmp_path / "secret_decrypted.txt"
    source.write_bytes(b"tamper test")

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)
    encrypt_file_stream(source, destination, file_key, salt)

    data = destination.read_bytes()
    data = data[:-1] + bytes([data[-1] ^ 0xFF])
    destination.write_bytes(data)

    with pytest.raises((InvalidTag, ValueError)):
        decrypt_file_stream(destination, decrypted, file_key)


def test_empty_file_roundtrip(tmp_path: Path, vault_key: bytes) -> None:
    """Encrypt and decrypt an empty file successfully."""
    source = tmp_path / "empty.txt"
    destination = tmp_path / "empty.txt.vault"
    decrypted = tmp_path / "empty_decrypted.txt"
    source.write_bytes(b"")

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)
    encrypt_file_stream(source, destination, file_key, salt)

    # File should contain at least version + salt + nonce + tag (no ciphertext)
    assert destination.exists()
    assert destination.stat().st_size >= 1 + 32 + 12 + 16

    decrypt_file_stream(destination, decrypted, file_key)
    assert decrypted.read_bytes() == b""


def test_wrong_key_fails(tmp_path: Path, vault_key: bytes) -> None:
    """Decrypting with a different key must fail GCM authentication."""
    source = tmp_path / "secret.txt"
    destination = tmp_path / "secret.txt.vault"
    decrypted = tmp_path / "secret_decrypted.txt"
    source.write_bytes(b"wrong key test")

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)
    encrypt_file_stream(source, destination, file_key, salt)

    # Derive a different key from a different master
    other_master = b"X" * 32
    other_vault_key = derive_vault_key(other_master)
    wrong_key = derive_file_key(other_vault_key, salt)

    with pytest.raises((InvalidTag, ValueError)):
        decrypt_file_stream(destination, decrypted, wrong_key)


def test_truncated_salt_fails(tmp_path: Path, vault_key: bytes) -> None:
    """A vault file with a truncated salt must raise ValueError."""
    from doctoragent.security.crypto import VERSION

    vault_file = tmp_path / "truncated_salt.vault"
    # Write version + only 10 bytes of salt (need 32)
    vault_file.write_bytes(VERSION + b"\x00" * 10)

    decrypted = tmp_path / "decrypted.txt"
    with pytest.raises(ValueError, match="short salt"):
        decrypt_file_stream(vault_file, decrypted, vault_key)


def test_truncated_nonce_fails(tmp_path: Path, vault_key: bytes) -> None:
    """A vault file with a truncated nonce must raise ValueError."""
    from doctoragent.security.crypto import SALT_LEN, VERSION

    vault_file = tmp_path / "truncated_nonce.vault"
    # Write version + full salt + only 5 bytes of nonce (need 12)
    vault_file.write_bytes(VERSION + b"\x00" * SALT_LEN + b"\x00" * 5)

    decrypted = tmp_path / "decrypted.txt"
    with pytest.raises(ValueError, match="short nonce"):
        decrypt_file_stream(vault_file, decrypted, vault_key)


def test_unsupported_version_fails(tmp_path: Path, vault_key: bytes) -> None:
    """A vault file with an unsupported version byte must raise ValueError."""
    vault_file = tmp_path / "bad_version.vault"
    # Write version 0xFF instead of 0x01
    vault_file.write_bytes(b"\xff" + b"\x00" * 100)

    decrypted = tmp_path / "decrypted.txt"
    with pytest.raises(ValueError, match="Unsupported vault file version"):
        decrypt_file_stream(vault_file, decrypted, vault_key)


def test_atomic_write_cleanup(
    tmp_path: Path, vault_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If _atomic_write_bytes fails, the temp file must be cleaned up."""
    from doctoragent.security import crypto

    source = tmp_path / "secret.txt"
    destination = tmp_path / "secret.txt.vault"
    source.write_bytes(b"cleanup test")

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)

    # Monkey-patch os.replace to simulate a failure
    def failing_replace(src: str, dst: str) -> None:
        raise OSError("Simulated failure")

    monkeypatch.setattr(crypto.os, "replace", failing_replace)

    with pytest.raises(OSError, match="Simulated failure"):
        encrypt_file_stream(source, destination, file_key, salt)

    # The destination should not exist (atomic write failed before rename)
    assert not destination.exists()
    # No temp files should be left behind
    temp_files = list(destination.parent.glob(f".{destination.name}.*.tmp"))
    assert len(temp_files) == 0


def test_large_file_roundtrip(tmp_path: Path, vault_key: bytes) -> None:
    """Encrypt and decrypt a 1MB file successfully."""
    source = tmp_path / "large.bin"
    destination = tmp_path / "large.bin.vault"
    decrypted = tmp_path / "large_decrypted.bin"

    # Generate 1MB of random data
    data = os.urandom(1024 * 1024)
    source.write_bytes(data)

    salt = generate_salt()
    file_key = derive_file_key(vault_key, salt)
    encrypt_file_stream(source, destination, file_key, salt)

    assert destination.exists()
    # Encrypted file should be larger due to version + salt + nonce + tag
    assert destination.stat().st_size > len(data)

    decrypt_file_stream(destination, decrypted, file_key)
    assert decrypted.read_bytes() == data

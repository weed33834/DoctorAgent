"""Tests for secure field-level storage."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from doctoragent.connections.secure_storage import (
    _key_file_path,
    _load_or_create_fallback_key,
    seal,
    seal_dict,
    unseal,
    unseal_dict,
)


@pytest.fixture
def isolated_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a temporary key file so tests do not touch the user's home directory."""
    key_path = tmp_path / "storage.key"
    monkeypatch.setenv("DOCTORAGENT_STORAGE_KEY_FILE", str(key_path))
    # Clear the module-level key cache so each test re-reads from the env path.
    _load_or_create_fallback_key.cache_clear()
    return key_path


def test_empty_value_roundtrip() -> None:
    """Empty strings are passed through unchanged."""
    assert seal("") == ""
    assert unseal("") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="Uses AES fallback, not DPAPI on Windows")
def test_fallback_seal_roundtrip(isolated_key_file: Path) -> None:
    """On non-Windows, values are encrypted with AES-256-GCM and prefixed with 'aes:'."""
    original = "secret-api-key"
    sealed = seal(original)
    assert sealed.startswith("aes:")
    assert sealed != original
    assert unseal(sealed) == original


@pytest.mark.skipif(sys.platform == "win32", reason="Uses AES fallback, not DPAPI on Windows")
def test_fallback_key_is_persistent(isolated_key_file: Path) -> None:
    """The same key is reused across seal/unseal calls."""
    sealed = seal("secret-one")
    # Re-load the key and decrypt: should succeed without creating a new key.
    assert unseal(sealed) == "secret-one"
    assert isolated_key_file.exists()


def test_fallback_key_rejects_permissive_permissions(
    isolated_key_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error is raised when the key file has group/other read permissions."""
    isolated_key_file.write_bytes(os.urandom(32))
    isolated_key_file.chmod(0o644)

    with pytest.raises(RuntimeError, match="overly permissive"):
        _load_or_create_fallback_key()


def test_unknown_format_rejected() -> None:
    """Values without a recognised encryption prefix are rejected (downgrade guard)."""
    with pytest.raises(ValueError, match="recognised encryption prefix"):
        unseal("raw-value")


def test_key_file_path_respects_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DOCTORAGENT_STORAGE_KEY_FILE overrides the default key file path."""
    custom = tmp_path / "custom.key"
    monkeypatch.setenv("DOCTORAGENT_STORAGE_KEY_FILE", str(custom))
    assert _key_file_path() == custom


@pytest.mark.skipif(sys.platform == "win32", reason="Uses AES fallback, not DPAPI on Windows")
def test_seal_dict_encrypts_only_target_fields(isolated_key_file: Path) -> None:
    """seal_dict only encrypts the requested string fields."""
    data = {
        "api_key": "secret-key",
        "name": "Public Name",
        "password": "secret-password",
        "timeout": 30,
    }
    sealed = seal_dict(data, {"api_key", "password"})
    assert sealed["api_key"].startswith("aes:")
    assert sealed["password"].startswith("aes:")
    assert sealed["name"] == "Public Name"
    assert sealed["timeout"] == 30


def test_unseal_dict_restores_target_fields(isolated_key_file: Path) -> None:
    """unseal_dict decrypts the requested string fields.

    Sealing uses the same field-name AAD that unseal_dict applies, matching
    the production seal_dict -> unseal_dict round-trip.
    """
    data = {
        "api_key": seal("secret-key", field_name="api_key"),
        "password": seal("secret-password", field_name="password"),
        "name": "Public Name",
    }
    unsealed = unseal_dict(data, {"api_key", "password"})
    assert unsealed["api_key"].get_secret_value() == "secret-key"
    assert unsealed["password"].get_secret_value() == "secret-password"
    assert unsealed["name"] == "Public Name"


@pytest.mark.skipif(sys.platform == "win32", reason="DPAPI works on Windows; test is for non-Windows only")
def test_unseal_dpapi_raises_on_non_windows() -> None:
    """DPAPI values cannot be unsealed on non-Windows platforms."""
    with pytest.raises(RuntimeError, match="DPAPI"):
        unseal("dpapi:ZmFrZWNpcGhlcg==")


def test_seal_uses_dpapi_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, seal uses DPAPI and prefixes with 'dpapi:'."""
    monkeypatch.setattr("doctoragent.connections.secure_storage._is_windows", lambda: True)
    fake_protected = b"protected-data"
    mock_protect = MagicMock(return_value=fake_protected)
    monkeypatch.setattr("doctoragent.security.win_helpers.protect_data", mock_protect)

    result = seal("my-secret")

    assert result.startswith("dpapi:")
    mock_protect.assert_called_once_with(b"my-secret")


def test_unseal_uses_dpapi_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, unseal uses DPAPI for 'dpapi:' prefixed values."""
    monkeypatch.setattr("doctoragent.connections.secure_storage._is_windows", lambda: True)
    fake_unprotected = b"original-secret"
    mock_unprotect = MagicMock(return_value=fake_unprotected)
    monkeypatch.setattr("doctoragent.security.win_helpers.unprotect_data", mock_unprotect)

    result = unseal("dpapi:cHJvdGVjdGVkLWRhdGE=")

    assert result == "original-secret"
    mock_unprotect.assert_called_once()

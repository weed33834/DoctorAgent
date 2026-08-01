"""Tests for the Windows Hello helper.

The real Windows Runtime API is not available on Linux, so these tests verify
platform-safe degradation and exercise the high-level behaviour with mocked
verification results.
"""

import sys
from pathlib import Path

import pytest

from doctoragent.security import windows_hello


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows degradation test")
def test_verify_user_identity_raises_on_linux() -> None:
    """verify_user_identity raises a clear RuntimeError on Linux."""
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        windows_hello.verify_user_identity()


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows degradation test")
def test_get_key_derivation_salt_raises_on_linux() -> None:
    """get_key_derivation_salt raises a clear RuntimeError on Linux."""
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        windows_hello.get_key_derivation_salt(Path("/tmp/dummy"))


def test_get_key_derivation_salt_returns_existing_salt_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After successful verification the persisted salt is returned."""
    storage = tmp_path / "master_key.bin"
    salt_path = tmp_path / windows_hello.SALT_FILE_NAME
    expected_salt = b"existing-salt" * 2 + b"xx"
    salt_path.write_bytes(expected_salt)

    monkeypatch.setattr(
        "doctoragent.security.windows_hello._require_windows",
        lambda: None,
    )
    monkeypatch.setattr(
        "doctoragent.security.windows_hello._verify_user_identity_winrt",
        lambda _message: True,
    )

    result = windows_hello.get_key_derivation_salt(storage)
    assert result == expected_salt


def test_get_key_derivation_salt_creates_salt_on_first_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new salt is generated and persisted after the first successful verification."""
    storage = tmp_path / "master_key.bin"
    salt_path = tmp_path / windows_hello.SALT_FILE_NAME

    monkeypatch.setattr(
        "doctoragent.security.windows_hello._require_windows",
        lambda: None,
    )
    monkeypatch.setattr(
        "doctoragent.security.windows_hello._verify_user_identity_winrt",
        lambda _message: True,
    )

    assert not salt_path.exists()
    salt1 = windows_hello.get_key_derivation_salt(storage)
    assert salt_path.exists()
    assert len(salt1) == windows_hello.SALT_LEN

    # A later successful verification returns the same persisted salt.
    salt2 = windows_hello.get_key_derivation_salt(storage)
    assert salt1 == salt2


def test_get_key_derivation_salt_raises_when_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user cancels or fails verification, no salt is returned."""
    storage = tmp_path / "master_key.bin"

    monkeypatch.setattr(
        "doctoragent.security.windows_hello._require_windows",
        lambda: None,
    )
    monkeypatch.setattr(
        "doctoragent.security.windows_hello._verify_user_identity_winrt",
        lambda _message: False,
    )

    with pytest.raises(windows_hello.WindowsHelloError):
        windows_hello.get_key_derivation_salt(storage)


def test_get_key_derivation_salt_uses_custom_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verification message is forwarded to the Windows implementation."""
    storage = tmp_path / "master_key.bin"
    captured: list[str] = []

    def fake_verify(message: str) -> bool:
        captured.append(message)
        return True

    monkeypatch.setattr(
        "doctoragent.security.windows_hello._require_windows",
        lambda: None,
    )
    monkeypatch.setattr(
        "doctoragent.security.windows_hello._verify_user_identity_winrt",
        fake_verify,
    )

    windows_hello.get_key_derivation_salt(storage, message="Custom prompt")
    assert captured == ["Custom prompt"]

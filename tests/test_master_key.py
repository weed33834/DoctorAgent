"""Tests for master key providers."""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_skip_windows_permissions = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permissions not enforced on Windows"
)

from doctoragent.security.master_key import (
    _REGISTRY,
    DpapiMasterKeyProvider,
    FilePasswordProvider,
    KeychainMasterKeyProvider,
    TpmMasterKeyProvider,
    _derive_final_key,
    create_master_key_provider,
    get_registered_providers,
    register_provider,
)


@_skip_windows_permissions
def test_file_password_provider_deterministic(tmp_path: Path) -> None:
    """Same password and storage_path produces the same key."""
    p1 = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    p2 = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    assert p1.get_key() == p2.get_key()
    assert len(p1.get_key()) == 32
    assert (tmp_path / "filepassword.salt").exists()


def test_file_password_provider_password_file(tmp_path: Path) -> None:
    """Provider can read password from a file."""
    pw_file = tmp_path / "password.txt"
    pw_file.write_text("file-password")
    provider = FilePasswordProvider(password_file=pw_file, storage_path=tmp_path)
    assert provider.exists()
    key = provider.get_key()
    assert len(key) == 32


def test_file_password_provider_rejects_both_args() -> None:
    """Cannot specify both password and password_file."""
    with pytest.raises(ValueError):
        FilePasswordProvider(password="x", password_file=Path("/tmp/pw"))


def test_file_password_provider_different_storage_different_keys(tmp_path: Path) -> None:
    """Different storage_path values must produce different keys for the same password."""
    p1 = FilePasswordProvider(password="hello-world", storage_path=tmp_path / "a")
    p2 = FilePasswordProvider(password="hello-world", storage_path=tmp_path / "b")
    assert p1.get_key() != p2.get_key()


@_skip_windows_permissions
def test_file_password_provider_salt_reloads_after_restart(tmp_path: Path) -> None:
    """A provider created after the salt file exists reuses the persisted salt."""
    p1 = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    key1 = p1.get_key()

    p2 = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    key2 = p2.get_key()

    assert key1 == key2
    salt = (tmp_path / "filepassword.salt").read_bytes()
    assert len(salt) == 32


@_skip_windows_permissions
def test_file_password_provider_salt_file_permissions(tmp_path: Path) -> None:
    """The salt file is created with owner-only read/write permissions."""
    provider = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    provider.get_key()
    salt_path = tmp_path / "filepassword.salt"
    mode = stat.S_IMODE(salt_path.stat().st_mode)
    expected = stat.S_IRUSR | stat.S_IWUSR
    assert mode == expected, f"Expected {oct(expected)}, got {oct(mode)}"


@_skip_windows_permissions
def test_file_password_provider_concurrent_init_converges(tmp_path: Path) -> None:
    """Simulated concurrent initialisation converges on the persisted salt."""
    salt_path = tmp_path / "filepassword.salt"
    salt_path.write_bytes(os.urandom(32))
    # Mirror production permissions (owner-only); _validate_salt_file rejects
    # group/other-accessible salt files.
    os.chmod(salt_path, stat.S_IRUSR | stat.S_IWUSR)
    p1 = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    p2 = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    # Both providers must derive the same key from the pre-existing salt.
    assert p1.get_key() == p2.get_key()


def test_file_password_provider_no_storage_path_uses_ephemeral_salt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without storage_path a random ephemeral salt is used and a warning is logged."""
    provider = FilePasswordProvider(password="hello-world")
    key1 = provider.get_key()
    key2 = provider.get_key()
    assert len(key1) == 32
    # Same instance caches the ephemeral salt.
    assert key1 == key2
    assert "ephemeral random salt" in caplog.text


def test_file_password_provider_salt_is_cached_in_memory(tmp_path: Path) -> None:
    """_get_or_create_salt returns the cached value on subsequent calls."""
    provider = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    salt1 = provider._get_or_create_salt()
    salt2 = provider._get_or_create_salt()
    assert salt1 == salt2
    # And it matches the file.
    assert salt1 == (tmp_path / "filepassword.salt").read_bytes()


@_skip_windows_permissions
def test_file_password_provider_atomic_write_existing_salt(tmp_path: Path) -> None:
    """Atomic write returns silently when the salt file already exists."""
    salt_path = tmp_path / "filepassword.salt"
    salt_path.write_bytes(os.urandom(32))
    # Mirror production permissions (owner-only); _validate_salt_file rejects
    # group/other-accessible salt files.
    os.chmod(salt_path, stat.S_IRUSR | stat.S_IWUSR)
    provider = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    key = provider.get_key()
    assert len(key) == 32


def test_file_password_provider_atomic_write_existing_file_directly(tmp_path: Path) -> None:
    """The atomic helper itself returns silently when the target file exists."""
    salt_path = tmp_path / "filepassword.salt"
    salt_path.write_bytes(os.urandom(32))
    FilePasswordProvider._atomic_write_salt(salt_path, b"new-salt")
    assert salt_path.read_bytes() != b"new-salt"


def test_file_password_provider_atomic_write_closes_fd_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If writing fails the file descriptor is closed before the exception propagates."""
    fake_fd = 12345
    closed_fds = []

    def fake_open(path: str | os.PathLike[str], flags: int, mode: int) -> int:
        return fake_fd

    def fake_fdopen(fd: int, mode: str) -> object:
        raise OSError("simulated write failure")

    def fake_close(fd: int) -> None:
        closed_fds.append(fd)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fdopen", fake_fdopen)
    monkeypatch.setattr(os, "close", fake_close)

    with pytest.raises(OSError, match="simulated write failure"):
        FilePasswordProvider._atomic_write_salt(tmp_path / "filepassword.salt", b"x")

    assert closed_fds == [fake_fd]


def test_file_password_provider_atomic_write_ignores_close_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError while closing the fd is swallowed before the original exception."""
    fake_fd = 12345

    def fake_open(path: str | os.PathLike[str], flags: int, mode: int) -> int:
        return fake_fd

    def fake_fdopen(fd: int, mode: str) -> object:
        raise OSError("simulated write failure")

    def fake_close(fd: int) -> None:
        raise OSError("close failed")

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fdopen", fake_fdopen)
    monkeypatch.setattr(os, "close", fake_close)

    with pytest.raises(OSError, match="simulated write failure"):
        FilePasswordProvider._atomic_write_salt(tmp_path / "filepassword.salt", b"x")


def test_file_password_provider_no_password() -> None:
    """Provider without a password raises RuntimeError."""
    provider = FilePasswordProvider()
    assert not provider.exists()
    with pytest.raises(RuntimeError, match="No password configured"):
        provider.get_key()


def test_tpm_provider_can_be_instantiated_on_linux(tmp_path: Path) -> None:
    """TPM provider is safe to construct on non-Windows platforms."""
    provider = TpmMasterKeyProvider(tmp_path / "tpm.bin")
    assert not provider.exists()
    assert provider.tpm_key_name == "DoctorAgentTPMMasterKey"


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-only TPM degradation test")
def test_tpm_provider_get_key_on_linux_raises_clear_error(tmp_path: Path) -> None:
    """TPM provider raises a clear RuntimeError when get_key is called on Linux."""
    provider = TpmMasterKeyProvider(tmp_path / "tpm.bin")
    with pytest.raises(RuntimeError, match="only available on Windows"):
        provider.get_key()


def test_tpm_provider_exists_reflects_storage_file(tmp_path: Path) -> None:
    """TPM provider exists() mirrors the storage file presence."""
    storage = tmp_path / "tpm.bin"
    provider = TpmMasterKeyProvider(storage)
    assert not provider.exists()
    storage.write_bytes(b"encrypted")
    assert provider.exists()


def test_tpm_provider_with_hello_salt_caches_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Windows APIs are mocked, TPM provider returns a deterministic key."""
    storage = tmp_path / "tpm.bin"
    provider = TpmMasterKeyProvider(storage, hello_salt=b"hello-salt" * 4)

    encrypted: list[bytes] = []

    def fake_encrypt(key_name: str, plaintext: bytes, overwrite: bool) -> bytes:
        encrypted.append(plaintext)
        return b"blob" + plaintext

    def fake_decrypt(key_name: str, ciphertext: bytes) -> bytes:
        return ciphertext[len("blob") :]

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "doctoragent.security.master_key._ncrypt_encrypt_with_persistent_key",
        fake_encrypt,
    )
    monkeypatch.setattr(
        "doctoragent.security.master_key._ncrypt_decrypt_with_persistent_key",
        fake_decrypt,
    )

    key1 = provider.get_key()
    key2 = provider.get_key()
    assert len(key1) == 32
    assert key1 == key2
    assert len(encrypted) == 1
    assert storage.read_bytes() == b"blob" + encrypted[0]


def test_tpm_provider_requires_existing_blob_when_decrypting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decrypt path reads the existing blob and derives the same key with salt."""
    storage = tmp_path / "tpm.bin"
    storage.write_bytes(b"encrypted-material")
    provider = TpmMasterKeyProvider(storage, hello_salt=b"salt" * 8)

    def fake_decrypt(key_name: str, ciphertext: bytes) -> bytes:
        return b"raw-key-material" * 2

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "doctoragent.security.master_key._ncrypt_decrypt_with_persistent_key",
        fake_decrypt,
    )

    key = provider.get_key()
    assert len(key) == 32
    # Same raw material and salt must produce the same key.
    assert key == provider.get_key()


def test_derive_final_key_without_salt_returns_material() -> None:
    """Without a hello salt the TPM material is used directly."""
    material = os.urandom(32)
    assert _derive_final_key(material, None) == material


def test_derive_final_key_with_salt_is_deterministic() -> None:
    """HKDF with the same salt and material is deterministic."""
    material = b"material" * 4
    salt = b"salt" * 8
    key1 = _derive_final_key(material, salt)
    key2 = _derive_final_key(material, salt)
    assert len(key1) == 32
    assert key1 == key2
    assert key1 != material


def test_dpapi_provider_exists(tmp_path: Path) -> None:
    """DPAPI provider reflects whether the storage file exists."""
    storage = tmp_path / "master_key.bin"
    provider = DpapiMasterKeyProvider(storage_path=storage)
    assert not provider.exists()
    storage.write_bytes(b"protected")
    assert provider.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-only degradation test")
def test_dpapi_provider_get_key_on_linux(tmp_path: Path) -> None:
    """DPAPI provider raises a clear error when get_key is called on Linux."""
    provider = DpapiMasterKeyProvider(storage_path=tmp_path / "master_key.bin")
    with pytest.raises(RuntimeError, match="only available on Windows"):
        provider.get_key()


def test_dpapi_provider_protect_and_store_delegates_to_win_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_protect_and_store writes the output of win_helpers.protect_data to disk."""
    provider = DpapiMasterKeyProvider(storage_path=tmp_path / "master_key.bin")

    def fake_protect(data: bytes) -> bytes:
        return b"protected:" + data

    monkeypatch.setattr("doctoragent.security.win_helpers.protect_data", fake_protect)
    provider._protect_and_store(b"master-secret")
    assert provider.storage_path.read_bytes() == b"protected:master-secret"


def test_dpapi_provider_generates_and_protects_new_key_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows a missing storage file triggers key generation and protection."""
    provider = DpapiMasterKeyProvider(storage_path=tmp_path / "master_key.bin")

    protected = []

    def fake_protect(data: bytes) -> bytes:
        protected.append(data)
        return b"blob" + data

    monkeypatch.setattr("doctoragent.security.win_helpers.protect_data", fake_protect)
    monkeypatch.setattr(sys, "platform", "win32")

    key = provider.get_key()
    assert len(key) == 32
    assert protected == [key]
    assert provider.storage_path.read_bytes() == b"blob" + key


def test_factory_creates_builtin_providers(tmp_path: Path) -> None:
    """Factory returns the correct built-in provider instances."""
    fp = create_master_key_provider("filepassword", tmp_path, password="x")
    assert isinstance(fp, FilePasswordProvider)

    dp = create_master_key_provider("dpapi", tmp_path / "dpapi.bin")
    assert isinstance(dp, DpapiMasterKeyProvider)

    tpm = create_master_key_provider("tpm", tmp_path / "tpm.bin")
    assert isinstance(tpm, TpmMasterKeyProvider)
    assert tpm.hello_salt is None

    tpm_with_salt = create_master_key_provider("tpm", tmp_path / "tpm_salt.bin", hello_salt=b"salt")
    assert isinstance(tpm_with_salt, TpmMasterKeyProvider)
    assert tpm_with_salt.hello_salt == b"salt"

    # Case-insensitive lookup.
    assert isinstance(
        create_master_key_provider("FilePassword", tmp_path, password="x"),
        FilePasswordProvider,
    )


def test_factory_unknown_provider() -> None:
    """Factory rejects unknown provider names."""
    with pytest.raises(ValueError):
        create_master_key_provider("unknown", Path("/tmp/dummy"))


def test_register_provider_round_trip(tmp_path: Path) -> None:
    """Custom providers can be registered and instantiated through the factory."""
    before = dict(_REGISTRY)
    try:

        class DummyProvider(FilePasswordProvider):
            pass

        register_provider("dummy", DummyProvider)
        assert "dummy" in get_registered_providers()
        provider = create_master_key_provider("dummy", tmp_path, password="x")
        assert isinstance(provider, DummyProvider)
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(before)


def test_register_provider_rejects_non_provider() -> None:
    """Registering a class that is not a MasterKeyProvider raises TypeError."""
    with pytest.raises(TypeError):
        register_provider("bad", str)  # type: ignore[arg-type]


def test_factory_reuses_cached_dpapi_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DPAPI provider caches the decrypted key and does not re-read the file."""
    storage = tmp_path / "master_key.bin"
    storage.write_bytes(b"protected")
    provider = DpapiMasterKeyProvider(storage_path=storage)

    mock_unprotect = MagicMock(return_value=b"decrypted-key")
    monkeypatch.setattr("doctoragent.security.win_helpers.unprotect_data", mock_unprotect)
    monkeypatch.setattr(sys, "platform", "win32")

    assert provider.get_key() == b"decrypted-key"
    mock_unprotect.assert_called_once_with(b"protected")


def test_file_password_provider_clear_clears_cached_key(tmp_path: Path) -> None:
    """clear() zeros the cached key and forces re-derivation on next get_key()."""
    provider = FilePasswordProvider(password="hello-world", storage_path=tmp_path)
    key = provider.get_key()
    provider.clear()
    assert provider._key is None
    assert provider.get_key() == key


def test_dpapi_provider_clear_clears_cached_key(tmp_path: Path) -> None:
    """clear() zeros the cached DPAPI key."""
    provider = DpapiMasterKeyProvider(storage_path=tmp_path / "master_key.bin")
    provider._key = b"cached-key"
    provider.clear()
    assert provider._key is None


def test_tpm_provider_clear_clears_cached_key(tmp_path: Path) -> None:
    """clear() zeros the cached TPM key."""
    provider = TpmMasterKeyProvider(storage_path=tmp_path / "tpm.bin")
    provider._key = b"cached-key"
    provider.clear()
    assert provider._key is None


# ── KeychainMasterKeyProvider tests ───────────────────────────────────────────


def test_keychain_provider_can_be_instantiated_on_linux(tmp_path: Path) -> None:
    """Keychain provider is safe to construct on non-macOS platforms."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")
    assert not provider.exists()
    assert provider.service_name == "DoctorAgent.keychain"
    assert provider.account_name == "master_key"


def test_keychain_provider_exists_on_linux_returns_false(tmp_path: Path) -> None:
    """On Linux exists() always returns False (no keychain CLI)."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")
    assert provider.exists() is False


def test_keychain_provider_get_key_on_linux_raises(tmp_path: Path) -> None:
    """Keychain provider raises NotImplementedError on Linux."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")
    with pytest.raises(NotImplementedError, match="only available on macOS"):
        provider.get_key()


def test_keychain_provider_protect_on_linux_raises(tmp_path: Path) -> None:
    """protect() raises NotImplementedError on Linux."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")
    with pytest.raises(NotImplementedError, match="only available on macOS"):
        provider.protect(b"test-key-material-32bytes-here!")


def test_keychain_provider_unprotect_on_linux_raises(tmp_path: Path) -> None:
    """unprotect() raises NotImplementedError on Linux."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")
    with pytest.raises(NotImplementedError, match="only available on macOS"):
        provider.unprotect()


def test_keychain_provider_clear_clears_cached_key(tmp_path: Path) -> None:
    """clear() zeros the cached keychain key."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")
    provider._key = b"cached-key"
    provider.clear()
    assert provider._key is None


def test_keychain_provider_generates_and_protects_new_key_on_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS a missing keychain entry triggers key generation and protection."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")

    protected: list[bytes] = []

    def fake_protect(key_material: bytes) -> None:
        protected.append(key_material)

    monkeypatch.setattr(provider, "protect", fake_protect)
    # Force platform to darwin so get_key() proceeds past the platform check.
    monkeypatch.setattr(sys, "platform", "darwin")
    # exists() returns False to trigger generation.
    monkeypatch.setattr(provider, "exists", lambda: False)

    key = provider.get_key()
    assert len(key) == 32
    assert len(protected) == 1
    assert protected[0] == key


def test_keychain_provider_get_key_caches_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keychain provider caches the decrypted key in memory."""
    provider = KeychainMasterKeyProvider(tmp_path / "keychain.bin")

    call_count = 0

    def fake_unprotect() -> bytes:
        nonlocal call_count
        call_count += 1
        return b"decrypted-key-material!!!!-----"

    monkeypatch.setattr(provider, "unprotect", fake_unprotect)
    monkeypatch.setattr(provider, "exists", lambda: True)
    monkeypatch.setattr(sys, "platform", "darwin")

    key1 = provider.get_key()
    key2 = provider.get_key()
    assert key1 == key2
    assert call_count == 1


def test_keychain_provider_service_name_default(tmp_path: Path) -> None:
    """Default service_name uses 'DoctorAgent' prefix."""
    provider = KeychainMasterKeyProvider(tmp_path / "my_vault.bin")
    assert provider.service_name == "DoctorAgent.my_vault"


def test_keychain_provider_service_name_custom(tmp_path: Path) -> None:
    """Custom service_name is respected."""
    provider = KeychainMasterKeyProvider(tmp_path / "my_vault.bin", service_name="MyApp")
    assert provider.service_name == "MyApp.my_vault"


def test_factory_creates_keychain_provider(tmp_path: Path) -> None:
    """Factory returns KeychainMasterKeyProvider for 'mac-keychain'."""
    kc = create_master_key_provider("mac-keychain", tmp_path / "kc.bin")
    assert isinstance(kc, KeychainMasterKeyProvider)

    # Case-insensitive lookup.
    kc2 = create_master_key_provider("Mac-Keychain", tmp_path / "kc2.bin")
    assert isinstance(kc2, KeychainMasterKeyProvider)


def test_keychain_provider_registered_in_factory(tmp_path: Path) -> None:
    """Keychain provider is in the registered providers registry."""
    assert "mac-keychain" in get_registered_providers()
    assert get_registered_providers()["mac-keychain"] is KeychainMasterKeyProvider

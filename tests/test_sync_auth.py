"""Tests for device authorization and revocation (auth.py)."""

import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from doctoragent.sync.auth import (
    MAX_PAIRED_DEVICES,
    PAIRING_CODE_DIGITS,
    DeviceAuth,
    _derive_shared_secret,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def config_dir(tmp_path: os.PathLike[str]) -> os.PathLike[str]:
    """Temporary config directory for device auth persistence."""
    return tmp_path / "config"


@pytest.fixture
def auth(config_dir: os.PathLike[str]) -> DeviceAuth:
    """A DeviceAuth instance backed by a temp directory."""
    return DeviceAuth(config_dir)


# ── Construction ─────────────────────────────────────────────────────────────


def test_auth_construction(config_dir: os.PathLike[str]) -> None:
    """DeviceAuth should create the config directory if missing."""
    DeviceAuth(config_dir)
    assert config_dir.exists()  # type: ignore[union-attr]


def test_auth_no_devices_initially(auth: DeviceAuth) -> None:
    """New DeviceAuth should have no authorized devices."""
    assert auth.list_authorized_devices() == []


# ── Pairing code generation ──────────────────────────────────────────────────


class TestPairingCode:
    """Tests for pairing code generation."""

    def test_generate_creates_session(self, auth: DeviceAuth) -> None:
        code = auth.generate_pairing_code()
        assert code
        assert len(code) == PAIRING_CODE_DIGITS
        assert code.isdigit()
        assert auth._pending_pairing is not None

    def test_pending_public_key(self, auth: DeviceAuth) -> None:
        auth.generate_pairing_code()
        pub = auth.get_pending_public_key()
        assert pub is not None
        assert len(pub) == 32  # X25519 public key

    def test_pending_public_key_none_when_no_session(self, auth: DeviceAuth) -> None:
        assert auth.get_pending_public_key() is None

    def test_multiple_generate_overwrites_session(self, auth: DeviceAuth) -> None:
        code1 = auth.generate_pairing_code()
        pub1 = auth.get_pending_public_key()
        code2 = auth.generate_pairing_code()
        pub2 = auth.get_pending_public_key()
        assert code1 != code2
        assert pub1 != pub2  # Different ephemeral key pairs

    def test_pairing_code_expiry(self, auth: DeviceAuth, monkeypatch: pytest.MonkeyPatch) -> None:
        auth.generate_pairing_code()
        # Simulate time passing beyond TTL by advancing the expiry directly
        auth._pending_pairing.expires_at = time.time() - 1  # expired
        pub = auth.get_pending_public_key()
        assert pub is None


# ── Device pairing ───────────────────────────────────────────────────────────


class TestDevicePairing:
    """End-to-end pairing flow tests."""

    def test_full_pairing_flow(self, auth: DeviceAuth) -> None:
        """Simulate a full two-device pairing."""
        # Device A (initiator) generates pairing code
        code = auth.generate_pairing_code()
        initiator_pub = auth.get_pending_public_key()
        assert initiator_pub is not None

        # Device B (responder) accepts the pairing
        auth2 = DeviceAuth(auth._config_dir)
        device_id_b, responder_pub = auth2.accept_pairing(code, initiator_pub, "Device B")
        assert device_id_b
        assert len(device_id_b) == 32
        assert len(responder_pub) == 32

        # Device A completes pairing with B's public key
        device_id_a = auth.pair_device("Device A", code, responder_pub)
        assert device_id_a
        assert len(device_id_a) == 32

        # Both should now be authorized
        assert auth.is_authorized(device_id_a)
        assert auth2.is_authorized(device_id_b)

        # Shared secrets should match
        secret_a = auth.get_shared_secret(device_id_a)
        secret_b = auth2.get_shared_secret(device_id_b)
        assert secret_a is not None
        assert secret_b is not None
        assert secret_a == secret_b

    def test_pairing_code_mismatch(self, auth: DeviceAuth) -> None:
        """Wrong pairing code should raise an error."""
        auth.generate_pairing_code()
        # Simulate a peer sending an arbitrary public key with wrong code
        fake_pub = X25519PrivateKey.generate().public_key()
        fake_pub_bytes = fake_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        with pytest.raises(RuntimeError, match="Pairing code mismatch"):
            auth.pair_device("Bad Device", "000000", fake_pub_bytes)

    def test_pairing_no_pending_session(self, auth: DeviceAuth) -> None:
        """pair_device without generate_pairing_code should raise."""
        with pytest.raises(RuntimeError, match="No pending pairing session"):
            auth.pair_device("Device", "123456", b"\x00" * 32)

    def test_accept_pairing_max_devices(self, auth: DeviceAuth) -> None:
        """accept_pairing should reject when max devices reached."""
        # Fill up to max
        for i in range(MAX_PAIRED_DEVICES):
            auth._devices[f"dev-{i}"] = auth._make_record(f"dev-{i}", f"Device {i}", os.urandom(32))
        with pytest.raises(RuntimeError, match="Maximum paired devices"):
            auth.accept_pairing("123456", b"\x00" * 32, "New Device")


# ── Device authorization ─────────────────────────────────────────────────────


class TestDeviceAuthManagement:
    """Tests for authorization checks and device listing."""

    def test_is_authorized_true(self, auth: DeviceAuth) -> None:
        code = auth.generate_pairing_code()
        initiator_pub = auth.get_pending_public_key()
        assert initiator_pub is not None
        # Use a second instance that shares the same config_dir to add Device B
        auth2 = DeviceAuth(auth._config_dir)
        device_id_b, responder_pub = auth2.accept_pairing(code, initiator_pub, "Device B")
        auth.pair_device("Device A", code, responder_pub)
        # Reload from disk to pick up Device B from the merged save
        auth = DeviceAuth(auth._config_dir)
        assert auth.is_authorized(device_id_b)

    def test_is_authorized_false(self, auth: DeviceAuth) -> None:
        assert not auth.is_authorized("non-existent-device")

    def test_list_authorized_devices(self, auth: DeviceAuth) -> None:
        auth._devices["d1"] = auth._make_record("d1", "One", os.urandom(32))
        auth._devices["d2"] = auth._make_record("d2", "Two", os.urandom(32))
        devices = auth.list_authorized_devices()
        assert len(devices) == 2
        names = {d["device_name"] for d in devices}
        assert names == {"One", "Two"}

    def test_get_shared_secret_nonexistent(self, auth: DeviceAuth) -> None:
        assert auth.get_shared_secret("no-such-device") is None

    def test_shared_secret_encrypted_at_rest(self, auth: DeviceAuth) -> None:
        """Shared secret should be sealed in the persisted JSON file."""
        code = auth.generate_pairing_code()
        initiator_pub = auth.get_pending_public_key()
        assert initiator_pub is not None
        device_id_b, responder_pub = DeviceAuth(auth._config_dir).accept_pairing(
            code, initiator_pub, "Device B"
        )
        auth.pair_device("Device A", code, responder_pub)

        # Check the persisted file
        import json

        devices_path = auth._config_dir / "devices.json"
        data = json.loads(devices_path.read_text())
        for entry in data:
            secret = entry.get("sealed_secret", "")
            # Should be encrypted (not raw hex)
            assert secret
            assert "aes:" in secret or "dpapi:" in secret

    def test_touch_device(self, auth: DeviceAuth) -> None:
        auth._devices["d1"] = auth._make_record("d1", "One", os.urandom(32))
        old_time = auth._devices["d1"].last_seen
        time.sleep(0.01)
        auth.touch_device("d1")
        new_time = auth._devices["d1"].last_seen
        assert new_time > old_time


# ── Device revocation ────────────────────────────────────────────────────────


class TestDeviceRevocation:
    """Tests for revoking device authorization."""

    def test_revoke_known_device(self, auth: DeviceAuth) -> None:
        auth._devices["d1"] = auth._make_record("d1", "One", os.urandom(32))
        assert auth.is_authorized("d1")
        auth.revoke_device("d1")
        assert not auth.is_authorized("d1")

    def test_revoke_unknown_device(self, auth: DeviceAuth) -> None:
        with pytest.raises(KeyError, match="Device not found"):
            auth.revoke_device("no-such-device")

    def test_revoke_persisted(self, auth: DeviceAuth) -> None:
        """Revocation should be persisted to disk."""
        auth._devices["d1"] = auth._make_record("d1", "One", os.urandom(32))
        auth.revoke_device("d1")

        # Reload from disk
        auth2 = DeviceAuth(auth._config_dir)
        assert not auth2.is_authorized("d1")

    def test_revoked_device_shared_secret_unavailable(self, auth: DeviceAuth) -> None:
        """After revocation, get_shared_secret returns None."""
        auth._devices["d1"] = auth._make_record("d1", "One", os.urandom(32))
        auth.revoke_device("d1")
        assert auth.get_shared_secret("d1") is None


# ── Persistence ──────────────────────────────────────────────────────────────


class TestPersistence:
    """Tests for load/save persistence."""

    def test_save_and_reload(self, auth: DeviceAuth) -> None:
        code = auth.generate_pairing_code()
        initiator_pub = auth.get_pending_public_key()
        assert initiator_pub is not None
        auth2 = DeviceAuth(auth._config_dir)
        device_id_b, responder_pub = auth2.accept_pairing(code, initiator_pub, "Device B")
        auth.pair_device("Device A", code, responder_pub)

        # Reload from same config dir
        auth2 = DeviceAuth(auth._config_dir)
        devices = auth2.list_authorized_devices()
        assert len(devices) == 2

    def test_load_corrupt_json(self, auth: DeviceAuth) -> None:
        """Corrupt JSON file should be handled gracefully."""
        devices_path = auth._config_dir / "devices.json"
        devices_path.write_text("not valid json")
        auth2 = DeviceAuth(auth._config_dir)
        assert auth2.list_authorized_devices() == []

    def test_load_missing_file(self, config_dir: os.PathLike[str]) -> None:
        """Missing devices.json should not cause errors."""
        auth = DeviceAuth(config_dir)
        assert auth.list_authorized_devices() == []


# ── Key derivation ───────────────────────────────────────────────────────────


class TestKeyDerivation:
    """Tests for X25519 shared secret derivation."""

    def test_derive_shared_secret_deterministic(self) -> None:
        """Same inputs produce the same shared secret."""
        priv_a = X25519PrivateKey.generate()
        pub_a = priv_a.public_key()
        priv_b = X25519PrivateKey.generate()
        pub_b = priv_b.public_key()

        code = "123456"
        secret1 = _derive_shared_secret(priv_a, pub_b, code)
        secret2 = _derive_shared_secret(priv_b, pub_a, code)
        assert secret1 == secret2
        assert len(secret1) == 32

    def test_derive_different_codes_different_secrets(self) -> None:
        """Different pairing codes produce different shared secrets."""
        priv_a = X25519PrivateKey.generate()
        priv_b = X25519PrivateKey.generate()
        pub_b = priv_b.public_key()

        s1 = _derive_shared_secret(priv_a, pub_b, "123456")
        s2 = _derive_shared_secret(priv_a, pub_b, "654321")
        assert s1 != s2


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case and error handling tests."""

    def test_max_devices_constraint(self, auth: DeviceAuth) -> None:
        for i in range(MAX_PAIRED_DEVICES):
            auth._devices[f"dev-{i}"] = auth._make_record(f"dev-{i}", f"Device {i}", os.urandom(32))
        code = auth.generate_pairing_code()
        pub = auth.get_pending_public_key()
        assert pub is not None
        with pytest.raises(RuntimeError, match="Maximum paired devices"):
            auth.pair_device("New", code, b"\x00" * 32)

    def test_zero_pairing_code(self, auth: DeviceAuth) -> None:
        """Edge case: all-zero pairing code mismatch."""
        auth.generate_pairing_code()
        with pytest.raises(RuntimeError, match="Pairing code mismatch"):
            auth.pair_device("Dev", "000000", b"\x00" * 32)

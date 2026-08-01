"""Tests for the secure sync protocol (protocol.py)."""

import json
import os

import pytest

from doctoragent.sync.protocol import (
    SecureSyncProtocol,
    SyncMessage,
    SyncState,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_secret() -> bytes:
    """32-byte shared secret for signing and transport encryption."""
    return os.urandom(32)


@pytest.fixture
def protocol(shared_secret: bytes) -> SecureSyncProtocol:
    """A protocol instance for device 'device-a'."""
    return SecureSyncProtocol("device-a", shared_secret)


@pytest.fixture
def protocol_b(shared_secret: bytes) -> SecureSyncProtocol:
    """A protocol instance for device 'device-b' with the same secret."""
    return SecureSyncProtocol("device-b", shared_secret)


# ── SyncMessage dataclass ────────────────────────────────────────────────────


def test_sync_message_defaults() -> None:
    """SyncMessage should have sensible defaults."""
    msg = SyncMessage()
    assert msg.version == 1
    assert msg.message_id == ""
    assert msg.message_type == ""
    assert msg.payload == {}
    assert msg.hmac == ""
    assert msg.timestamp == 0.0


def test_sync_message_custom() -> None:
    """SyncMessage should accept custom values."""
    msg = SyncMessage(
        version=1,
        message_id="abc123",
        message_type="hello",
        sender_device_id="dev-1",
        sender_device_name="My Device",
        payload={"key": "val"},
        hmac="deadbeef",
        timestamp=1234567890.0,
    )
    assert msg.sender_device_name == "My Device"
    assert msg.payload["key"] == "val"


# ── SecureSyncProtocol – construction ────────────────────────────────────────


def test_protocol_requires_32_byte_secret() -> None:
    """Constructor must reject non-32-byte shared secrets."""
    with pytest.raises(ValueError, match="32 bytes"):
        SecureSyncProtocol("dev", b"too-short")
    with pytest.raises(ValueError, match="32 bytes"):
        SecureSyncProtocol("dev", b"x" * 33)


def test_protocol_accepts_32_byte_secret() -> None:
    """Constructor should accept a 32-byte secret."""
    secret = os.urandom(32)
    proto = SecureSyncProtocol("dev", secret)
    assert proto.device_id == "dev"
    assert proto.shared_secret == secret


# ── Signing & verification ───────────────────────────────────────────────────


class TestSigning:
    """Tests for message signing and verification."""

    def test_sign_message_attaches_hmac(self, protocol: SecureSyncProtocol) -> None:
        msg = SyncMessage(
            message_id="test-1",
            message_type="hello",
            sender_device_id=protocol.device_id,
            timestamp=100.0,
        )
        signed = protocol.sign_message(msg)
        assert signed.hmac
        assert len(signed.hmac) == 64  # SHA256 hex digest

    def test_verify_valid_message(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_hello("my-device")
        assert protocol.verify_message(msg)

    def test_verify_message_rejects_tampered_payload(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_hello("my-device")
        msg.payload["capabilities"].append("extra_cap")
        assert not protocol.verify_message(msg)

    def test_verify_message_rejects_tampered_type(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_hello("my-device")
        msg.message_type = "bad_type"
        assert not protocol.verify_message(msg)

    def test_verify_message_rejects_no_hmac(self, protocol: SecureSyncProtocol) -> None:
        msg = SyncMessage(
            message_id="test",
            message_type="hello",
            sender_device_id=protocol.device_id,
            timestamp=100.0,
        )
        assert not protocol.verify_message(msg)

    def test_verify_cross_protocol(
        self, protocol: SecureSyncProtocol, protocol_b: SecureSyncProtocol
    ) -> None:
        """A message signed by protocol should verify on protocol_b (same secret)."""
        msg = protocol.create_hello("dev-a")
        assert protocol_b.verify_message(msg)

    def test_verify_fails_different_secret(self) -> None:
        """A message signed with one secret must not verify with another."""
        proto_a = SecureSyncProtocol("dev-a", os.urandom(32))
        proto_b = SecureSyncProtocol("dev-b", os.urandom(32))
        msg = proto_a.create_hello("dev-a")
        assert not proto_b.verify_message(msg)


# ── Message factories ────────────────────────────────────────────────────────


class TestMessageFactories:
    """Tests for create_hello, create_sync_request, etc."""

    def test_create_hello(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_hello("My Device")
        assert msg.message_type == "hello"
        assert msg.sender_device_id == "device-a"
        assert msg.sender_device_name == "My Device"
        assert msg.hmac
        assert protocol.verify_message(msg)

    def test_create_sync_request(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_sync_request("peer-id", since=100.0, sender_name="Me")
        assert msg.message_type == "sync_request"
        assert msg.payload["target_device_id"] == "peer-id"
        assert msg.payload["since"] == 100.0
        assert protocol.verify_message(msg)

    def test_create_sync_response(self, protocol: SecureSyncProtocol) -> None:
        files = [
            {"path": "a.txt", "hash": "abc", "mtime": 1.0, "size": 100},
        ]
        msg = protocol.create_sync_response(files, "Me")
        assert msg.message_type == "sync_response"
        assert len(msg.payload["files"]) == 1
        assert msg.payload["files"][0]["path"] == "a.txt"
        assert protocol.verify_message(msg)

    def test_create_sync_push(self, protocol: SecureSyncProtocol) -> None:
        file_meta = {"path": "b.txt", "hash": "def", "mtime": 2.0}
        msg = protocol.create_sync_push(file_meta, "Me")
        assert msg.message_type == "sync_push"
        assert msg.payload["file"]["path"] == "b.txt"
        assert protocol.verify_message(msg)

    def test_create_ack(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_ack("msg-123", "Me")
        assert msg.message_type == "ack"
        assert msg.payload["ack_message_id"] == "msg-123"
        assert protocol.verify_message(msg)


# ── Serialization ────────────────────────────────────────────────────────────


class TestPackUnpack:
    """Round-trip serialization tests."""

    def test_pack_unpack_roundtrip(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_hello("Test Device")
        data = protocol.pack(msg)
        assert isinstance(data, bytes)

        unpacked = protocol.unpack(data)
        assert unpacked.message_id == msg.message_id
        assert unpacked.message_type == msg.message_type
        assert unpacked.sender_device_id == msg.sender_device_id
        assert unpacked.hmac == msg.hmac
        assert unpacked.timestamp == msg.timestamp
        assert unpacked.payload == msg.payload

    def test_pack_is_json(self, protocol: SecureSyncProtocol) -> None:
        msg = protocol.create_hello("test")
        data = protocol.pack(msg)
        parsed = json.loads(data.decode("utf-8"))
        assert parsed["message_type"] == "hello"
        assert "hmac" in parsed


# ── Transport encryption ─────────────────────────────────────────────────────


class TestTransportEncryption:
    """End-to-end transport encryption tests."""

    def test_encrypt_decrypt_roundtrip(self, protocol: SecureSyncProtocol) -> None:
        plaintext = b"Hello, peer device!"
        transport_key = os.urandom(32)
        ciphertext = protocol.encrypt_transport(plaintext, transport_key)
        assert len(ciphertext) > len(plaintext)
        decrypted = protocol.decrypt_transport(ciphertext, transport_key)
        assert decrypted == plaintext

    def test_transport_tamper_detected(self, protocol: SecureSyncProtocol) -> None:
        from cryptography.exceptions import InvalidTag

        plaintext = b"secret data"
        transport_key = os.urandom(32)
        ciphertext = protocol.encrypt_transport(plaintext, transport_key)

        # Tamper with one byte of ciphertext
        tampered = bytearray(ciphertext)
        tampered[-1] ^= 0xFF

        with pytest.raises(InvalidTag):
            protocol.decrypt_transport(bytes(tampered), transport_key)

    def test_transport_wrong_key_fails(self, protocol: SecureSyncProtocol) -> None:
        from cryptography.exceptions import InvalidTag

        plaintext = b"secret data"
        key_a = os.urandom(32)
        key_b = os.urandom(32)
        ciphertext = protocol.encrypt_transport(plaintext, key_a)
        with pytest.raises(InvalidTag):
            protocol.decrypt_transport(ciphertext, key_b)

    def test_transport_non_random_nonce(self, protocol: SecureSyncProtocol) -> None:
        """Each encryption should use a different nonce."""
        plaintext = b"test"
        key = os.urandom(32)
        ct1 = protocol.encrypt_transport(plaintext, key)
        ct2 = protocol.encrypt_transport(plaintext, key)
        # Nonces differ → ciphertexts differ
        assert ct1 != ct2

    def test_transport_empty_plaintext(self, protocol: SecureSyncProtocol) -> None:
        key = os.urandom(32)
        ct = protocol.encrypt_transport(b"", key)
        decrypted = protocol.decrypt_transport(ct, key)
        assert decrypted == b""

    def test_full_stack_encrypted_exchange(
        self, protocol: SecureSyncProtocol, protocol_b: SecureSyncProtocol
    ) -> None:
        """Simulate a full encrypted message exchange between two devices."""
        # Device A creates and signs a sync request
        request = protocol.create_sync_request("device-b", since=0.0)
        # Pack and transport-encrypt
        packed = protocol.pack(request)
        encrypted = protocol.encrypt_transport(packed, protocol.shared_secret)
        # Device B decrypts, unpacks, and verifies
        decrypted = protocol_b.decrypt_transport(encrypted, protocol_b.shared_secret)
        unpacked = protocol_b.unpack(decrypted)
        assert protocol_b.verify_message(unpacked)
        assert unpacked.message_type == "sync_request"
        assert unpacked.sender_device_id == "device-a"
        assert unpacked.payload["target_device_id"] == "device-b"


# ── SyncState ────────────────────────────────────────────────────────────────


class TestSyncState:
    """Tests for sync state tracking."""

    def test_default_state(self) -> None:
        state = SyncState()
        assert state.last_sync_time == {}
        assert state.known_files == {}
        assert state.pending_changes == []

    def test_track_last_sync(self) -> None:
        state = SyncState()
        state.last_sync_time["device-a"] = 123.0
        assert state.last_sync_time["device-a"] == 123.0

    def test_track_known_files(self) -> None:
        state = SyncState()
        state.known_files["/vault/a.txt"] = "sha256:abc"
        assert state.known_files["/vault/a.txt"] == "sha256:abc"

    def test_pending_changes(self) -> None:
        state = SyncState()
        change = {"path": "/vault/b.txt", "op": "add", "hash": "sha256:def"}
        state.pending_changes.append(change)
        assert len(state.pending_changes) == 1
        assert state.pending_changes[0]["path"] == "/vault/b.txt"


# ── Canonical signing ────────────────────────────────────────────────────────


def test_canonical_sign_is_deterministic(protocol: SecureSyncProtocol) -> None:
    """Signing the same logical message twice produces the same HMAC."""
    msg1 = SyncMessage(
        message_id="id",
        message_type="hello",
        sender_device_id=protocol.device_id,
        timestamp=1.0,
        payload={"a": 1, "b": 2},
    )
    msg2 = SyncMessage(
        message_id="id",
        message_type="hello",
        sender_device_id=protocol.device_id,
        timestamp=1.0,
        payload={"b": 2, "a": 1},  # different key order
    )
    signed1 = protocol.sign_message(msg1)
    signed2 = protocol.sign_message(msg2)
    assert signed1.hmac == signed2.hmac


def test_shared_secret_without_constructor_check(shared_secret: bytes) -> None:
    """Protocol with valid secret should work."""
    proto = SecureSyncProtocol("d", shared_secret)
    assert proto.device_id == "d"


def test_empty_payload_signing(protocol: SecureSyncProtocol) -> None:
    """Messages with empty payload should sign and verify correctly."""
    msg = protocol._base_message("ack", "test")
    signed = protocol.sign_message(msg)
    assert protocol.verify_message(signed)

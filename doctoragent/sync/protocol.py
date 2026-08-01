"""End-to-end encrypted sync protocol for DoctorAgent.

Implements zero-trust multi-device sync: only ciphertext is transferred,
every message is independently signed, and the transport layer is encrypted.

Protocol layers:
  1. Message layer: JSON-serialised SyncMessage with HMAC-SHA256 signing
  2. Transport layer: AES-256-GCM encryption of serialised messages
  3. State layer:  per-device sync state tracking (SyncState)
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Message types (wire protocol – kept as plain strings for JSON compatibility)
# ---------------------------------------------------------------------------
MSG_HELLO = "hello"
MSG_SYNC_REQUEST = "sync_request"
MSG_SYNC_RESPONSE = "sync_response"
MSG_SYNC_PUSH = "sync_push"
MSG_ACK = "ack"
MSG_ERROR = "error"

NONCE_LEN = 12  # bytes – standard for AES-GCM

# Replay-protection defaults.  Messages whose timestamp drifts further than
# ``REPLAY_MAX_SKEW`` seconds from the local clock are rejected, and every
# observed ``message_id`` is remembered (up to ``REPLAY_WINDOW_SIZE``) so a
# retransmitted message cannot be processed twice.
REPLAY_MAX_SKEW = 300.0  # seconds – tolerance for clock skew between peers
REPLAY_WINDOW_SIZE = 10_000  # max message_ids retained per protocol instance


# ── Message format ───────────────────────────────────────────────────────────


@dataclass
class SyncMessage:
    """Protocol message envelope.

    Every message is independently signed with HMAC-SHA256 using a
    per-device-pair shared secret.  The ``hmac`` field covers every other
    field in canonical order so tampering is detected before the payload
    is inspected.
    """

    version: int = 1
    message_id: str = ""
    message_type: str = ""
    sender_device_id: str = ""
    sender_device_name: str = ""
    payload: dict[str, object] = field(default_factory=dict)
    hmac: str = ""
    timestamp: float = 0.0


# ── File index (snapshot) ────────────────────────────────────────────────────


@dataclass
class FileIndex:
    """Snapshot of all file metadata in the vault."""

    files: dict[str, dict[str, object]] = field(default_factory=dict)
    snapshot_time: float = 0.0
    device_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "snapshot_time": self.snapshot_time,
            "device_id": self.device_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileIndex":
        return cls(
            files=data.get("files", {}),
            snapshot_time=data.get("snapshot_time", 0.0),
            device_id=data.get("device_id", ""),
        )


# ── Sync state ───────────────────────────────────────────────────────────────


@dataclass
class SyncState:
    """Per-device-pair sync state.

    Tracks which files are already known (by content hash) and when each
    device was last seen, enabling efficient incremental sync.
    """

    last_sync_time: dict[str, float] = field(default_factory=dict)
    known_files: dict[str, str] = field(default_factory=dict)
    pending_changes: list[dict[str, Any]] = field(default_factory=list)


# ── Protocol implementation ──────────────────────────────────────────────────


class ReplayWindow:
    """Track recently-seen ``message_id`` values to defeat replay attacks.

    A message is considered fresh only when its timestamp is within
    :data:`REPLAY_MAX_SKEW` seconds of the local clock *and* its
    ``message_id`` has not been observed before.  Seen IDs are retained in
    insertion order and evicted once the window exceeds
    :data:`REPLAY_WINDOW_SIZE`, bounding memory usage.
    """

    def __init__(
        self,
        max_skew: float = REPLAY_MAX_SKEW,
        max_size: int = REPLAY_WINDOW_SIZE,
    ) -> None:
        self._max_skew = max_skew
        self._max_size = max_size
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def check(self, msg: "SyncMessage") -> bool:
        """Return ``True`` if *msg* is fresh (not a replay).

        Fresh messages have their ``message_id`` recorded so a subsequent
        retransmission is rejected.
        """
        now = time.time()
        if msg.timestamp and abs(now - msg.timestamp) > self._max_skew:
            return False
        message_id = msg.message_id
        if not message_id:
            return False
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._order.append(message_id)
        while len(self._seen) > self._max_size:
            old = self._order.popleft()
            self._seen.discard(old)
        return True


class SecureSyncProtocol:
    """Secure multi-device sync protocol with end-to-end encryption.

    Each device pair shares a secret established during device pairing
    (see :class:`doctoragent.sync.auth.DeviceAuth`).  The secret is used
    for both message signing (HMAC-SHA256) and transport encryption
    (AES-256-GCM).

    Parameters
    ----------
    device_id:
        Unique identifier for the local device (e.g. a UUID).
    shared_secret:
        32-byte secret shared with a specific peer device.
    """

    def __init__(self, device_id: str, shared_secret: bytes) -> None:
        if len(shared_secret) != 32:
            raise ValueError("shared_secret must be exactly 32 bytes")
        self.device_id = device_id
        self.shared_secret = shared_secret
        # Per-instance replay protection.  A fresh protocol object (e.g. one
        # built per peer session) starts with an empty window.
        self._replay = ReplayWindow()

    # ------------------------------------------------------------------
    # Message creation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _new_message_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _now() -> float:
        return time.time()

    def _canonical_fields(self, msg: SyncMessage) -> dict[str, Any]:
        """Return message fields in a deterministic order *without* the hmac."""
        return {
            "version": msg.version,
            "message_id": msg.message_id,
            "message_type": msg.message_type,
            "sender_device_id": msg.sender_device_id,
            "sender_device_name": msg.sender_device_name,
            "payload": msg.payload,
            "timestamp": msg.timestamp,
        }

    def _canonical_bytes(self, msg: SyncMessage) -> bytes:
        """Deterministic JSON encoding of the canonical (hmac-free) fields."""
        return json.dumps(
            self._canonical_fields(msg), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    # ------------------------------------------------------------------
    # Signing & verification
    # ------------------------------------------------------------------

    def sign_message(self, msg: SyncMessage) -> SyncMessage:
        """Compute and attach an HMAC-SHA256 signature to *msg*.

        The signature covers the canonical JSON representation of every
        field *except* ``hmac`` itself.
        """
        msg.hmac = hmac.new(
            self.shared_secret, self._canonical_bytes(msg), hashlib.sha256
        ).hexdigest()
        return msg

    def verify_message(self, msg: SyncMessage) -> bool:
        """Return ``True`` if the attached HMAC matches the message content.

        Uses ``hmac.compare_digest`` to resist timing side-channels.  The
        verification is performed over a copy so the caller's message object
        (including its ``hmac`` field) is never mutated, which makes the check
        safe to perform concurrently or on shared/cached messages.
        """
        received = msg.hmac
        if not received:
            return False
        # ``_canonical_fields`` already excludes the ``hmac`` field, so we can
        # recompute the expected MAC directly without touching *msg*.
        expected = hmac.new(
            self.shared_secret, self._canonical_bytes(msg), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, received)

    def check_replay(self, msg: SyncMessage) -> bool:
        """Return ``True`` if *msg* is fresh and record its ``message_id``.

        Combines a timestamp-skew check with a per-instance ``message_id``
        dedup set so a captured message cannot be replayed within the
        :data:`REPLAY_MAX_SKEW` window.  Should be called *after*
        :meth:`verify_message` succeeds so only authenticated messages
        populate the window.
        """
        return self._replay.check(msg)

    # ------------------------------------------------------------------
    # Factory methods for each message type
    # ------------------------------------------------------------------

    def _base_message(self, msg_type: str, sender_name: str) -> SyncMessage:
        return SyncMessage(
            version=1,
            message_id=self._new_message_id(),
            message_type=msg_type,
            sender_device_id=self.device_id,
            sender_device_name=sender_name,
            timestamp=self._now(),
        )

    def create_hello(self, sender_name: str = "") -> SyncMessage:
        """Create a ``hello`` announcement message."""
        msg = self._base_message(MSG_HELLO, sender_name)
        msg.payload = {"capabilities": ["sync_v1"], "protocol_version": 1}
        return self.sign_message(msg)

    def create_sync_request(
        self, peer_device_id: str, since: float = 0.0, sender_name: str = ""
    ) -> SyncMessage:
        """Request changed files since *since* (Unix timestamp)."""
        msg = self._base_message(MSG_SYNC_REQUEST, sender_name)
        msg.payload = {"target_device_id": peer_device_id, "since": since}
        return self.sign_message(msg)

    def create_sync_response(
        self, files: list[dict[str, Any]], sender_name: str = ""
    ) -> SyncMessage:
        """Respond with a list of file metadata entries.

        Each entry in *files* should be a dict with keys such as
        ``path``, ``hash``, ``mtime``, ``size``.
        """
        msg = self._base_message(MSG_SYNC_RESPONSE, sender_name)
        msg.payload = {"files": files}
        return self.sign_message(msg)

    def create_sync_push(self, file_meta: dict[str, Any], sender_name: str = "") -> SyncMessage:
        """Push a single file metadata change to a peer."""
        msg = self._base_message(MSG_SYNC_PUSH, sender_name)
        msg.payload = {"file": file_meta}
        return self.sign_message(msg)

    def create_ack(self, ack_message_id: str, sender_name: str = "") -> SyncMessage:
        """Acknowledge receipt of a message."""
        msg = self._base_message(MSG_ACK, sender_name)
        msg.payload = {"ack_message_id": ack_message_id}
        return self.sign_message(msg)

    def create_error(self, reason: str, sender_name: str = "") -> SyncMessage:
        """Create a signed ``error`` message describing a protocol failure."""
        msg = self._base_message(MSG_ERROR, sender_name)
        msg.payload = {"error": reason}
        return self.sign_message(msg)

    # ------------------------------------------------------------------
    # Serialisation (pack / unpack)
    # ------------------------------------------------------------------

    @staticmethod
    def pack(msg: SyncMessage) -> bytes:
        """Serialize *msg* to JSON bytes."""
        data = {
            "version": msg.version,
            "message_id": msg.message_id,
            "message_type": msg.message_type,
            "sender_device_id": msg.sender_device_id,
            "sender_device_name": msg.sender_device_name,
            "payload": msg.payload,
            "hmac": msg.hmac,
            "timestamp": msg.timestamp,
        }
        return json.dumps(data, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def unpack(data: bytes) -> SyncMessage:
        """Deserialize JSON bytes into a :class:`SyncMessage`."""
        obj = json.loads(data.decode("utf-8"))
        return SyncMessage(
            version=obj.get("version", 1),
            message_id=obj.get("message_id", ""),
            message_type=obj.get("message_type", ""),
            sender_device_id=obj.get("sender_device_id", ""),
            sender_device_name=obj.get("sender_device_name", ""),
            payload=obj.get("payload", {}),
            hmac=obj.get("hmac", ""),
            timestamp=obj.get("timestamp", 0.0),
        )

    # ------------------------------------------------------------------
    # Transport encryption (AES-256-GCM)
    # ------------------------------------------------------------------

    def encrypt_transport(self, plaintext: bytes, key: bytes) -> bytes:
        """Encrypt *plaintext* with AES-256-GCM.

        Returns ``nonce || ciphertext`` where *nonce* is 12 random bytes.
        The GCM authentication tag is appended to the ciphertext by the
        ``cryptography`` library.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(NONCE_LEN)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
        return nonce + ciphertext

    def decrypt_transport(self, ciphertext: bytes, key: bytes) -> bytes:
        """Decrypt *ciphertext* produced by :meth:`encrypt_transport`.

        Raises ``InvalidTag`` if the data has been tampered with.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = ciphertext[:NONCE_LEN]
        data = ciphertext[NONCE_LEN:]
        return AESGCM(key).decrypt(nonce, data, None)

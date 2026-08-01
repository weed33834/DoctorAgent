"""Device authorization and revocation management for DoctorAgent sync.

Manages device pairing, shared-secret key exchange, and revocation.
Implements a simplified PAKE-style protocol:

1. Device A generates a 6-digit pairing code (5-minute expiry) and an
   ephemeral X25519 key pair.  The code is shown to the user.
2. The user enters the code manually on Device B.
3. Device B generates its own ephemeral X25519 key pair and sends its
   public key to Device A (along with the pairing code for verification).
4. Both devices derive a 32-byte shared secret via HKDF-SHA256:
   ``HKDF(ikm=DH_shared, salt=pairing_code, info=b"doctoragent-pairing-v1")``
5. Each device stores the shared secret encrypted at rest (via
   ``secure_storage.seal``).

Design notes
------------
- Every pairing session uses fresh ephemeral keys → forward secrecy.
- The pairing code acts as HKDF salt, binding the resulting key to the
  out-of-band code exchange.
- Shared secrets are sealed with :func:`doctoragent.connections.secure_storage.seal`
  before persisting to `config_dir / "devices.json"`.
- Max 5 paired devices (``MAX_PAIRED_DEVICES``).
"""

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

logger = logging.getLogger(__name__)

# ``fcntl`` provides advisory file locking for cross-process safety on POSIX.
# It is unavailable on Windows; there the in-process RLock still serialises
# concurrent access from threads within a single interpreter.
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover – non-POSIX platform
    _fcntl = None  # type: ignore[assignment]

# ── Constants ────────────────────────────────────────────────────────────────

PAIRING_CODE_DIGITS = 6
PAIRING_CODE_TTL = 300  # seconds (5 minutes)
MAX_PAIRED_DEVICES = 5
HKDF_INFO_PAIRING = b"doctoragent-pairing-v1"
HKDF_LENGTH = 32

# ── Type aliases (cryptography is a required dependency) ─────────────────────

_X25519PrivateKeyType: TypeAlias = X25519PrivateKey
_X25519PublicKeyType: TypeAlias = X25519PublicKey


# ── Ephemeral pairing session (in-memory only) ───────────────────────────────


@dataclass
class PairingSession:
    """In-memory state for an active pairing session."""

    code: str
    expires_at: float
    private_key: "_X25519PrivateKeyType"
    public_key: "_X25519PublicKeyType"


# ── Persisted device record ──────────────────────────────────────────────────


@dataclass
class DeviceRecord:
    """Persisted record of an authorized peer device."""

    device_id: str
    device_name: str
    created_at: float = 0.0
    last_seen: float = 0.0
    sealed_secret: str = ""  # sealed via secure_storage.seal


# ── Device auth manager ──────────────────────────────────────────────────────


class DeviceAuth:
    """Manage device pairing, authorization, and revocation.

    Parameters
    ----------
    config_dir:
        Directory where ``devices.json`` is stored.  Created if missing.
    """

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._devices_path = config_dir / "devices.json"
        self._devices: dict[str, DeviceRecord] = {}
        self._pending_pairing: PairingSession | None = None
        # Reentrant lock guards all in-memory state (``_devices``,
        # ``_pending_pairing``) so concurrent callers from multiple threads
        # cannot corrupt each other's pairing / revocation operations.
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_pairing_code(self) -> str:
        """Generate a 6-digit numeric pairing code valid for 5 minutes.

        Also creates an ephemeral X25519 key pair for the pending session.
        The public key can be retrieved via :meth:`get_pending_public_key`
        for transmission to the peer device.
        """
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        with self._lock:
            code = format(secrets.randbelow(10**PAIRING_CODE_DIGITS), f"0{PAIRING_CODE_DIGITS}d")
            private = X25519PrivateKey.generate()
            public = private.public_key()

            self._pending_pairing = PairingSession(
                code=code,
                expires_at=time.time() + PAIRING_CODE_TTL,
                private_key=private,
                public_key=public,
            )
            logger.info("Pairing code generated (valid for %ds)", PAIRING_CODE_TTL)
            return code

    def pair_device(self, device_name: str, pairing_code: str, peer_public_key_bytes: bytes) -> str:
        """Complete device pairing as the initiator (code generator).

        Verifies the pairing code, performs X25519 key exchange with the
        peer's public key, and stores the resulting shared secret.

        Parameters
        ----------
        device_name:
            Human-readable name for the paired device.
        pairing_code:
            The 6-digit code the peer entered (must match the pending session).
        peer_public_key_bytes:
            Raw 32-byte X25519 public key from the peer device.

        Returns
        -------
        A new *device_id* (32 hex chars) for the paired device.

        Raises
        ------
        RuntimeError:
            If no pending pairing session exists, the code is wrong, expired,
            or the maximum device count is reached.
        """
        with self._lock:
            if self._pending_pairing is None:
                raise RuntimeError("No pending pairing session; call generate_pairing_code() first")
            if time.time() > self._pending_pairing.expires_at:
                self._pending_pairing = None
                raise RuntimeError("Pairing code expired")
            if not pairing_code or not secrets.compare_digest(
                pairing_code.encode(), self._pending_pairing.code.encode()
            ):
                raise RuntimeError("Pairing code mismatch")

            if len(self._devices) >= MAX_PAIRED_DEVICES:
                raise RuntimeError(f"Maximum paired devices ({MAX_PAIRED_DEVICES}) reached")

            our_private = self._pending_pairing.private_key
            peer_public = _x25519_public_from_bytes(peer_public_key_bytes)

            shared_secret = _derive_shared_secret(our_private, peer_public, pairing_code)
            device_id = str(secrets.token_hex(16))

            record = self._make_record(device_id, device_name, shared_secret)
            self._devices[device_id] = record
            self._pending_pairing = None
            self._save()

            logger.info("Device paired: %s (%s)", device_name, device_id)
            return device_id

    def accept_pairing(
        self,
        pairing_code: str,
        initiator_public_key_bytes: bytes,
        device_name: str,
    ) -> tuple[str, bytes]:
        """Complete device pairing as the responder (code receiver).

        Generates our own ephemeral X25519 key pair, derives the shared secret,
        and returns the new device ID and our public key (for the initiator).

        Parameters
        ----------
        pairing_code:
            The 6-digit code displayed on the initiator's screen.
        initiator_public_key_bytes:
            Raw 32-byte X25519 public key from the initiator.
        device_name:
            Human-readable name for this device.

        Returns
        -------
        A tuple of ``(device_id, our_public_key_bytes)``.

        Raises
        ------
        RuntimeError:
            If the maximum device count is reached.
        """
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        with self._lock:
            if len(self._devices) >= MAX_PAIRED_DEVICES:
                raise RuntimeError(f"Maximum paired devices ({MAX_PAIRED_DEVICES}) reached")

            our_private = X25519PrivateKey.generate()
            our_public = our_private.public_key()
            peer_public = _x25519_public_from_bytes(initiator_public_key_bytes)

            shared_secret = _derive_shared_secret(our_private, peer_public, pairing_code)
            device_id = str(secrets.token_hex(16))

            record = self._make_record(device_id, device_name, shared_secret)
            self._devices[device_id] = record
            self._save()

            our_public_bytes = _x25519_public_to_bytes(our_public)
            logger.info("Device paired (responder): %s (%s)", device_name, device_id)
            return device_id, our_public_bytes

    def get_pending_public_key(self) -> bytes | None:
        """Return the pending session's X25519 public key bytes, or ``None``.

        Expired sessions are automatically cleared.
        """
        with self._lock:
            if self._pending_pairing is None:
                return None
            if time.time() > self._pending_pairing.expires_at:
                self._pending_pairing = None
                return None
            return _x25519_public_to_bytes(self._pending_pairing.public_key)

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    def is_authorized(self, device_id: str) -> bool:
        """Return ``True`` if *device_id* is authorized."""
        with self._lock:
            return device_id in self._devices

    def revoke_device(self, device_id: str) -> None:
        """Revoke authorization for *device_id*.

        Raises ``KeyError`` if the device is not found.
        """
        with self._lock:
            if device_id not in self._devices:
                raise KeyError(f"Device not found: {device_id}")
            name = self._devices[device_id].device_name
            del self._devices[device_id]
            self._save()
            logger.info("Device revoked: %s (%s)", name, device_id)

    def list_authorized_devices(self) -> list[dict[str, Any]]:
        """Return metadata for all authorized devices."""
        with self._lock:
            return [
                {
                    "device_id": r.device_id,
                    "device_name": r.device_name,
                    "created_at": r.created_at,
                    "last_seen": r.last_seen,
                }
                for r in self._devices.values()
            ]

    def get_shared_secret(self, device_id: str) -> bytes | None:
        """Return the unsealed shared secret for *device_id*, or ``None``."""
        with self._lock:
            record = self._devices.get(device_id)
            if record is None or not record.sealed_secret:
                return None
            sealed = record.sealed_secret

        # Unsealing may touch the OS keychain / DPAPI and is not part of the
        # critical section that guards ``_devices``.
        from doctoragent.connections.secure_storage import unseal

        hex_secret = unseal(sealed)
        return bytes.fromhex(hex_secret)

    def touch_device(self, device_id: str) -> None:
        """Update the last-seen timestamp for *device_id*."""
        with self._lock:
            record = self._devices.get(device_id)
            if record:
                record.last_seen = time.time()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _make_record(self, device_id: str, device_name: str, shared_secret: bytes) -> DeviceRecord:
        from doctoragent.connections.secure_storage import seal

        return DeviceRecord(
            device_id=device_id,
            device_name=device_name,
            created_at=time.time(),
            last_seen=time.time(),
            sealed_secret=seal(shared_secret.hex()),
        )

    def _load(self) -> None:
        """Load device records from disk."""
        if not self._devices_path.exists():
            return
        try:
            raw = json.loads(self._devices_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # The persisted file is corrupt/unreadable.  Move it aside so the
            # next save starts from a clean slate and operators can inspect
            # the offending file rather than silently discarding it.
            self._backup_corrupt()
            logger.warning("Corrupt devices.json backed up; starting with empty device list")
            return
        try:
            for entry in raw:
                record = DeviceRecord(
                    device_id=entry["device_id"],
                    device_name=entry["device_name"],
                    created_at=entry.get("created_at", 0.0),
                    last_seen=entry.get("last_seen", 0.0),
                    sealed_secret=entry.get("sealed_secret", ""),
                )
                self._devices[record.device_id] = record
        except (KeyError, TypeError):
            self._backup_corrupt()
            logger.warning("Malformed devices.json backed up; starting with empty device list")

    def _backup_corrupt(self) -> None:
        """Rename the current ``devices.json`` to ``.corrupt-<ns>`` for review."""
        try:
            backup = self._devices_path.parent / (
                f"{self._devices_path.name}.corrupt-{time.time_ns()}"
            )
            self._devices_path.rename(backup)
        except OSError:
            logger.debug("Could not back up corrupt devices.json", exc_info=True)

    def _save(self) -> None:
        """Persist device records to disk atomically.

        Merges in-memory records with any existing on-disk records so that
        multiple :class:`DeviceAuth` instances sharing the same config file
        do not silently clobber each other.  An exclusive ``fcntl.flock`` on
        the data file serialises writers across processes, and the file is
        created with ``0600`` permissions so sealed secrets are not
        world-readable.
        """
        # Take the in-process lock first so we don't fight ourselves.
        with self._lock:
            path = self._devices_path
            lock_fd: int | None = None
            if _fcntl is not None:
                try:
                    lock_fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
                    _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
                except OSError:
                    # Fall back to an unlocked write if the lock file cannot
                    # be opened (e.g. read-only filesystem); better to keep
                    # in-memory state consistent than to crash the caller.
                    lock_fd = None

            try:
                merged: dict[str, dict[str, Any]] = {}
                if path.exists():
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        for entry in raw:
                            merged[entry["device_id"]] = entry
                    except (json.JSONDecodeError, KeyError, OSError, TypeError):
                        pass

                for r in self._devices.values():
                    merged[r.device_id] = {
                        "device_id": r.device_id,
                        "device_name": r.device_name,
                        "created_at": r.created_at,
                        "last_seen": r.last_seen,
                        "sealed_secret": r.sealed_secret,
                    }

                data = sorted(merged.values(), key=lambda d: d["device_id"])
                content = json.dumps(data, indent=2)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(content, encoding="utf-8")
                try:
                    os.chmod(str(tmp), 0o600)
                except OSError:
                    pass
                tmp.replace(path)
                try:
                    os.chmod(str(path), 0o600)
                except OSError:
                    pass
            finally:
                if lock_fd is not None:
                    try:
                        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)


# ── Crypto helpers ───────────────────────────────────────────────────────────


def _derive_shared_secret(
    our_private: "_X25519PrivateKeyType",
    peer_public: "_X25519PublicKeyType",
    pairing_code: str,
) -> bytes:
    """Derive a 32-byte shared secret from DH exchange + pairing code.

    Uses HKDF-SHA256 with the X25519 shared key as IKM and the pairing
    code as salt.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    shared_key = our_private.exchange(peer_public)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=HKDF_LENGTH,
        salt=pairing_code.encode("utf-8"),
        info=HKDF_INFO_PAIRING,
    )
    return hkdf.derive(shared_key)


def _x25519_public_to_bytes(key: "_X25519PublicKeyType") -> bytes:
    """Serialize X25519 public key to 32 raw bytes."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _x25519_public_from_bytes(raw: bytes) -> "_X25519PublicKeyType":
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    return X25519PublicKey.from_public_bytes(raw)

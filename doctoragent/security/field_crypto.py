"""Field-level encryption for structured metadata.

The whole-file encryption in :mod:`doctoragent.security.crypto` protects file
*bodies* but leaves structured metadata (summary, tags, classifier output)
exposed in the SQLite index.  :class:`FieldEncryptor` wraps individual field
values with AES-256-GCM so that each field gets its own IV and authentication
tag.  Per-field keys are derived from the master key with HKDF-SHA256 using a
distinct ``info`` per field name, so compromising one field's ciphertext never
reveals another field's key.

The encrypted field value is ``base64(iv(12) + ciphertext + tag(16))`` — a
single opaque string that can be stored transparently in any TEXT column of
the task store.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Wire-format constants.
_IV_LEN = 12
_TAG_LEN = 16  # GCM tag length, implied by the ciphertext returned by AESGCM.
_KEY_LEN = 32
# Prefix that marks a stored string as a field-encryption envelope.  Keeping a
# prefix lets ``decrypt_field`` distinguish an encrypted envelope from a plain
# string and simply return the latter unchanged, so callers can mix encrypted
# and unencrypted rows during a rollout.
_ENVELOPE_PREFIX = "FENC1:"

# Fields eligible for field-level encryption.  Anything outside this set is
# left as-is by ``encrypt_dict`` so unrelated columns are never disturbed.
DEFAULT_ENCRYPTED_FIELDS: frozenset[str] = frozenset({"metadata", "summary", "tags"})


class FieldEncryptionError(Exception):
    """Raised when field-level encryption or decryption fails."""


def _derive_field_key(master_key: bytes, field_name: str) -> bytes:
    """Derive a 32-byte per-field key from *master_key* via HKDF-SHA256.

    Each field name becomes part of the HKDF ``info`` so two fields never
    share a key even when derived from the same master key.
    """
    info = b"doctoragent-field-key-v1:" + field_name.encode("utf-8")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=None,
        info=info,
    )
    return hkdf.derive(master_key)


def _is_envelope(value: str) -> bool:
    """Return True if *value* looks like a field-encryption envelope."""
    return isinstance(value, str) and value.startswith(_ENVELOPE_PREFIX)


class FieldEncryptor:
    """Encrypt and decrypt individual structured fields.

    Parameters
    ----------
    master_key:
        The 32-byte master key from which per-field keys are derived.
    encrypted_fields:
        Optional override of the set of field names eligible for
        ``encrypt_dict`` / ``decrypt_dict``.  Defaults to
        :data:`DEFAULT_ENCRYPTED_FIELDS`.
    """

    def __init__(
        self,
        master_key: bytes,
        encrypted_fields: Mapping[str, bool] | frozenset[str] | None = None,
    ) -> None:
        if master_key is None or len(master_key) != _KEY_LEN:
            raise ValueError("master_key must be 32 bytes for AES-256-GCM")
        self._master_key = master_key
        self._key_cache: dict[str, bytes] = {}
        if encrypted_fields is None:
            self._encrypted_fields: frozenset[str] = DEFAULT_ENCRYPTED_FIELDS
        else:
            self._encrypted_fields = self._normalize_field_set(encrypted_fields)

    @staticmethod
    def _normalize_field_set(
        spec: Mapping[str, bool] | frozenset[str] | set[str],
    ) -> frozenset[str]:
        if isinstance(spec, frozenset | set):
            return frozenset(spec)
        # Mapping[str, bool]: treat True values as enabled fields.
        return frozenset(name for name, enabled in spec.items() if enabled)

    # ── per-field key management ──────────────────────────────────────────

    def _field_key(self, field_name: str) -> bytes:
        """Return the per-field key, caching it for the lifetime of the encryptor."""
        key = self._key_cache.get(field_name)
        if key is None:
            key = _derive_field_key(self._master_key, field_name)
            self._key_cache[field_name] = key
        return key

    @property
    def encrypted_fields(self) -> frozenset[str]:
        """Return the set of field names this encryptor will process."""
        return self._encrypted_fields

    # ── single-field API ──────────────────────────────────────────────────

    def encrypt_field(self, name: str, value: Any) -> str:
        """Encrypt a single field value and return the envelope string.

        ``value`` is JSON-serialised before encryption so any JSON-compatible
        Python object (str, list, dict, …) can be stored transparently.
        ``None`` is returned unchanged — storing ``None`` for a missing field
        is both cheaper and avoids turning a missing value into an envelope.
        """
        if value is None:
            return None  # type: ignore[return-value]
        import json

        plaintext = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        iv = os.urandom(_IV_LEN)
        aesgcm = AESGCM(self._field_key(name))
        # AESGCM.encrypt returns ciphertext || tag.
        ciphertext = aesgcm.encrypt(iv, plaintext, None)
        envelope = _ENVELOPE_PREFIX + base64.b64encode(iv + ciphertext).decode("ascii")
        return envelope

    def decrypt_field(self, name: str, ciphertext: str) -> Any:
        """Decrypt a single envelope back to its original Python value.

        Non-envelope strings are returned unchanged so callers can operate on
        a mix of encrypted and legacy plaintext columns.
        """
        if ciphertext is None:
            return None
        if not isinstance(ciphertext, str):
            # Already a structured value (e.g. a list decoded elsewhere).
            return ciphertext
        if not _is_envelope(ciphertext):
            return ciphertext
        import json

        raw_b64 = ciphertext[len(_ENVELOPE_PREFIX) :]
        try:
            blob = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FieldEncryptionError("Invalid field envelope: bad base64") from exc
        if len(blob) < _IV_LEN + _TAG_LEN:
            raise FieldEncryptionError("Invalid field envelope: too short")
        iv = blob[:_IV_LEN]
        body = blob[_IV_LEN:]
        aesgcm = AESGCM(self._field_key(name))
        try:
            plaintext = aesgcm.decrypt(iv, body, None)
        except Exception as exc:  # noqa: BLE001 — surface any GCM failure uniformly
            raise FieldEncryptionError(
                f"Field decryption failed for '{name}' (wrong key or tampered data)"
            ) from exc
        return json.loads(plaintext.decode("utf-8"))

    # ── dict API ──────────────────────────────────────────────────────────

    def encrypt_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *data* with eligible fields encrypted in place.

        Only fields listed in :attr:`encrypted_fields` are touched; everything
        else is passed through unchanged.  A value that is already an envelope
        is not re-encrypted (idempotency).
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            if key in self._encrypted_fields and value is not None:
                if isinstance(value, str) and _is_envelope(value):
                    result[key] = value
                else:
                    result[key] = self.encrypt_field(key, value)
            else:
                result[key] = value
        return result

    def decrypt_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *data* with eligible fields decrypted in place.

        Fields that are not envelopes (legacy plaintext, ``None``) are left
        untouched so the method is safe to run on mixed data.
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            if key in self._encrypted_fields and isinstance(value, str) and _is_envelope(value):
                result[key] = self.decrypt_field(key, value)
            else:
                result[key] = value
        return result

    # ── introspection ─────────────────────────────────────────────────────

    def is_encrypted(self, field_name: str, value: Any) -> bool:
        """Return True if *value* is an envelope produced by this encryptor."""
        return (
            field_name in self._encrypted_fields and isinstance(value, str) and _is_envelope(value)
        )

    def clear_key_cache(self) -> None:
        """Drop cached per-field keys.  Keys are re-derived on next use."""
        self._key_cache.clear()


__all__ = [
    "DEFAULT_ENCRYPTED_FIELDS",
    "FieldEncryptor",
    "FieldEncryptionError",
]

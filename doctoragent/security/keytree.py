"""Three-tier key hierarchy for DoctorAgent."""

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Key lengths (bytes) for derived keys.
VAULT_KEY_LEN = 32
FILE_KEY_LEN = 32
AUDIT_KEY_LEN = 32


def derive_audit_key(master_key: bytes) -> bytes:
    """Derive the audit-log HMAC key from the Master Key via HKDF-SHA256.

    Deriving instead of storing a standalone key file next to the audit log
    means an attacker who captures the disk can no longer re-sign a tampered
    chain unless they also hold the master key (which lives behind
    Argon2id/DPAPI/TPM — see ``security/master_key.py``). Key separation is
    provided by the HKDF ``info`` label, so the audit key never equals the
    vault or file keys even though they share the same master secret.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=AUDIT_KEY_LEN,
        salt=None,
        info=b"doctoragent-audit-hmac-v1",
    )
    return hkdf.derive(master_key)


def derive_vault_key(master_key: bytes, info: bytes = b"vault-key-v1") -> bytes:
    """Derive Vault Key from Master Key using HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=VAULT_KEY_LEN,
        salt=None,
        info=info,
    )
    return hkdf.derive(master_key)


def derive_file_key(vault_key: bytes, salt: bytes) -> bytes:
    """Derive File Key from Vault Key using HKDF-SHA256.

    The Vault Key is already a high-entropy 32-byte secret, so an expensive
    memory-hard KDF (Argon2id) is unnecessary here.  HKDF-SHA256 with the
    per-file *salt* provides the same key-separation guarantees at a fraction
    of the cost.  The *salt* parameter is retained for API compatibility.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=FILE_KEY_LEN,
        salt=salt,
        info=b"doctoragent-file-key-v1",
    )
    return hkdf.derive(vault_key)


def generate_salt() -> bytes:
    """Generate a 32-byte random salt."""
    return os.urandom(32)

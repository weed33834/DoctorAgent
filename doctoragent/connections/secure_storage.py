"""Field-level encryption for sensitive connection fields.

On Windows 11 uses DPAPI (user-bound).
On other platforms uses AES-256-GCM with a persistent key file stored under
``~/.config/doctoragent/.storage_key`` (or ``DOCTORAGENT_STORAGE_KEY_FILE``).
The key file is created with owner-only permissions (0o600) and its
permissions are verified on load.
"""

import base64
import contextlib
import functools
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

_logger = logging.getLogger(__name__)

_KEY_ENV_VAR = "DOCTORAGENT_STORAGE_KEY_FILE"
# Sensitive header names that should be sealed even inside custom_headers.
_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "x-api-key",
        "apikey",
        "cookie",
        "set-cookie",
        "x-auth-token",
        "proxy-authorization",
        "x-secret",
        "x-token",
    }
)


# Module-level key cache to avoid re-reading the key file on every seal/unseal.
def _default_key_path() -> Path:
    """Return the default storage key path (lazy to avoid import-time issues)."""
    return Path.home() / ".config" / "doctoragent" / ".storage_key"


def _is_windows() -> bool:
    """Return True if running on Windows."""
    return sys.platform == "win32"


def _key_file_path() -> Path:
    """Return the path to the fallback storage key file."""
    if _KEY_ENV_VAR in os.environ:
        return Path(os.environ[_KEY_ENV_VAR]).expanduser()
    return _default_key_path()


@functools.cache
def _load_or_create_fallback_key() -> bytes:
    """Return a 32-byte AES key, generating it if necessary.

    The key file is created with 0o600 permissions. If an existing file has
    overly permissive permissions, an error is raised. Memoised via
    :func:`functools.cache` so the key file is read once per process.
    """
    key_path = _key_file_path()
    try:
        data = key_path.read_bytes()
    except FileNotFoundError:
        pass
    else:
        # Use lstat to detect symlinks (stat follows them).
        lst = os.lstat(key_path)
        if not stat.S_ISREG(lst.st_mode):
            raise RuntimeError(
                f"Storage key file {key_path} is not a regular file (possible symlink attack)"
            )
        mode = lst.st_mode
        if mode & stat.S_IRWXG or mode & stat.S_IRWXO:
            raise RuntimeError(
                f"Storage key file {key_path} has overly permissive permissions "
                f"({oct(stat.S_IMODE(mode))}); restrict it to owner-only access and retry."
            )
        return data

    key = os.urandom(32)
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(
        key_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return key


def _fallback_seal(value: str, aad: bytes | None = None) -> str:
    """Seal *value* using AES-256-GCM with the persistent fallback key."""
    key = _load_or_create_fallback_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)
    blob = nonce + ciphertext
    return f"aes:{base64.b64encode(blob).decode('ascii')}"


def _fallback_unseal(value: str, aad: bytes | None = None) -> str:
    """Unseal an AES-256-GCM encrypted value."""
    from cryptography.exceptions import InvalidTag

    key = _load_or_create_fallback_key()
    raw = base64.b64decode(value[4:].encode("ascii"))
    nonce, ciphertext = raw[:12], raw[12:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        _logger.critical(
            "AES-GCM authentication tag verification failed — possible tampering detected"
        )
        raise
    return plaintext.decode("utf-8")


def seal(value: str, field_name: str | None = None) -> str:
    """Seal a sensitive string.

    On Windows returns base64(DPAPI(plaintext)) prefixed with 'dpapi:'.
    On other platforms returns AES-256-GCM ciphertext prefixed with 'aes:'.
    """
    if not value:
        return value
    aad = field_name.encode("utf-8") if field_name else None
    if not _is_windows():
        return _fallback_seal(value, aad)

    from doctoragent.security.win_helpers import protect_data

    protected = protect_data(value.encode("utf-8"))
    return f"dpapi:{base64.b64encode(protected).decode('ascii')}"


def unseal(value: str, field_name: str | None = None) -> str:
    """Unseal a sensitive string."""
    if not value:
        return value
    aad = field_name.encode("utf-8") if field_name else None
    if value.startswith("aes:"):
        return _fallback_unseal(value, aad)
    if value.startswith("dpapi:"):
        if not _is_windows():
            raise RuntimeError("Cannot unseal DPAPI value on non-Windows platform")
        from doctoragent.security.win_helpers import unprotect_data

        protected = base64.b64decode(value[6:].encode("ascii"))
        return unprotect_data(protected).decode("utf-8")
    # Reject unrecognised values to prevent downgrade attacks.
    raise ValueError(
        "Value has no recognised encryption prefix (aes: or dpapi:). "
        "Refusing to return potentially plaintext data."
    )


def seal_dict(data: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """Seal specified string fields in a dictionary."""
    result: dict[str, Any] = {}
    for key, val in data.items():
        if key in fields:
            # Extract secret value from SecretStr before sealing.
            if isinstance(val, SecretStr):
                val = val.get_secret_value()
            if isinstance(val, str):
                result[key] = seal(val, field_name=key)
            else:
                result[key] = val
        elif key == "custom_headers" and isinstance(val, dict):
            # Seal sensitive headers inside custom_headers.
            sealed_headers: dict[str, str] = {}
            for hk, hv in val.items():
                if isinstance(hv, str) and hk.lower() in _SENSITIVE_HEADER_KEYS:
                    sealed_headers[hk] = seal(hv, field_name=f"custom_headers.{hk}")
                else:
                    sealed_headers[hk] = hv
            result[key] = sealed_headers
        else:
            result[key] = val
    return result


def unseal_dict(data: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """Unseal specified string fields in a dictionary."""
    result: dict[str, Any] = {}
    for key, val in data.items():
        if key in fields:
            if isinstance(val, str):
                unsealed = unseal(val, field_name=key)
                # Wrap back as SecretStr for model validation.
                result[key] = SecretStr(unsealed) if unsealed else SecretStr("")
            else:
                result[key] = val
        elif key == "custom_headers" and isinstance(val, dict):
            # Unseal sensitive headers inside custom_headers.
            unsealed_headers: dict[str, str] = {}
            for hk, hv in val.items():
                if isinstance(hv, str) and hv.startswith(("aes:", "dpapi:")):
                    try:
                        unsealed_headers[hk] = unseal(hv, field_name=f"custom_headers.{hk}")
                    except (ValueError, RuntimeError):
                        unsealed_headers[hk] = hv
                else:
                    unsealed_headers[hk] = hv
            result[key] = unsealed_headers
        else:
            result[key] = val
    return result

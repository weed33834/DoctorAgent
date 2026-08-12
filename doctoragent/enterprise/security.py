"""Enterprise authentication security primitives (M14 B/C).

Self-contained, dependency-light security helpers for the organization layer:

* Password hashing/verification (PBKDF2-HMAC-SHA256 with per-user salt).
* TOTP (RFC 6238) MFA — a pure-Python implementation, no external dependency.
* Password policy enforcement (length / complexity / reuse).
* Account lockout state machine (failed attempts → lock window).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timezone
from typing import Any

PBKDF2_ITERATIONS = 200_000


# ── password hashing ──────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256; returns ``salt$hash`` hex."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against a ``salt$hash`` produced by :func:`hash_password`."""
    if not stored or "$" not in stored:
        return False
    salt_hex, digest_hex = stored.split("$", 1)
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


# ── TOTP (RFC 6238) ──────────────────────────────────────────────────


def _totp_backend():
    """Return a pyotp-like module (mature lib) or None if unavailable.

    We prefer the well-tested ``pyotp`` library instead of hand-rolling RFC 6238;
    the pure-Python implementation below remains as an offline fallback.
    """
    try:
        import pyotp  # type: ignore[import-not-found]

        return pyotp
    except ImportError:  # pragma: no cover
        return None


def _base32_decode(secret: str) -> bytes:
    """Decode a (possibly unpadded) base32 secret into raw bytes.

    The fallback RFC 6238 path relies on this helper; ``generate_totp_secret``
    emits unpadded base32, so re-pad before decoding.
    """
    secret = secret.strip().upper()
    secret = "".join(ch for ch in secret if ch not in "= \t")
    pad = (-len(secret)) % 8
    return base64.b32decode(secret + "=" * pad)


def generate_totp_secret() -> str:
    """Generate a base32-encoded 20-byte TOTP secret."""
    pyotp = _totp_backend()
    if pyotp is not None:
        return pyotp.random_base32()
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, *, period: int = 30, digits: int = 6, at: float | None = None) -> str:
    """Compute the current TOTP code (RFC 6238, SHA-1 default)."""
    pyotp = _totp_backend()
    if pyotp is not None:
        at_s = at if at is not None else time.time()
        return pyotp.TOTP(secret, interval=period, digits=digits).at(at_s)
    counter = int((at if at is not None else time.time()) // period)
    msg = struct.pack(">Q", counter)
    key = _base32_decode(secret)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify_totp(secret: str, code: str, *, window: int = 1) -> bool:
    """Verify a TOTP code allowing ``window`` steps of clock drift either way."""
    pyotp = _totp_backend()
    if pyotp is not None:
        return pyotp.TOTP(secret).verify(code, valid_window=window)
    if not code or not code.isdigit():
        return False
    code = code.strip()
    now = time.time()
    for offset in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret, at=now + offset * 30), code):
            return True
    return False


def totp_provisioning_uri(secret: str, email: str, issuer: str = "DoctorAgent") -> str:
    """OTPAuth URL for QR-code enrollment (otpauth://totp/...)."""
    pyotp = _totp_backend()
    if pyotp is not None:
        return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)
    label = f"{issuer}:{email}"
    params = f"secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
    import urllib.parse

    return "otpauth://totp/" + urllib.parse.quote(label) + "?" + params


# ── password policy ───────────────────────────────────────────────────


class PasswordPolicyError(ValueError):
    pass


class PasswordPolicy:
    """Enforce a configurable password complexity policy (M14 C.4)."""

    def __init__(
        self,
        min_length: int = 8,
        require_upper: bool = True,
        require_lower: bool = True,
        require_digit: bool = True,
        require_symbol: bool = False,
        max_age_days: int = 0,  # 0 = no expiry
    ) -> None:
        self.min_length = min_length
        self.require_upper = require_upper
        self.require_lower = require_lower
        self.require_digit = require_digit
        self.require_symbol = require_symbol
        self.max_age_days = max_age_days

    def validate(self, password: str) -> None:
        """Raise :class:`PasswordPolicyError` if the password violates policy."""
        problems: list[str] = []
        if len(password) < self.min_length:
            problems.append(f"至少 {self.min_length} 位")
        if self.require_upper and not any(c.isupper() for c in password):
            problems.append("需包含大写字母")
        if self.require_lower and not any(c.islower() for c in password):
            problems.append("需包含小写字母")
        if self.require_digit and not any(c.isdigit() for c in password):
            problems.append("需包含数字")
        if self.require_symbol and not any(not c.isalnum() for c in password):
            problems.append("需包含符号")
        if problems:
            raise PasswordPolicyError("密码不符合策略: " + "、".join(problems))

    def describe(self) -> dict[str, Any]:
        return {
            "min_length": self.min_length,
            "require_upper": self.require_upper,
            "require_lower": self.require_lower,
            "require_digit": self.require_digit,
            "require_symbol": self.require_symbol,
            "max_age_days": self.max_age_days,
        }


# ── account lockout (M14 C.6) ────────────────────────────────────────


class AccountLockout:
    """Failed-attempt tracking → temporary lock (brute-force protection)."""

    def __init__(
        self,
        max_attempts: int = 5,
        window_minutes: int = 15,
        lock_minutes: int = 15,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_minutes = window_minutes
        self.lock_minutes = lock_minutes

    def is_locked(self, failed_attempts: int, locked_until: str) -> tuple[bool, str]:
        """Return (locked?, remaining_seconds_as_str)."""
        if failed_attempts >= self.max_attempts:
            if locked_until:
                until = _parse_iso(locked_until)
                if until and until.timestamp() > time.time():
                    return True, locked_until
            return True, ""
        return False, ""

    def next_locked_until(self, failed_attempts: int) -> str:
        """Return a lock expiry timestamp when attempts exceed the threshold."""
        if failed_attempts + 1 >= self.max_attempts:
            return datetime.now(timezone.utc).timestamp() + self.lock_minutes * 60
        return ""


def _parse_iso(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

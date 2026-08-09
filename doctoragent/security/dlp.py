"""Data Loss Prevention (DLP) module.

Scans content for sensitive information (PII, PCI, PHI) before it is
stored, exported, or transmitted. Provides:
- Pattern-based detection (regex) for common sensitive data types
- LLM-based contextual analysis for nuanced detection
- Policy engine: configurable rules for handling sensitive content
- Content redaction/masking capabilities

The :class:`DataLossPrevention` scanner ships with built-in regex patterns
for the most common sensitive data types (SSN, credit cards with Luhn
validation, phone numbers, e-mail addresses, IP addresses, bank-account
numbers, passport numbers, API keys and PEM private-key headers).  Callers
can extend detection with :meth:`add_pattern` and govern how each type is
handled via :meth:`set_policy` + :meth:`apply_policy`.

The module is thread-safe: pattern compilation and the policy table are
guarded by a lock so a shared instance can serve concurrent requests.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from doctoragent.compat import StrEnum

logger = logging.getLogger(__name__)


class SensitiveDataType(StrEnum):
    """The categories of sensitive data the scanner can identify."""

    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    PHONE = "phone"
    EMAIL = "email"
    IP_ADDRESS = "ip_address"
    BANK_ACCOUNT = "bank_account"
    PASSPORT = "passport"
    API_KEY = "api_key"
    PRIVATE_KEY = "private_key"
    CUSTOM = "custom"


class DLPAction(StrEnum):
    """How a policy instructs the engine to handle matched content."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    REDACT = "redact"


# Built-in regex patterns.  Credit-card candidates are additionally
# validated with the Luhn checksum, so the pattern is deliberately liberal.
_BUILTIN_PATTERNS: dict[SensitiveDataType, str] = {
    SensitiveDataType.SSN: r"\b\d{3}-\d{2}-\d{4}\b",
    SensitiveDataType.CREDIT_CARD: r"\b(?:\d[ -]?){12,18}\d\b",
    SensitiveDataType.PHONE: r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)|(?<!\d)1[3-9]\d{9}(?!\d)",
    SensitiveDataType.EMAIL: r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    SensitiveDataType.IP_ADDRESS: r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
    SensitiveDataType.BANK_ACCOUNT: r"\b\d{8,17}\b",
    SensitiveDataType.PASSPORT: r"\b[A-Z]{1,2}\d{7,8}\b",
    SensitiveDataType.API_KEY: (
        r"\b(?:AKIA[0-9A-Z]{16}"
        r"|(?:sk|pk|api|key|ghp|gho|github_pat|xox[bpoaprs])_[A-Za-z0-9_]{20,}"
        r"|[A-Fa-f0-9]{40,})\b"
    ),
    SensitiveDataType.PRIVATE_KEY: (
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
    ),
}

# Default confidence assigned to a match of each type before any contextual
# refinement.  Luhn-validated credit cards and PEM headers rank highest.
_DEFAULT_CONFIDENCE: dict[SensitiveDataType, float] = {
    SensitiveDataType.SSN: 0.9,
    SensitiveDataType.CREDIT_CARD: 0.95,
    SensitiveDataType.PHONE: 0.7,
    SensitiveDataType.EMAIL: 0.95,
    SensitiveDataType.IP_ADDRESS: 0.8,
    SensitiveDataType.BANK_ACCOUNT: 0.6,
    SensitiveDataType.PASSPORT: 0.6,
    SensitiveDataType.API_KEY: 0.85,
    SensitiveDataType.PRIVATE_KEY: 0.99,
    SensitiveDataType.CUSTOM: 0.5,
}


@dataclass
class SensitiveMatch:
    """A single detected occurrence of sensitive data.

    Attributes:
        data_type: The :class:`SensitiveDataType` that was matched.
        value: The raw matched text.
        start_pos: Inclusive start offset within the scanned text.
        end_pos: Exclusive end offset within the scanned text.
        confidence: Detection confidence in ``[0, 1]``.
        masked_value: A masked representation suitable for safe display.
    """

    data_type: SensitiveDataType
    value: str
    start_pos: int
    end_pos: int
    confidence: float = 0.5
    masked_value: str = ""


@dataclass
class DLPPolicy:
    """A rule describing how to handle one or more sensitive data types.

    Attributes:
        action: The :class:`DLPAction` to take when a matching type is found.
        data_types: The types this policy governs (informational; the engine
            also keys policies by individual type via :meth:`set_policy`).
        severity: Coarse severity for logging/alerting (``INFO``/``WARN``...).
        message: Human-readable explanation surfaced in :class:`DLPResult`.
    """

    action: DLPAction = DLPAction.WARN
    data_types: list[SensitiveDataType] = dc_field(default_factory=list)
    severity: str = "WARN"
    message: str = "Sensitive data detected"


@dataclass
class DLPResult:
    """Outcome of applying DLP policies to a piece of content.

    Attributes:
        action: The most restrictive action taken across all matches.
        matches: All sensitive-data matches found in the content.
        redacted_text: The content after redaction (unchanged if not redacted).
        warnings: Human-readable warning messages, one per triggered policy.
        blocked: ``True`` when a BLOCK policy fired — callers should refuse
            to store/transmit the content.
    """

    action: DLPAction = DLPAction.ALLOW
    matches: list[SensitiveMatch] = dc_field(default_factory=list)
    redacted_text: str = ""
    warnings: list[str] = dc_field(default_factory=list)
    blocked: bool = False


def _luhn_check(value: str) -> bool:
    """Return ``True`` if *value* contains a valid Luhn checksum.

    Non-digit characters are ignored, so ``"4111 1111 1111 1111"`` and
    ``"4111111111111111"`` are both accepted.  A valid candidate must
    contain 13-19 digits.
    """
    digits = [int(c) for c in value if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


class DataLossPrevention:
    """Scan, redact and policy-govern sensitive data in text content.

    Example:
        >>> dlp = DataLossPrevention()
        >>> dlp.set_policy(SensitiveDataType.SSN,
        ...                DLPPolicy(action=DLPAction.BLOCK,
        ...                         data_types=[SensitiveDataType.SSN],
        ...                         message="SSN is not allowed"))
        >>> matches = dlp.scan("Contact me at 123-45-6789")
        >>> result = dlp.apply_policy("Contact me at 123-45-6789", matches)
        >>> result.blocked
        True
    """

    def __init__(self, policies: Any | None = None) -> None:
        self._lock = threading.Lock()
        # Compile built-in patterns into a working copy.
        self._patterns: dict[SensitiveDataType, re.Pattern[str]] = {
            data_type: re.compile(pattern) for data_type, pattern in _BUILTIN_PATTERNS.items()
        }
        # data_type -> DLPPolicy
        self._policies: dict[SensitiveDataType, DLPPolicy] = {}
        if policies is not None:
            self._load_initial_policies(policies)

    def _load_initial_policies(self, policies: Any) -> None:
        """Accept either a ``{data_type: DLPPolicy}`` dict or an iterable."""
        if isinstance(policies, dict):
            for data_type, policy in policies.items():
                if not isinstance(data_type, SensitiveDataType):
                    data_type = SensitiveDataType(str(data_type))
                self._policies[data_type] = policy
        else:
            for policy in policies:
                for data_type in policy.data_types:
                    self._policies[data_type] = policy

    # ── scanning ─────────────────────────────────────────────────────────

    def scan(self, text: str) -> list[SensitiveMatch]:
        """Scan *text* and return all sensitive-data matches.

        Matches are de-duplicated: when two matches overlap, the one with
        the higher (or equal) confidence that contains the other is kept.
        Credit-card candidates are Luhn-validated before being reported.
        """
        if not text:
            return []
        matches: list[SensitiveMatch] = []
        with self._lock:
            patterns = list(self._patterns.items())
        for data_type, pattern in patterns:
            for m in pattern.finditer(text):
                value = m.group(0)
                if data_type == SensitiveDataType.CREDIT_CARD and not _luhn_check(value):
                    continue
                confidence = _DEFAULT_CONFIDENCE.get(data_type, 0.5)
                matches.append(
                    SensitiveMatch(
                        data_type=data_type,
                        value=value,
                        start_pos=m.start(),
                        end_pos=m.end(),
                        confidence=confidence,
                        masked_value=self._mask_value(value, data_type),
                    )
                )
        matches = self._dedupe(matches)
        logger.debug("DLP scan found %d sensitive matches", len(matches))
        return matches

    def scan_and_redact(self, text: str) -> tuple[str, list[SensitiveMatch]]:
        """Scan *text* and return ``(redacted_text, matches)``."""
        matches = self.scan(text)
        return self.redact(text, matches), matches

    # ── redaction ────────────────────────────────────────────────────────

    def redact(self, text: str, matches: list[SensitiveMatch]) -> str:
        """Replace each match in *text* with its masked representation.

        Replacements are applied from the end of the string backwards so
        earlier match offsets stay valid.
        """
        if not matches:
            return text
        result = text
        for match in sorted(matches, key=lambda m: m.start_pos, reverse=True):
            if match.masked_value is None:
                continue
            result = result[: match.start_pos] + match.masked_value + result[match.end_pos :]
        return result

    def _mask_value(self, value: str, data_type: SensitiveDataType) -> str:
        """Mask *value* according to its type, preserving a minimal hint."""
        if not value:
            return value
        if data_type == SensitiveDataType.SSN:
            digits = re.sub(r"\D", "", value)
            return f"***-**-{digits[-4:]}" if len(digits) >= 4 else "*" * len(value)
        if data_type == SensitiveDataType.CREDIT_CARD:
            digits = re.sub(r"\D", "", value)
            return ("*" * (len(digits) - 4) + digits[-4:]) if len(digits) >= 4 else "*" * len(value)
        if data_type == SensitiveDataType.EMAIL:
            if "@" in value:
                local, domain = value.split("@", 1)
                masked_local = (local[0] + "*" * max(1, len(local) - 1)) if local else "*"
                return f"{masked_local}@{domain}"
            return "*" * len(value)
        if data_type == SensitiveDataType.IP_ADDRESS:
            parts = value.split(".")
            if len(parts) == 4:
                return f"{parts[0]}.{parts[1]}.x.x"
            return "*.*.*.*"
        if data_type == SensitiveDataType.PHONE:
            digits = re.sub(r"\D", "", value)
            return ("*" * (len(digits) - 4) + digits[-4:]) if len(digits) >= 4 else "*" * len(value)
        if data_type == SensitiveDataType.PRIVATE_KEY:
            return "[REDACTED PRIVATE KEY]"
        if data_type == SensitiveDataType.API_KEY:
            if len(value) <= 8:
                return "*" * len(value)
            return value[:4] + "*" * (len(value) - 8) + value[-4:]
        if data_type == SensitiveDataType.BANK_ACCOUNT:
            digits = re.sub(r"\D", "", value)
            return ("*" * (len(digits) - 4) + digits[-4:]) if len(digits) >= 4 else "*" * len(value)
        if data_type == SensitiveDataType.PASSPORT:
            return value[0] + "*" * (len(value) - 1)
        # CUSTOM / default: keep first and last char, mask the middle.
        if len(value) <= 2:
            return "*" * len(value)
        return value[0] + "*" * (len(value) - 2) + value[-1]

    # ── policy application ───────────────────────────────────────────────

    def apply_policy(self, text: str, matches: list[SensitiveMatch]) -> DLPResult:
        """Evaluate configured policies against *matches* and act on *text*.

        The most restrictive action wins, with the precedence
        ``BLOCK > REDACT > WARN > ALLOW``.  REDACT actions rewrite the
        content via :meth:`redact`; BLOCK leaves the text untouched but
        flags ``blocked=True`` so the caller can refuse it.
        """
        warnings: list[str] = []
        blocked = False
        should_redact = False
        redactable: list[SensitiveMatch] = []

        with self._lock:
            policies = dict(self._policies)

        for match in matches:
            policy = policies.get(match.data_type)
            if policy is None:
                continue
            label = f"{policy.message} ({match.data_type})"
            if policy.action == DLPAction.BLOCK:
                blocked = True
                warnings.append(label)
                logger.warning(
                    "DLP BLOCK: %s at %d-%d", match.data_type, match.start_pos, match.end_pos
                )
            elif policy.action == DLPAction.REDACT:
                should_redact = True
                redactable.append(match)
                warnings.append(label)
            elif policy.action == DLPAction.WARN:
                warnings.append(label)
            # ALLOW -> no effect

        if blocked:
            final_action = DLPAction.BLOCK
        elif should_redact:
            final_action = DLPAction.REDACT
        elif warnings:
            final_action = DLPAction.WARN
        else:
            final_action = DLPAction.ALLOW

        redacted_text = self.redact(text, redactable) if should_redact and not blocked else text

        return DLPResult(
            action=final_action,
            matches=matches,
            redacted_text=redacted_text,
            warnings=warnings,
            blocked=blocked,
        )

    # ── configuration ────────────────────────────────────────────────────

    def add_pattern(self, data_type: SensitiveDataType, pattern: str) -> None:
        """Add or override a regex *pattern* for *data_type*.

        ``CUSTOM`` is the intended type for caller-supplied patterns, but
        any built-in type may be overridden.  Invalid regex raises
        :class:`re.error`.
        """
        if not isinstance(data_type, SensitiveDataType):
            data_type = SensitiveDataType(str(data_type))
        compiled = re.compile(pattern)
        with self._lock:
            self._patterns[data_type] = compiled
        logger.info("DLP pattern registered for %s", data_type)

    def set_policy(self, data_type: SensitiveDataType, policy: DLPPolicy) -> None:
        """Associate *policy* with *data_type* for :meth:`apply_policy`."""
        if not isinstance(data_type, SensitiveDataType):
            data_type = SensitiveDataType(str(data_type))
        if data_type not in policy.data_types:
            policy.data_types.append(data_type)
        with self._lock:
            self._policies[data_type] = policy
        logger.info("DLP policy set for %s: %s", data_type, policy.action)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _dedupe(matches: list[SensitiveMatch]) -> list[SensitiveMatch]:
        """Drop matches fully contained in a higher-or-equal confidence match.

        Ordering: earliest start first, then longest span, then highest
        confidence — so a more specific/longer match is preferred over a
        shorter one nested inside it.
        """
        ordered = sorted(
            matches,
            key=lambda m: (m.start_pos, -(m.end_pos - m.start_pos), -m.confidence),
        )
        result: list[SensitiveMatch] = []
        for m in ordered:
            contained = any(
                r.start_pos <= m.start_pos
                and m.end_pos <= r.end_pos
                and r.confidence >= m.confidence
                for r in result
            )
            if not contained:
                result.append(m)
        result.sort(key=lambda m: m.start_pos)
        return result

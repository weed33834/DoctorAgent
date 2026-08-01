"""Zero-Trust security architecture for DoctorAgent.

Implements the core principle "never trust, always verify":
- Continuous authentication: verify identity on every request
- Device posture assessment: check device security state before access
- Least-privilege access: dynamic authorization per request
- Micro-segmentation: isolate resources and enforce boundary checks
- Audit every access decision

Unlike the existing trusted-local-connection model, Zero-Trust does not
grant blanket trust to localhost or any network location.  Every access
request is independently evaluated against the current device posture,
behavioural signals and per-resource policy, producing a short-lived,
context-aware :class:`AccessDecision`.

The engine is intentionally self-contained: it keeps its own in-memory
registry of device postures, sessions, policies and an access-decision
history.  All mutable state is guarded by a single :class:`threading.Lock`
so a shared instance is safe to use from multiple worker threads.
"""

from __future__ import annotations

import fnmatch
import logging
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta
from typing import Any

from doctoragent.compat import UTC, StrEnum

logger = logging.getLogger(__name__)

# Lifetime of a positive access decision.  Zero-Trust favours short-lived
# authorizations that must be continually re-earned rather than long-lived
# blanket grants.
_DEFAULT_DECISION_TTL = timedelta(minutes=15)
# A device is considered "stale" if it has not been seen within this window.
_STALE_DEVICE_THRESHOLD = timedelta(hours=1)
# Sliding window used by the behavioural anomaly detector.
_BEHAVIOUR_WINDOW = timedelta(minutes=5)
_BEHAVIOUR_MAX_REQUESTS = 60  # requests within the window before flagging


class TrustLevel(StrEnum):
    """Discrete trust tiers mapped from a continuous ``[0, 1]`` score."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FULL = "full"


# Score -> TrustLevel boundaries (see :meth:`get_trust_level`), declared
# after the TrustLevel class so members are referenceable.
_TRUST_BOUNDARIES = (
    (0.80, TrustLevel.FULL),
    (0.60, TrustLevel.HIGH),
    (0.40, TrustLevel.MEDIUM),
    (0.20, TrustLevel.LOW),
    (0.0, TrustLevel.NONE),
)


@dataclass
class AccessRequest:
    """A single authorization question posed to the engine.

    Attributes:
        subject_id: Identity requesting access (user/service id).
        resource_path: The resource being acted upon, e.g. a vault path or
            API route.  Matched against registered policy patterns.
        action: The verb being requested (``read``/``write``/``delete``...).
        timestamp: When the request occurred.  Defaults to *now* if absent.
        device_id: The device originating the request, if known.
        ip_address: The source IP address, if known.
        context: Extra free-form signals (location, session age, MFA flag,
            ...) used by the trust calculation.
    """

    subject_id: str
    resource_path: str
    action: str
    timestamp: datetime = dc_field(default_factory=lambda: datetime.now(UTC))
    device_id: str | None = None
    ip_address: str | None = None
    context: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class AccessDecision:
    """The engine's verdict for a single :class:`AccessRequest`.

    Attributes:
        allowed: Whether the request is permitted.
        trust_level: The trust tier that was computed for this request.
        reason: Human-readable explanation of the verdict.
        conditions: Caveats attached to the grant (e.g. "MFA required").
        expires_at: When the grant lapses — continuous auth means this is
            always short-lived.  ``None`` for denials.
    """

    allowed: bool
    trust_level: TrustLevel
    reason: str
    conditions: list[str] = dc_field(default_factory=list)
    expires_at: datetime | None = None
    timestamp: datetime = dc_field(default_factory=lambda: datetime.now(UTC))


@dataclass
class DevicePosture:
    """Recorded security state of a registered device.

    Attributes:
        device_id: Stable identifier for the device.
        os_version: Operating system / version string.
        disk_encrypted: Whether full-disk encryption is enabled.
        firewall_enabled: Whether a host firewall is active.
        last_seen: When the device last reported in.
        trust_score: Last computed trust score in ``[0, 1]``.
    """

    device_id: str
    os_version: str = ""
    disk_encrypted: bool = False
    firewall_enabled: bool = False
    last_seen: datetime = dc_field(default_factory=lambda: datetime.now(UTC))
    trust_score: float = 0.0


class ZeroTrustEngine:
    """Evaluate access requests under a Zero-Trust policy.

    The engine combines four signals for every request:

    1. **Revocation** — a subject that has been explicitly revoked is denied
       outright.
    2. **Device posture** — disk encryption, firewall and freshness of the
       last check-in.
    3. **Trust score** — a continuous ``[0, 1]`` blend of device posture and
       request context, mapped to a :class:`TrustLevel`.
    4. **Resource policy** — the minimum trust level required for the
       requested resource, matched by glob pattern.

    Behavioural anomalies (e.g. request bursts) can add conditions or tip a
    borderline decision into a denial.
    """

    def __init__(self, audit_logger: Any | None = None) -> None:
        self._audit = audit_logger
        self._lock = threading.Lock()
        # device_id -> DevicePosture
        self._devices: dict[str, DevicePosture] = {}
        # resource_pattern -> minimum TrustLevel
        self._policies: dict[str, TrustLevel] = {}
        # session_token -> {"subject_id", "device_id", "expires_at"}
        self._sessions: dict[str, dict[str, Any]] = {}
        # subject_ids whose access has been revoked
        self._revoked: set[str] = set()
        # recent (subject_id, AccessDecision) pairs (newest last) for history
        self._history: deque[tuple[str, AccessDecision]] = deque(maxlen=1000)
        # subject_id -> deque[datetime] of recent request timestamps
        self._request_times: dict[str, deque[datetime]] = {}
        # subject_id -> set of recently seen resource_paths
        self._seen_resources: dict[str, set[str]] = {}

    # ── device management ────────────────────────────────────────────────

    def register_device(self, device_id: str, posture: DevicePosture) -> None:
        """Register a new device and its initial security posture."""
        posture.device_id = device_id
        with self._lock:
            self._devices[device_id] = posture
        logger.info(
            "Registered device %s (disk_encrypted=%s, firewall=%s)",
            device_id,
            posture.disk_encrypted,
            posture.firewall_enabled,
        )

    def update_device_posture(self, device_id: str, posture: DevicePosture) -> None:
        """Refresh the recorded posture for an already-registered device."""
        posture.device_id = device_id
        posture.last_seen = datetime.now(UTC)
        with self._lock:
            self._devices[device_id] = posture
        logger.debug("Updated posture for device %s", device_id)

    def _get_device(self, device_id: str | None) -> DevicePosture | None:
        """Return the posture for *device_id* or ``None`` if unknown."""
        if device_id is None:
            return None
        with self._lock:
            return self._devices.get(device_id)

    # ── policy management ────────────────────────────────────────────────

    def set_policy(self, resource_pattern: str, min_trust_level: TrustLevel) -> None:
        """Set the minimum :class:`TrustLevel` required to access resources.

        *resource_pattern* is a glob (``fnmatch``) such as ``"vault:///**"``
        or ``"admin/*"``.  The most specific matching pattern wins; when
        several match, the one requiring the *highest* trust level is used
        so that overlapping policies never weaken each other.
        """
        if not isinstance(min_trust_level, TrustLevel):
            min_trust_level = TrustLevel(str(min_trust_level))
        with self._lock:
            self._policies[resource_pattern] = min_trust_level
        logger.info("Policy set: %s requires %s", resource_pattern, min_trust_level)

    def _required_trust(self, resource_path: str) -> TrustLevel:
        """Resolve the minimum trust level for *resource_path*."""
        required = TrustLevel.NONE
        with self._lock:
            for pattern, level in self._policies.items():
                if fnmatch.fnmatch(resource_path, pattern):
                    if _trust_rank(level) > _trust_rank(required):
                        required = level
        return required

    # ── trust scoring ────────────────────────────────────────────────────

    def calculate_trust_score(
        self, device_posture: DevicePosture | None, context: dict[str, Any]
    ) -> float:
        """Compute a continuous trust score in ``[0, 1]``.

        The score is a weighted sum of independent signals.  An unknown
        device scores low; a known, encrypted, firewalled, recently-seen
        device on a familiar network during business hours scores high.
        """
        score = 0.0
        conditions: list[str] = []

        if device_posture is None:
            # Unknown device — heavily penalized but not necessarily zero.
            score += 0.05
            conditions.append("unknown_device")
        else:
            score += 0.20  # known device baseline
            if device_posture.disk_encrypted:
                score += 0.20
            else:
                conditions.append("disk_unencrypted")
            if device_posture.firewall_enabled:
                score += 0.15
            else:
                conditions.append("firewall_disabled")
            age = datetime.now(UTC) - device_posture.last_seen
            if age <= _STALE_DEVICE_THRESHOLD:
                score += 0.15
            elif age <= _STALE_DEVICE_THRESHOLD * 24:
                score += 0.05
                conditions.append("stale_device")
            else:
                conditions.append("very_stale_device")

        # Contextual signals.
        if context.get("mfa_verified"):
            score += 0.15
        if context.get("known_location"):
            score += 0.10
        elif context.get("location") is not None:
            score -= 0.10
            conditions.append("unknown_location")
        hour = context.get("hour")
        if hour is not None:
            try:
                h = int(hour)
                if 8 <= h <= 20:
                    score += 0.05
                else:
                    score -= 0.05
                    conditions.append("off_hours")
            except (TypeError, ValueError):
                pass
        if context.get("privileged_action"):
            score -= 0.10
            conditions.append("privileged_action")

        # Clamp to [0, 1].
        score = max(0.0, min(1.0, score))
        if conditions:
            logger.debug("Trust conditions: %s", ", ".join(conditions))
        return score

    def get_trust_level(self, score: float) -> TrustLevel:
        """Map a continuous score to a discrete :class:`TrustLevel`."""
        for boundary, level in _TRUST_BOUNDARIES:
            if score >= boundary:
                return level
        return TrustLevel.NONE

    # ── session / continuous auth ────────────────────────────────────────

    def check_continuous_auth(self, session_token: str) -> bool:
        """Return ``True`` if *session_token* is valid and not expired.

        Continuous authentication means a session is never trusted for long;
        each check also refreshes knowledge of the session's existence but
        does not extend its lifetime (callers must re-establish if expired).
        """
        with self._lock:
            session = self._sessions.get(session_token)
            if session is None:
                return False
            if session.get("subject_id") in self._revoked:
                return False
            expires_at = session.get("expires_at")
            if expires_at is None or datetime.now(UTC) >= expires_at:
                self._sessions.pop(session_token, None)
                return False
            return True

    def _issue_session(self, subject_id: str, device_id: str | None) -> str:
        """Mint a short-lived session token for a granted request."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "subject_id": subject_id,
                "device_id": device_id,
                "expires_at": datetime.now(UTC) + _DEFAULT_DECISION_TTL,
            }
        return token

    # ── revocation ───────────────────────────────────────────────────────

    def revoke_access(self, subject_id: str) -> None:
        """Revoke all access — sessions and future grants — for *subject_id*."""
        with self._lock:
            self._revoked.add(subject_id)
            # Drop any active sessions belonging to this subject.
            stale = [
                token
                for token, info in self._sessions.items()
                if info.get("subject_id") == subject_id
            ]
            for token in stale:
                self._sessions.pop(token, None)
        logger.warning("Access revoked for subject %s", subject_id)
        self._safe_audit(
            "policy_violation",
            {"subject_id": subject_id, "reason": "access_revoked"},
        )

    # ── the main evaluation ──────────────────────────────────────────────

    def evaluate_access(self, request: AccessRequest) -> AccessDecision:
        """Evaluate *request* and return an :class:`AccessDecision`."""
        conditions: list[str] = []
        now = datetime.now(UTC)

        # 1. Explicit revocation is an unconditional denial.
        with self._lock:
            is_revoked = request.subject_id in self._revoked
        if is_revoked:
            decision = AccessDecision(
                allowed=False,
                trust_level=TrustLevel.NONE,
                reason=f"subject {request.subject_id} is revoked",
                conditions=["revoked"],
                expires_at=None,
            )
            self._record_decision(request, decision)
            self._safe_audit(
                "policy_violation",
                {
                    "subject_id": request.subject_id,
                    "resource": request.resource_path,
                    "action": request.action,
                    "reason": "revoked_subject",
                },
            )
            return decision

        # 2. Device posture assessment.
        device = self._get_device(request.device_id)
        posture_ok, posture_reasons = self._check_device_posture(request.device_id)
        conditions.extend(posture_reasons)

        # 3. Trust score + level.
        context = dict(request.context)
        context.setdefault("privileged_action", request.action in _PRIVILEGED_ACTIONS)
        score = self.calculate_trust_score(device, context)
        trust_level = self.get_trust_level(score)
        if device is not None:
            with self._lock:
                device.trust_score = score

        # 4. Resource policy.
        required = self._required_trust(request.resource_path)

        # 5. Behavioural anomaly check.
        anomaly, anomaly_reasons = self._check_behavioral_anomaly(request)
        conditions.extend(anomaly_reasons)

        # 6. Verdict.
        allowed = posture_ok and _trust_rank(trust_level) >= _trust_rank(required) and not anomaly

        if allowed:
            reason = f"trust {trust_level} meets required {required}"
            expires_at = now + _DEFAULT_DECISION_TTL
            self._issue_session(request.subject_id, request.device_id)
        else:
            reasons = []
            if not posture_ok:
                reasons.append("device posture insufficient")
            if _trust_rank(trust_level) < _trust_rank(required):
                reasons.append(f"trust {trust_level} below required {required}")
            if anomaly:
                reasons.append("behavioural anomaly detected")
            reason = "; ".join(reasons) or "denied"

        decision = AccessDecision(
            allowed=allowed,
            trust_level=trust_level,
            reason=reason,
            conditions=conditions,
            expires_at=expires_at if allowed else None,
        )

        self._record_decision(request, decision)
        if not allowed:
            self._safe_audit(
                "policy_violation",
                {
                    "subject_id": request.subject_id,
                    "resource": request.resource_path,
                    "action": request.action,
                    "trust_level": str(trust_level),
                    "required": str(required),
                    "reason": reason,
                },
            )
        else:
            logger.debug(
                "Access granted to %s for %s on %s (trust=%s)",
                request.subject_id,
                request.action,
                request.resource_path,
                trust_level,
            )
        return decision

    # ── internal checks ──────────────────────────────────────────────────

    def _check_device_posture(self, device_id: str | None) -> tuple[bool, list[str]]:
        """Verify the security state of *device_id*.

        Returns a ``(ok, reasons)`` pair.  A missing device fails with a
        reason so the caller can surface *why* access was denied.
        """
        reasons: list[str] = []
        device = self._get_device(device_id)
        if device is None:
            return False, ["unknown_device"]
        if not device.disk_encrypted:
            reasons.append("disk_unencrypted")
        if not device.firewall_enabled:
            reasons.append("firewall_disabled")
        age = datetime.now(UTC) - device.last_seen
        if age > _STALE_DEVICE_THRESHOLD:
            reasons.append("stale_device")
        return (len(reasons) == 0, reasons)

    def _check_behavioral_anomaly(self, request: AccessRequest) -> tuple[bool, list[str]]:
        """Detect unusual access patterns for *request*.

        Currently flags request-rate bursts and access to a resource the
        subject has never touched before.  Both are *soft* signals: they add
        conditions and, for borderline trust levels, can flip a grant into a
        denial via the anomaly flag.
        """
        reasons: list[str] = []
        now = request.timestamp
        with self._lock:
            times = self._request_times.setdefault(
                request.subject_id, deque(maxlen=_BEHAVIOUR_MAX_REQUESTS * 2)
            )
            times.append(now)
            # Prune to the sliding window.
            cutoff = now - _BEHAVIOUR_WINDOW
            while times and times[0] < cutoff:
                times.popleft()
            burst = len(times) > _BEHAVIOUR_MAX_REQUESTS

            seen = self._seen_resources.setdefault(request.subject_id, set())
            new_resource = request.resource_path not in seen
            seen.add(request.resource_path)

        if burst:
            reasons.append("request_burst")
        if new_resource:
            reasons.append("new_resource")
        # Only a burst counts as a hard anomaly that can deny access; a new
        # resource on its own is informational.
        return (burst, reasons)

    # ── history ──────────────────────────────────────────────────────────

    def _record_decision(self, request: AccessRequest, decision: AccessDecision) -> None:
        """Append *decision* to the in-memory history ring."""
        with self._lock:
            self._history.append((request.subject_id, decision))

    def get_access_history(
        self, subject_id: str | None = None, limit: int = 100
    ) -> list[AccessDecision]:
        """Return recent access decisions, optionally filtered by subject.

        Results are newest-first.  A *limit* of ``0`` returns an empty list.
        """
        if limit <= 0:
            return []
        with self._lock:
            records = list(self._history)
        if subject_id is not None:
            records = [r for r in records if r[0] == subject_id]
        records.reverse()  # newest first
        return [decision for _subject, decision in records[:limit]]

    # ── helpers ──────────────────────────────────────────────────────────

    def _safe_audit(self, event_type: str, details: dict[str, Any]) -> None:
        """Best-effort audit log write that never propagates failures.

        :class:`AuditLogger.log` rejects event types outside its allow-list;
        we only ever call it with ``"policy_violation"`` (which is allowed)
        and swallow any other error so the security decision path stays
        resilient.
        """
        if self._audit is None:
            return
        try:
            self._audit.log(event_type, details)
        except Exception:  # noqa: BLE001 - audit must not break authz
            logger.debug("Audit log call failed for %s", event_type, exc_info=True)


# ── module-level helpers ────────────────────────────────────────────────────

_TRUST_ORDER: dict[TrustLevel, int] = {
    TrustLevel.NONE: 0,
    TrustLevel.LOW: 1,
    TrustLevel.MEDIUM: 2,
    TrustLevel.HIGH: 3,
    TrustLevel.FULL: 4,
}

# Actions considered privileged for trust-score adjustment.
_PRIVILEGED_ACTIONS: frozenset[str] = frozenset(
    {"delete", "admin", "rotate", "export", "share", "grant"}
)


def _trust_rank(level: TrustLevel) -> int:
    """Numeric rank of a :class:`TrustLevel` for comparison."""
    return _TRUST_ORDER.get(level, 0)

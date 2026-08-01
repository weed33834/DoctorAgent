"""Security analytics module - anomaly detection and threat intelligence.

Implements:
- Behavioral baselines: learn normal access patterns per user/device
- Anomaly detection: flag unusual access (off-hours, bulk downloads, new locations)
- Risk scoring: real-time risk score per user/session
- Threat indicators: track and correlate security events
- Dashboard data: aggregated metrics for security posture

The engine is a self-contained User & Entity Behavior Analysis (UEBA)
facility.  It consumes :class:`SecurityEvent` records, learns per-subject
:class:`BehavioralBaseline` objects from history, and continuously flags
events that deviate from those baselines.  Anomalies feed back into a
per-subject risk score that callers (e.g. the Zero-Trust engine) can use to
gate access.

All shared state is guarded by a :class:`threading.Lock` and the recent
event stream is held in a bounded :class:`collections.deque` so memory use
stays flat under sustained load.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta
from typing import Any

from doctoragent.compat import UTC, StrEnum

logger = logging.getLogger(__name__)

# Size of the in-memory sliding window of events.
_EVENT_WINDOW = 10_000
# How many anomalies to retain for "top anomalies" queries.
_ANOMALY_HISTORY = 5_000
# Events above this risk score are considered "high risk".
_HIGH_RISK_THRESHOLD = 0.7
# Subjects with a risk score at or above this are flagged in posture.
_HIGH_RISK_SUBJECT_THRESHOLD = 0.6


class AnomalyType(StrEnum):
    """Categories of behavioural anomaly the engine can detect."""

    OFF_HOURS_ACCESS = "off_hours_access"
    BULK_OPERATION = "bulk_operation"
    NEW_LOCATION = "new_location"
    UNUSUAL_RESOURCE = "unusual_resource"
    FREQUENCY_SPIKE = "frequency_spike"
    PRIVILEGE_ESCALATION = "privilege_escalation"


# Default per-anomaly thresholds.  The meaning is detector-specific; e.g.
# for OFF_HOURS_ACCESS it is the standard-deviation multiplier, for
# BULK_OPERATION the max events per window, for FREQUENCY_SPIKE the
# multiplier over the baseline frequency.
_DEFAULT_THRESHOLDS: dict[AnomalyType, float] = {
    AnomalyType.OFF_HOURS_ACCESS: 2.0,
    AnomalyType.BULK_OPERATION: 10.0,
    AnomalyType.NEW_LOCATION: 1.0,
    AnomalyType.UNUSUAL_RESOURCE: 1.0,
    AnomalyType.FREQUENCY_SPIKE: 3.0,
    AnomalyType.PRIVILEGE_ESCALATION: 1.0,
}

# Default risk weight contributed by each anomaly type when present.
_ANOMALY_RISK_WEIGHTS: dict[AnomalyType, float] = {
    AnomalyType.OFF_HOURS_ACCESS: 0.15,
    AnomalyType.BULK_OPERATION: 0.30,
    AnomalyType.NEW_LOCATION: 0.20,
    AnomalyType.UNUSUAL_RESOURCE: 0.15,
    AnomalyType.FREQUENCY_SPIKE: 0.25,
    AnomalyType.PRIVILEGE_ESCALATION: 0.40,
}


@dataclass
class BehavioralBaseline:
    """Learned "normal" behaviour for a single subject.

    Attributes:
        subject_id: The user/device this baseline describes.
        avg_access_hour: Mean hour-of-day (0-24, fractional) of access.
        std_access_hour: Standard deviation of the access hour.
        typical_resources: Resources routinely accessed by the subject.
        typical_actions: Actions routinely performed by the subject.
        access_frequency: Observed events per day.
        last_updated: When the baseline was last recomputed.
    """

    subject_id: str
    avg_access_hour: float = 12.0
    std_access_hour: float = 6.0
    typical_resources: set[str] = dc_field(default_factory=set)
    typical_actions: set[str] = dc_field(default_factory=set)
    access_frequency: float = 0.0
    last_updated: datetime = dc_field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SecurityEvent:
    """A single security-relevant event submitted for analysis.

    Attributes:
        event_id: Stable unique identifier for the event.
        timestamp: When the event occurred.
        event_type: Categorical type (e.g. ``"login"``, ``"file_read"``).
        subject_id: The user/device that performed the action.
        resource: The resource acted upon.
        severity: Coarse severity (``INFO``/``WARNING``/``HIGH``/...).
        details: Free-form contextual payload.
        risk_score: Risk score in ``[0, 1]`` assigned during analysis.
    """

    event_id: str
    timestamp: datetime
    event_type: str
    subject_id: str
    resource: str = ""
    severity: str = "INFO"
    details: dict[str, Any] = dc_field(default_factory=dict)
    risk_score: float = 0.0


class SecurityAnalyticsEngine:
    """Anomaly detection, risk scoring and security-posture aggregation.

    Typical usage:

        engine = SecurityAnalyticsEngine(audit_logger=logger)
        engine.build_baseline("alice", historical_events)
        for ev in incoming_events:
            engine.record_event(ev)
        anomalies = engine.detect_anomalies(incoming_events)
    """

    def __init__(self, audit_logger: Any | None = None) -> None:
        self._audit = audit_logger
        self._lock = threading.Lock()
        # Sliding window of recent events (bounded).
        self._events: deque[SecurityEvent] = deque(maxlen=_EVENT_WINDOW)
        # subject_id -> BehavioralBaseline
        self._baselines: dict[str, BehavioralBaseline] = {}
        # subject_id -> running risk score (exp-moving blend).
        self._subject_risk: dict[str, float] = {}
        # Detected anomalies retained for top-N / trend queries.
        self._anomalies: deque[SecurityEvent] = deque(maxlen=_ANOMALY_HISTORY)
        # Configurable per-anomaly thresholds (copy of defaults).
        self._thresholds: dict[AnomalyType, float] = dict(_DEFAULT_THRESHOLDS)
        # subject_id -> set of known locations (for NEW_LOCATION detection).
        self._known_locations: dict[str, set[str]] = defaultdict(set)

    # ── event ingestion ──────────────────────────────────────────────────

    def record_event(self, event: SecurityEvent) -> None:
        """Record *event* and fold it into the running risk state.

        The event's ``risk_score`` is refreshed from the subject's current
        anomaly state so callers always see an up-to-date value.
        """
        if not event.event_id:
            event.event_id = self._make_event_id(event)
        with self._lock:
            self._events.append(event)
            # Track location for the subject when provided.
            location = event.details.get("location")
            if location is not None:
                self._known_locations[event.subject_id].add(str(location))
            # Refresh the live risk score for the subject.
            risk = self._calculate_risk_locked(event.subject_id)
            event.risk_score = risk
            self._subject_risk[event.subject_id] = risk
        logger.debug(
            "Recorded event %s for %s (risk=%.2f)",
            event.event_id,
            event.subject_id,
            event.risk_score,
        )

    # ── baselines ────────────────────────────────────────────────────────

    def build_baseline(
        self, subject_id: str, historical_events: list[SecurityEvent]
    ) -> BehavioralBaseline:
        """Compute and store a behavioural baseline from *historical_events*.

        Only events belonging to *subject_id* are considered.  With fewer
        than two events the statistical fields keep their defaults.
        """
        own = [e for e in historical_events if e.subject_id == subject_id]
        baseline = BehavioralBaseline(subject_id=subject_id)

        if own:
            hours = [
                e.timestamp.hour + e.timestamp.minute / 60.0 + e.timestamp.second / 3600.0
                for e in own
            ]
            baseline.avg_access_hour = sum(hours) / len(hours)
            if len(hours) > 1:
                mean = baseline.avg_access_hour
                variance = sum((h - mean) ** 2 for h in hours) / (len(hours) - 1)
                baseline.std_access_hour = math.sqrt(variance)
            else:
                baseline.std_access_hour = 6.0
            baseline.typical_resources = {e.resource for e in own if e.resource}
            baseline.typical_actions = {e.details.get("action", e.event_type) for e in own}
            span_days = self._span_days(own)
            baseline.access_frequency = len(own) / span_days if span_days > 0 else float(len(own))

        baseline.last_updated = datetime.now(UTC)
        with self._lock:
            self._baselines[subject_id] = baseline
            # Seed known locations from history.
            for e in own:
                location = e.details.get("location")
                if location is not None:
                    self._known_locations[subject_id].add(str(location))
        logger.info(
            "Built baseline for %s from %d events (avg_hour=%.1f, freq=%.2f/day)",
            subject_id,
            len(own),
            baseline.avg_access_hour,
            baseline.access_frequency,
        )
        return baseline

    def _get_baseline(self, subject_id: str) -> BehavioralBaseline:
        """Return the baseline for *subject_id*, creating an empty one if absent."""
        baseline = self._baselines.get(subject_id)
        if baseline is None:
            baseline = BehavioralBaseline(subject_id=subject_id)
            self._baselines[subject_id] = baseline
        return baseline

    # ── anomaly detection ────────────────────────────────────────────────

    def detect_anomalies(self, events: list[SecurityEvent]) -> list[SecurityEvent]:
        """Detect anomalies across a batch of *events*.

        Returns a new list of :class:`SecurityEvent` objects (one per
        detected anomaly) whose ``event_type`` is the :class:`AnomalyType`
        and whose ``risk_score`` reflects the anomaly's weight.  The
        original events are not mutated; detected anomalies are also stored
        internally for trend/top-N queries.
        """
        detected: list[SecurityEvent] = []
        with self._lock:
            for event in events:
                baseline = self._get_baseline(event.subject_id)
                # Off-hours.
                if self._detect_off_hours(event.timestamp, baseline):
                    detected.append(self._make_anomaly(event, AnomalyType.OFF_HOURS_ACCESS))
                # Bulk operation / frequency over the whole batch.
                bulk_hits = self._detect_bulk_operation(events, baseline)
                # detect_bulk_operation returns affected subject ids; flag
                # this event if its subject is among them.
                if event.subject_id in bulk_hits:
                    detected.append(self._make_anomaly(event, AnomalyType.BULK_OPERATION))
                freq_hits = self._detect_frequency_spike(events, baseline)
                if event.subject_id in freq_hits:
                    detected.append(self._make_anomaly(event, AnomalyType.FREQUENCY_SPIKE))
                # Unusual resource.
                if (
                    event.resource
                    and baseline.typical_resources
                    and event.resource not in baseline.typical_resources
                ):
                    detected.append(self._make_anomaly(event, AnomalyType.UNUSUAL_RESOURCE))
                # New location.
                location = event.details.get("location")
                if location is not None:
                    known = self._known_locations.get(event.subject_id, set())
                    if location not in known:
                        detected.append(self._make_anomaly(event, AnomalyType.NEW_LOCATION))
                # Privilege escalation.
                if event.details.get("privilege_escalation"):
                    detected.append(self._make_anomaly(event, AnomalyType.PRIVILEGE_ESCALATION))

            for anomaly in detected:
                self._anomalies.append(anomaly)

        if detected:
            logger.info("Detected %d anomalies from %d events", len(detected), len(events))
            self._safe_audit(
                "policy_violation",
                {
                    "anomalies": len(detected),
                    "events": len(events),
                    "types": sorted({a.event_type for a in detected}),
                },
            )
        return detected

    def _make_anomaly(self, source: SecurityEvent, anomaly_type: AnomalyType) -> SecurityEvent:
        """Build a derived :class:`SecurityEvent` representing one anomaly."""
        weight = _ANOMALY_RISK_WEIGHTS.get(anomaly_type, 0.2)
        return SecurityEvent(
            event_id=self._make_event_id(source, salt=anomaly_type.value),
            timestamp=source.timestamp,
            event_type=str(anomaly_type),
            subject_id=source.subject_id,
            resource=source.resource,
            severity="HIGH" if weight >= 0.3 else "WARNING",
            details={
                "source_event_id": source.event_id,
                "anomaly_type": str(anomaly_type),
            },
            risk_score=min(1.0, source.risk_score + weight),
        )

    def _detect_off_hours(self, timestamp: datetime, baseline: BehavioralBaseline) -> bool:
        """Return ``True`` if *timestamp* is statistically off-hours.

        "Off-hours" means the access hour deviates from the subject's mean
        by more than ``threshold`` standard deviations (clamped to a minimum
        of 3 hours so a very tight baseline does not over-trigger).
        """
        threshold = self._thresholds.get(AnomalyType.OFF_HOURS_ACCESS, 2.0)
        hour = timestamp.hour + timestamp.minute / 60.0
        deviation = abs(hour - baseline.avg_access_hour)
        # Wrap-around: 23h vs 1h are only 2h apart on a 24h clock.
        deviation = min(deviation, 24.0 - deviation)
        min_gap = max(threshold * baseline.std_access_hour, 3.0)
        return deviation > min_gap

    def _detect_bulk_operation(
        self, events: list[SecurityEvent], baseline: BehavioralBaseline
    ) -> set[str]:
        """Return subject ids performing a bulk operation within *events*.

        A bulk operation is more than ``threshold`` events by the same
        subject inside a short window.  Returns the set of offending subject
        ids so :meth:`detect_anomalies` can flag the matching events.
        """
        threshold = int(self._thresholds.get(AnomalyType.BULK_OPERATION, 10.0))
        window = timedelta(minutes=5)
        by_subject: dict[str, list[datetime]] = defaultdict(list)
        for e in events:
            by_subject[e.subject_id].append(e.timestamp)
        offenders: set[str] = set()
        for subject, times in by_subject.items():
            times.sort()
            left = 0
            for right in range(len(times)):
                while times[right] - times[left] > window:
                    left += 1
                if (right - left + 1) > threshold:
                    offenders.add(subject)
                    break
        return offenders

    def _detect_frequency_spike(
        self, events: list[SecurityEvent], baseline: BehavioralBaseline
    ) -> set[str]:
        """Return subject ids whose event rate spikes above baseline.

        Compares the observed events-per-day in *events* against the
        subject's baseline ``access_frequency``; a ratio above
        ``threshold`` is a spike.
        """
        threshold = self._thresholds.get(AnomalyType.FREQUENCY_SPIKE, 3.0)
        by_subject: dict[str, list[SecurityEvent]] = defaultdict(list)
        for e in events:
            by_subject[e.subject_id].append(e)
        offenders: set[str] = set()
        for subject, subject_events in by_subject.items():
            baseline_freq = baseline.access_frequency if baseline.access_frequency > 0 else 1.0
            span = self._span_days(subject_events) or 1.0
            observed_freq = len(subject_events) / span
            if observed_freq >= threshold * baseline_freq:
                offenders.add(subject)
        return offenders

    # ── risk scoring ─────────────────────────────────────────────────────

    def calculate_risk_score(self, subject_id: str) -> float:
        """Return the current real-time risk score in ``[0, 1]``."""
        with self._lock:
            return self._calculate_risk_locked(subject_id)

    def _calculate_risk_locked(self, subject_id: str) -> float:
        """Risk = weighted recent anomaly contribution, blended with history.

        Held while *self._lock* is already acquired.
        """
        # Recent anomalies (last 24h) for this subject drive the live score.
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        score = 0.0
        seen_types: set[str] = set()
        for anomaly in self._anomalies:
            if anomaly.subject_id != subject_id or anomaly.timestamp < cutoff:
                continue
            # Each anomaly type contributes once to avoid double counting.
            if anomaly.event_type in seen_types:
                continue
            seen_types.add(anomaly.event_type)
            try:
                atype = AnomalyType(anomaly.event_type)
            except ValueError:
                continue  # unknown anomaly type — ignore defensively
            weight = _ANOMALY_RISK_WEIGHTS.get(atype, 0.2)
            score += weight
        # Exponentially blend with the previous score so risk decays
        # gracefully rather than resetting to zero.
        previous = self._subject_risk.get(subject_id, 0.0)
        score = max(score, previous * 0.5)
        return max(0.0, min(1.0, score))

    # ── posture / reporting ──────────────────────────────────────────────

    def get_security_posture(self) -> dict[str, Any]:
        """Aggregate current security posture into dashboard metrics.

        Returns a dict with: ``total_events``, ``anomalies_count``,
        ``high_risk_subjects`` (list) and ``avg_risk_score``.
        """
        with self._lock:
            total_events = len(self._events)
            anomalies_count = len(self._anomalies)
            risk_items = list(self._subject_risk.items())
        high_risk_subjects = sorted([s for s, r in risk_items if r >= _HIGH_RISK_SUBJECT_THRESHOLD])
        avg_risk = sum(r for _, r in risk_items) / len(risk_items) if risk_items else 0.0
        return {
            "total_events": total_events,
            "anomalies_count": anomalies_count,
            "high_risk_subjects": high_risk_subjects,
            "avg_risk_score": round(avg_risk, 4),
        }

    def get_risk_trend(self, days: int = 7) -> list[dict[str, Any]]:
        """Return a day-by-day risk trend over the last *days* days.

        Each entry is ``{"date": "YYYY-MM-DD", "avg_risk": float,
        "event_count": int}``.
        """
        if days <= 0:
            return []
        now = datetime.now(UTC)
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        buckets: dict[str, list[tuple[float, datetime]]] = defaultdict(list)
        with self._lock:
            events_snapshot = list(self._events)
            anomalies_snapshot = list(self._anomalies)
        # Average risk per day is approximated from anomaly risk scores.
        for anomaly in anomalies_snapshot:
            if anomaly.timestamp >= start:
                day = anomaly.timestamp.strftime("%Y-%m-%d")
                buckets[day].append((anomaly.risk_score, anomaly.timestamp))
        trend: list[dict[str, Any]] = []
        for offset in range(days):
            day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
            entries = buckets.get(day, [])
            avg = sum(r for r, _ in entries) / len(entries) if entries else 0.0
            count = sum(1 for e in events_snapshot if e.timestamp.strftime("%Y-%m-%d") == day)
            trend.append({"date": day, "avg_risk": round(avg, 4), "event_count": count})
        return trend

    def get_top_anomalies(self, limit: int = 10) -> list[SecurityEvent]:
        """Return the highest-risk anomalies, descending by ``risk_score``."""
        if limit <= 0:
            return []
        with self._lock:
            anomalies = list(self._anomalies)
        anomalies.sort(key=lambda a: a.risk_score, reverse=True)
        return anomalies[:limit]

    # ── configuration ────────────────────────────────────────────────────

    def set_threshold(self, anomaly_type: AnomalyType, threshold: float) -> None:
        """Configure the detection *threshold* for *anomaly_type*."""
        if not isinstance(anomaly_type, AnomalyType):
            anomaly_type = AnomalyType(str(anomaly_type))
        with self._lock:
            self._thresholds[anomaly_type] = float(threshold)
        logger.info("Threshold for %s set to %.2f", anomaly_type, threshold)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _span_days(events: list[SecurityEvent]) -> float:
        """Span of *events* in days (minimum 1 day to avoid div-by-zero)."""
        if not events:
            return 0.0
        times = [e.timestamp for e in events]
        span = (max(times) - min(times)).total_seconds() / 86400.0
        return span if span > 0 else 1.0

    @staticmethod
    def _make_event_id(source: SecurityEvent, salt: str = "") -> str:
        """Derive a deterministic-ish id for an event lacking one."""
        material = f"{source.timestamp.isoformat()}|{source.subject_id}|{source.event_type}|{salt}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def _safe_audit(self, event_type: str, details: dict[str, Any]) -> None:
        """Best-effort audit log write that never propagates failures."""
        if self._audit is None:
            return
        try:
            self._audit.log(event_type, details)
        except Exception:  # noqa: BLE001 - analytics must not break on audit
            logger.debug("Audit log call failed for %s", event_type, exc_info=True)

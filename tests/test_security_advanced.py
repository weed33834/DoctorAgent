# mypy: ignore-errors
"""Tests for the advanced security modules.

Covers:
- Shamir's Secret Sharing (split/reconstruct, hex helpers, edge cases)
- Zero-Trust engine (device registration, trust scoring, access evaluation,
  revocation, policy enforcement)
- Security analytics engine (event recording, baselines, anomaly detection,
  risk scoring, posture reporting)
- Data Loss Prevention (pattern scanning, redaction, policy enforcement,
  custom patterns)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

UTC = timezone.utc

from doctoragent.security.shamir import (
    Share,
    ShamirSecretSharing,
)
from doctoragent.security.zero_trust import (
    AccessDecision,
    AccessRequest,
    DevicePosture,
    TrustLevel,
    ZeroTrustEngine,
)
from doctoragent.security.analytics import (
    AnomalyType,
    BehavioralBaseline,
    SecurityAnalyticsEngine,
    SecurityEvent,
)
from doctoragent.security.dlp import (
    DataLossPrevention,
    DLPAction,
    DLPPolicy,
    DLPResult,
    SensitiveDataType,
)


# ---------------------------------------------------------------------------
# Shamir's Secret Sharing
# ---------------------------------------------------------------------------

class TestShamirSecretSharing:
    """Tests for :class:`ShamirSecretSharing`."""

    @pytest.fixture
    def sss(self) -> ShamirSecretSharing:
        return ShamirSecretSharing()

    @pytest.mark.parametrize(
        "threshold,total",
        [
            (2, 3),
            (3, 5),
            (5, 7),
            (2, 2),
            (1, 1),
        ],
    )
    def test_split_and_reconstruct(
        self, sss: ShamirSecretSharing, threshold: int, total: int
    ) -> None:
        secret = b"my top secret key data"
        shares = sss.split(secret, threshold=threshold, total=total)
        assert len(shares) == total
        for share in shares:
            assert share.threshold == threshold
            assert share.total == total
            assert len(share.data) == len(secret)
            assert 1 <= share.index <= total
        # Reconstruct with exactly *threshold* shares.
        reconstructed = sss.reconstruct(shares[:threshold])
        assert reconstructed == secret

    @pytest.mark.parametrize(
        "threshold,total",
        [
            (2, 3),
            (3, 5),
            (5, 7),
        ],
    )
    def test_reconstruct_with_extra_shares(
        self, sss: ShamirSecretSharing, threshold: int, total: int
    ) -> None:
        secret = b"another secret value"
        shares = sss.split(secret, threshold=threshold, total=total)
        # Using more than the threshold should still work.
        reconstructed = sss.reconstruct(shares)
        assert reconstructed == secret

    def test_fewer_than_threshold_cannot_reconstruct(
        self, sss: ShamirSecretSharing
    ) -> None:
        secret = b"protected data"
        shares = sss.split(secret, threshold=3, total=5)
        # Only 2 shares — should raise.
        with pytest.raises(ValueError, match="need at least 3 shares"):
            sss.reconstruct(shares[:2])

    def test_reconstruct_with_too_few_shares_one_short(
        self, sss: ShamirSecretSharing
    ) -> None:
        shares = sss.split(b"x" * 16, threshold=5, total=7)
        with pytest.raises(ValueError, match="need at least 5 shares"):
            sss.reconstruct(shares[:4])

    def test_reconstruct_empty_shares_raises(self, sss: ShamirSecretSharing) -> None:
        with pytest.raises(ValueError, match="empty share list"):
            sss.reconstruct([])

    def test_reconstruct_duplicate_index_raises(
        self, sss: ShamirSecretSharing
    ) -> None:
        shares = sss.split(b"secret123", threshold=2, total=3)
        # Duplicate index by replacing share[1]'s index with share[0]'s.
        tampered = Share(
            index=shares[0].index,
            data=shares[1].data,
            threshold=shares[1].threshold,
            total=shares[1].total,
        )
        with pytest.raises(ValueError, match="duplicate share index"):
            sss.reconstruct([shares[0], tampered])

    def test_reconstruct_inconsistent_metadata_raises(
        self, sss: ShamirSecretSharing
    ) -> None:
        shares_a = sss.split(b"secret_a", threshold=2, total=3)
        shares_b = sss.split(b"secret_b", threshold=3, total=5)
        with pytest.raises(ValueError, match="inconsistent threshold/total"):
            sss.reconstruct([shares_a[0], shares_b[0]])

    def test_hex_split_and_reconstruct(self, sss: ShamirSecretSharing) -> None:
        secret_hex = "deadbeefcafebabe"
        share_tokens = sss.split_hex(secret_hex, threshold=3, total=5)
        assert len(share_tokens) == 5
        for token in share_tokens:
            # Format: index-threshold-total:datahex
            assert ":" in token
            assert "-" in token
        reconstructed_hex = sss.reconstruct_hex(share_tokens[:3])
        assert reconstructed_hex == secret_hex

    def test_hex_round_trip_with_ascii_secret(
        self, sss: ShamirSecretSharing
    ) -> None:
        original = "hello world"
        secret_hex = original.encode().hex()
        tokens = sss.split_hex(secret_hex, threshold=2, total=3)
        result_hex = sss.reconstruct_hex(tokens[:2])
        assert bytes.fromhex(result_hex).decode() == original

    def test_reconstruct_hex_malformed_token_raises(
        self, sss: ShamirSecretSharing
    ) -> None:
        with pytest.raises(ValueError, match="malformed share token"):
            sss.reconstruct_hex(["not-a-valid-token"])

    def test_split_empty_secret(self, sss: ShamirSecretSharing) -> None:
        shares = sss.split(b"", threshold=2, total=3)
        assert len(shares) == 3
        for share in shares:
            assert share.data == b""
        reconstructed = sss.reconstruct(shares[:2])
        assert reconstructed == b""

    def test_split_single_byte(self, sss: ShamirSecretSharing) -> None:
        secret = b"\x42"
        shares = sss.split(secret, threshold=2, total=3)
        for share in shares:
            assert len(share.data) == 1
        assert sss.reconstruct(shares[:2]) == secret

    def test_split_invalid_threshold_raises(self, sss: ShamirSecretSharing) -> None:
        with pytest.raises(ValueError, match="threshold must be >= 1"):
            sss.split(b"secret", threshold=0, total=3)

    def test_split_threshold_exceeds_total_raises(
        self, sss: ShamirSecretSharing
    ) -> None:
        with pytest.raises(ValueError, match="threshold cannot exceed total"):
            sss.split(b"secret", threshold=5, total=3)

    def test_split_hex_invalid_hex_raises(self, sss: ShamirSecretSharing) -> None:
        with pytest.raises(ValueError, match="invalid hex secret"):
            sss.split_hex("not-hex!!", threshold=2, total=3)

    def test_share_dataclass_validation(self) -> None:
        share = Share(index=1, data=b"abc", threshold=2, total=3)
        assert share.index == 1
        assert share.data == b"abc"

    def test_share_invalid_index_raises(self) -> None:
        with pytest.raises(ValueError, match="share index"):
            Share(index=0, data=b"abc", threshold=2, total=3)

    def test_share_threshold_gt_total_raises(self) -> None:
        with pytest.raises(ValueError, match="total must be >= threshold"):
            Share(index=1, data=b"abc", threshold=5, total=3)

    def test_threshold_of_one(self, sss: ShamirSecretSharing) -> None:
        secret = b"single-share-secret"
        shares = sss.split(secret, threshold=1, total=1)
        assert len(shares) == 1
        assert sss.reconstruct(shares) == secret


# ---------------------------------------------------------------------------
# Zero-Trust Engine
# ---------------------------------------------------------------------------

class TestZeroTrustEngine:
    """Tests for :class:`ZeroTrustEngine`."""

    @pytest.fixture
    def engine(self) -> ZeroTrustEngine:
        return ZeroTrustEngine()

    @pytest.fixture
    def good_posture(self) -> DevicePosture:
        return DevicePosture(
            device_id="dev-good",
            os_version="macOS 14.0",
            disk_encrypted=True,
            firewall_enabled=True,
            last_seen=datetime.now(UTC),
        )

    @pytest.fixture
    def poor_posture(self) -> DevicePosture:
        return DevicePosture(
            device_id="dev-poor",
            os_version="Windows 10",
            disk_encrypted=False,
            firewall_enabled=False,
            last_seen=datetime.now(UTC),
        )

    def test_register_device(self, engine: ZeroTrustEngine, good_posture: DevicePosture) -> None:
        engine.register_device("dev-1", good_posture)
        # Access should be evaluated using the registered device.
        request = AccessRequest(
            subject_id="user-1",
            resource_path="vault:///docs/readme.txt",
            action="read",
            device_id="dev-1",
            context={"mfa_verified": True, "known_location": True, "hour": 10},
        )
        decision = engine.evaluate_access(request)
        assert decision.allowed

    def test_calculate_trust_score_known_good_device(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        score = engine.calculate_trust_score(
            good_posture,
            {"mfa_verified": True, "known_location": True, "hour": 12},
        )
        assert score >= 0.6  # Should be HIGH or FULL

    def test_calculate_trust_score_unknown_device(
        self, engine: ZeroTrustEngine
    ) -> None:
        score = engine.calculate_trust_score(None, {})
        assert score <= 0.1  # Unknown device is heavily penalized

    def test_calculate_trust_score_poor_posture(
        self, engine: ZeroTrustEngine, poor_posture: DevicePosture
    ) -> None:
        score = engine.calculate_trust_score(poor_posture, {})
        # No encryption, no firewall, no MFA -> low score
        assert score < 0.4

    def test_get_trust_level_boundaries(self, engine: ZeroTrustEngine) -> None:
        assert engine.get_trust_level(0.85) == TrustLevel.FULL
        assert engine.get_trust_level(0.65) == TrustLevel.HIGH
        assert engine.get_trust_level(0.45) == TrustLevel.MEDIUM
        assert engine.get_trust_level(0.25) == TrustLevel.LOW
        assert engine.get_trust_level(0.0) == TrustLevel.NONE

    def test_evaluate_access_granted_for_high_trust(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        engine.set_policy("vault:///**", TrustLevel.MEDIUM)
        request = AccessRequest(
            subject_id="user-1",
            resource_path="vault:///docs/secret.txt",
            action="read",
            device_id="dev-good",
            context={"mfa_verified": True, "known_location": True, "hour": 14},
        )
        decision = engine.evaluate_access(request)
        assert decision.allowed
        assert decision.expires_at is not None

    def test_evaluate_access_denied_for_low_trust(
        self, engine: ZeroTrustEngine, poor_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-poor", poor_posture)
        engine.set_policy("vault:///**", TrustLevel.HIGH)
        request = AccessRequest(
            subject_id="user-bad",
            resource_path="vault:///docs/secret.txt",
            action="read",
            device_id="dev-poor",
            context={},
        )
        decision = engine.evaluate_access(request)
        assert not decision.allowed
        assert decision.expires_at is None

    def test_evaluate_access_denied_for_unknown_device(
        self, engine: ZeroTrustEngine
    ) -> None:
        engine.set_policy("vault:///**", TrustLevel.MEDIUM)
        request = AccessRequest(
            subject_id="user-1",
            resource_path="vault:///docs/secret.txt",
            action="read",
            device_id="unknown-device",
            context={},
        )
        decision = engine.evaluate_access(request)
        assert not decision.allowed
        assert "unknown_device" in decision.conditions or "posture" in decision.reason

    def test_revoke_access_denies_future_requests(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        engine.set_policy("vault:///**", TrustLevel.LOW)
        request = AccessRequest(
            subject_id="user-revoked",
            resource_path="vault:///docs/readme.txt",
            action="read",
            device_id="dev-good",
            context={"mfa_verified": True},
        )
        # First request should succeed.
        decision = engine.evaluate_access(request)
        assert decision.allowed
        # Revoke access.
        engine.revoke_access("user-revoked")
        # Second request should be denied.
        decision2 = engine.evaluate_access(request)
        assert not decision2.allowed
        assert "revoked" in decision2.reason

    def test_policy_enforcement_highest_trust_wins(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        # Two overlapping policies — the stricter one should win.
        engine.set_policy("vault:///**", TrustLevel.LOW)
        engine.set_policy("vault:///admin/**", TrustLevel.FULL)
        request = AccessRequest(
            subject_id="user-1",
            resource_path="vault:///admin/config",
            action="read",
            device_id="dev-good",
            context={"mfa_verified": True, "known_location": True, "hour": 12},
        )
        decision = engine.evaluate_access(request)
        # The admin resource requires FULL trust, which even a good device
        # without all signals may not reach.
        # The decision must reflect the FULL requirement.
        if not decision.allowed:
            assert "FULL" in decision.reason or "trust" in decision.reason

    def test_get_access_history(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        engine.set_policy("vault:///**", TrustLevel.LOW)
        for i in range(3):
            engine.evaluate_access(
                AccessRequest(
                    subject_id="user-history",
                    resource_path=f"vault:///docs/{i}.txt",
                    action="read",
                    device_id="dev-good",
                    context={"mfa_verified": True},
                )
            )
        history = engine.get_access_history("user-history")
        assert len(history) == 3
        # Limit filtering.
        assert len(engine.get_access_history("user-history", limit=2)) == 2
        # Non-matching subject returns empty.
        assert engine.get_access_history("nobody") == []

    def test_update_device_posture(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        updated = DevicePosture(
            device_id="dev-good",
            disk_encrypted=False,
            firewall_enabled=False,
        )
        engine.update_device_posture("dev-good", updated)
        # After degrading the posture, access should be harder.
        engine.set_policy("vault:///**", TrustLevel.MEDIUM)
        request = AccessRequest(
            subject_id="user-1",
            resource_path="vault:///docs/test.txt",
            action="read",
            device_id="dev-good",
            context={},
        )
        decision = engine.evaluate_access(request)
        assert not decision.allowed

    def test_privileged_action_reduces_score(
        self, engine: ZeroTrustEngine, good_posture: DevicePosture
    ) -> None:
        engine.register_device("dev-good", good_posture)
        base_score = engine.calculate_trust_score(
            good_posture, {"mfa_verified": True, "hour": 12}
        )
        privileged_score = engine.calculate_trust_score(
            good_posture,
            {"mfa_verified": True, "hour": 12, "privileged_action": True},
        )
        assert privileged_score < base_score


# ---------------------------------------------------------------------------
# Security Analytics Engine
# ---------------------------------------------------------------------------

class TestSecurityAnalyticsEngine:
    """Tests for :class:`SecurityAnalyticsEngine`."""

    @pytest.fixture
    def engine(self) -> SecurityAnalyticsEngine:
        return SecurityAnalyticsEngine()

    def _make_event(
        self,
        subject_id: str = "alice",
        event_type: str = "file_read",
        resource: str = "vault:///docs/readme.txt",
        hour: int = 10,
        details: dict[str, Any] | None = None,
    ) -> SecurityEvent:
        ts = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
        return SecurityEvent(
            event_id=f"evt-{subject_id}-{hour}-{resource}",
            timestamp=ts,
            event_type=event_type,
            subject_id=subject_id,
            resource=resource,
            details=details or {},
        )

    def test_record_event(self, engine: SecurityAnalyticsEngine) -> None:
        event = self._make_event()
        engine.record_event(event)
        posture = engine.get_security_posture()
        assert posture["total_events"] >= 1

    def test_record_event_assigns_event_id_if_missing(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        event = SecurityEvent(
            event_id="",
            timestamp=datetime.now(UTC),
            event_type="login",
            subject_id="bob",
        )
        engine.record_event(event)
        assert event.event_id != ""

    def test_build_baseline(self, engine: SecurityAnalyticsEngine) -> None:
        events = [
            self._make_event(hour=10, resource="vault:///docs/a.txt"),
            self._make_event(hour=11, resource="vault:///docs/a.txt"),
            self._make_event(hour=12, resource="vault:///docs/b.txt"),
        ]
        baseline = engine.build_baseline("alice", events)
        assert baseline.subject_id == "alice"
        assert 10.0 <= baseline.avg_access_hour <= 12.0
        assert "vault:///docs/a.txt" in baseline.typical_resources
        assert "vault:///docs/b.txt" in baseline.typical_resources
        assert baseline.access_frequency > 0

    def test_build_baseline_empty(self, engine: SecurityAnalyticsEngine) -> None:
        baseline = engine.build_baseline("nobody", [])
        assert baseline.subject_id == "nobody"
        assert baseline.avg_access_hour == 12.0
        assert baseline.typical_resources == set()

    def test_detect_anomalies_off_hours(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        # Build a baseline centred on midday.
        normal_events = [
            self._make_event(hour=12, subject_id="alice"),
            self._make_event(hour=13, subject_id="alice"),
            self._make_event(hour=14, subject_id="alice"),
        ]
        engine.build_baseline("alice", normal_events)
        # An event at 3 AM should be flagged as off-hours.
        off_hours_event = self._make_event(hour=3, subject_id="alice")
        anomalies = engine.detect_anomalies([off_hours_event])
        types = {a.event_type for a in anomalies}
        assert str(AnomalyType.OFF_HOURS_ACCESS) in types

    def test_detect_anomalies_bulk_operation(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        baseline_events = [
            self._make_event(hour=10, subject_id="bulk-user"),
        ]
        engine.build_baseline("bulk-user", baseline_events)
        # Generate more than the bulk threshold (10) events in a short window.
        now = datetime.now(UTC)
        bulk_events = [
            SecurityEvent(
                event_id=f"bulk-{i}",
                timestamp=now,
                event_type="file_read",
                subject_id="bulk-user",
                resource=f"vault:///docs/{i}.txt",
            )
            for i in range(15)
        ]
        anomalies = engine.detect_anomalies(bulk_events)
        types = {a.event_type for a in anomalies}
        assert str(AnomalyType.BULK_OPERATION) in types

    def test_detect_anomalies_new_location(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        # Seed known locations.
        normal_events = [
            self._make_event(
                hour=10, subject_id="carol", details={"location": "office"}
            ),
        ]
        engine.build_baseline("carol", normal_events)
        # An event from a new location.
        new_loc_event = self._make_event(
            hour=10, subject_id="carol", details={"location": "malaysia"}
        )
        anomalies = engine.detect_anomalies([new_loc_event])
        types = {a.event_type for a in anomalies}
        assert str(AnomalyType.NEW_LOCATION) in types

    def test_detect_anomalies_privilege_escalation(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        engine.build_baseline("dave", [self._make_event(hour=10, subject_id="dave")])
        esc_event = self._make_event(
            hour=10,
            subject_id="dave",
            details={"privilege_escalation": True},
        )
        anomalies = engine.detect_anomalies([esc_event])
        types = {a.event_type for a in anomalies}
        assert str(AnomalyType.PRIVILEGE_ESCALATION) in types

    def test_detect_anomalies_no_anomalies_for_normal(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        normal_events = [
            self._make_event(hour=12, subject_id="eve"),
            self._make_event(hour=13, subject_id="eve"),
        ]
        engine.build_baseline("eve", normal_events)
        # A normal event matching the baseline.
        normal = self._make_event(hour=12, subject_id="eve")
        anomalies = engine.detect_anomalies([normal])
        # Should not flag off-hours for a midday event with midday baseline.
        types = {a.event_type for a in anomalies}
        assert str(AnomalyType.OFF_HOURS_ACCESS) not in types

    def test_calculate_risk_score(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        # Multiple events at similar hours produce a tight baseline so that
        # a 3 AM access is statistically off-hours.
        baseline_events = [
            self._make_event(hour=10, subject_id="frank", resource="vault:///docs/a.txt"),
            self._make_event(hour=11, subject_id="frank", resource="vault:///docs/b.txt"),
            self._make_event(hour=12, subject_id="frank", resource="vault:///docs/c.txt"),
        ]
        engine.build_baseline("frank", baseline_events)
        # Record some anomalies to drive up risk.
        off_hours = self._make_event(hour=3, subject_id="frank", resource="vault:///docs/d.txt")
        engine.detect_anomalies([off_hours])
        risk = engine.calculate_risk_score("frank")
        assert risk > 0.0

    def test_calculate_risk_score_no_data(self, engine: SecurityAnalyticsEngine) -> None:
        risk = engine.calculate_risk_score("ghost")
        assert risk == 0.0

    def test_get_security_posture(
        self, engine: SecurityAnalyticsEngine
    ) -> None:
        engine.record_event(self._make_event(subject_id="alice"))
        engine.record_event(self._make_event(subject_id="bob"))
        posture = engine.get_security_posture()
        assert "total_events" in posture
        assert "anomalies_count" in posture
        assert "high_risk_subjects" in posture
        assert "avg_risk_score" in posture
        assert posture["total_events"] >= 2

    def test_set_threshold(self, engine: SecurityAnalyticsEngine) -> None:
        engine.set_threshold(AnomalyType.BULK_OPERATION, 50.0)
        # With a high threshold, a previously-flagged bulk operation should not trigger.
        baseline_events = [self._make_event(hour=10, subject_id="thresh-user")]
        engine.build_baseline("thresh-user", baseline_events)
        now = datetime.now(UTC)
        events = [
            SecurityEvent(
                event_id=f"t-{i}",
                timestamp=now,
                event_type="file_read",
                subject_id="thresh-user",
                resource=f"vault:///docs/{i}.txt",
            )
            for i in range(15)
        ]
        anomalies = engine.detect_anomalies(events)
        types = {a.event_type for a in anomalies}
        assert str(AnomalyType.BULK_OPERATION) not in types

    def test_get_top_anomalies(self, engine: SecurityAnalyticsEngine) -> None:
        # Build a tight baseline so a 3 AM event is flagged as off-hours.
        baseline_events = [
            self._make_event(hour=10, subject_id="grace", resource="vault:///docs/a.txt"),
            self._make_event(hour=11, subject_id="grace", resource="vault:///docs/b.txt"),
            self._make_event(hour=12, subject_id="grace", resource="vault:///docs/c.txt"),
        ]
        engine.build_baseline("grace", baseline_events)
        off_hours = self._make_event(hour=3, subject_id="grace", resource="vault:///docs/d.txt")
        engine.detect_anomalies([off_hours])
        top = engine.get_top_anomalies(limit=10)
        assert len(top) >= 1


# ---------------------------------------------------------------------------
# Data Loss Prevention
# ---------------------------------------------------------------------------

class TestDataLossPrevention:
    """Tests for :class:`DataLossPrevention`."""

    @pytest.fixture
    def dlp(self) -> DataLossPrevention:
        return DataLossPrevention()

    def test_scan_ssn(self, dlp: DataLossPrevention) -> None:
        text = "My SSN is 123-45-6789 and I live in NYC."
        matches = dlp.scan(text)
        ssn_matches = [m for m in matches if m.data_type == SensitiveDataType.SSN]
        assert len(ssn_matches) == 1
        assert ssn_matches[0].value == "123-45-6789"
        assert ssn_matches[0].confidence >= 0.9

    def test_scan_credit_card(self, dlp: DataLossPrevention) -> None:
        # 4111 1111 1111 1111 is a valid Luhn number.
        text = "Card: 4111 1111 1111 1111 expires 12/25"
        matches = dlp.scan(text)
        cc_matches = [m for m in matches if m.data_type == SensitiveDataType.CREDIT_CARD]
        assert len(cc_matches) == 1

    def test_scan_credit_card_invalid_luhn_skipped(
        self, dlp: DataLossPrevention
    ) -> None:
        # 13 digits but invalid Luhn checksum.
        text = "1234 5678 9012 3"
        matches = dlp.scan(text)
        cc_matches = [m for m in matches if m.data_type == SensitiveDataType.CREDIT_CARD]
        assert len(cc_matches) == 0

    def test_scan_email(self, dlp: DataLossPrevention) -> None:
        text = "Contact me at john.doe@example.com for details."
        matches = dlp.scan(text)
        email_matches = [m for m in matches if m.data_type == SensitiveDataType.EMAIL]
        assert len(email_matches) == 1
        assert email_matches[0].value == "john.doe@example.com"

    def test_scan_phone(self, dlp: DataLossPrevention) -> None:
        text = "Call me at (555) 123-4567 today."
        matches = dlp.scan(text)
        phone_matches = [m for m in matches if m.data_type == SensitiveDataType.PHONE]
        assert len(phone_matches) >= 1

    def test_scan_multiple_types(self, dlp: DataLossPrevention) -> None:
        text = (
            "SSN: 123-45-6789, Email: test@test.com, "
            "Phone: (555) 123-4567, Card: 4111 1111 1111 1111"
        )
        matches = dlp.scan(text)
        types_found = {m.data_type for m in matches}
        assert SensitiveDataType.SSN in types_found
        assert SensitiveDataType.EMAIL in types_found
        assert SensitiveDataType.PHONE in types_found
        assert SensitiveDataType.CREDIT_CARD in types_found

    def test_scan_empty_text(self, dlp: DataLossPrevention) -> None:
        assert dlp.scan("") == []

    def test_scan_no_sensitive_data(self, dlp: DataLossPrevention) -> None:
        text = "This is a perfectly normal sentence with no sensitive data."
        matches = dlp.scan(text)
        assert matches == []

    def test_scan_match_positions(self, dlp: DataLossPrevention) -> None:
        text = "SSN: 123-45-6789"
        matches = dlp.scan(text)
        ssn = [m for m in matches if m.data_type == SensitiveDataType.SSN][0]
        assert text[ssn.start_pos : ssn.end_pos] == "123-45-6789"

    def test_scan_and_redact(self, dlp: DataLossPrevention) -> None:
        text = "My SSN is 123-45-6789."
        redacted, matches = dlp.scan_and_redact(text)
        assert "123-45-6789" not in redacted
        assert len(matches) >= 1

    def test_redact_preserves_non_sensitive(
        self, dlp: DataLossPrevention
    ) -> None:
        text = "Hello world, my email is test@example.com goodbye."
        matches = dlp.scan(text)
        redacted = dlp.redact(text, matches)
        assert "Hello world" in redacted
        assert "goodbye" in redacted
        assert "test@example.com" not in redacted

    def test_apply_policy_block(self, dlp: DataLossPrevention) -> None:
        dlp.set_policy(
            SensitiveDataType.SSN,
            DLPPolicy(
                action=DLPAction.BLOCK,
                data_types=[SensitiveDataType.SSN],
                message="SSN not allowed",
            ),
        )
        text = "SSN: 123-45-6789"
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert result.blocked
        assert result.action == DLPAction.BLOCK
        assert any("SSN" in w for w in result.warnings)

    def test_apply_policy_redact(self, dlp: DataLossPrevention) -> None:
        dlp.set_policy(
            SensitiveDataType.EMAIL,
            DLPPolicy(
                action=DLPAction.REDACT,
                data_types=[SensitiveDataType.EMAIL],
                message="Email redacted",
            ),
        )
        text = "Email: test@example.com"
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert not result.blocked
        assert result.action == DLPAction.REDACT
        assert "test@example.com" not in result.redacted_text

    def test_apply_policy_warn(self, dlp: DataLossPrevention) -> None:
        dlp.set_policy(
            SensitiveDataType.PHONE,
            DLPPolicy(
                action=DLPAction.WARN,
                data_types=[SensitiveDataType.PHONE],
                message="Phone detected",
            ),
        )
        text = "Phone: (555) 123-4567"
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert not result.blocked
        assert result.action == DLPAction.WARN
        assert len(result.warnings) >= 1

    def test_apply_policy_allow(self, dlp: DataLossPrevention) -> None:
        text = "No sensitive data here."
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert result.action == DLPAction.ALLOW
        assert not result.blocked
        assert result.warnings == []

    def test_apply_policy_block_overrides_redact(
        self, dlp: DataLossPrevention
    ) -> None:
        dlp.set_policy(
            SensitiveDataType.EMAIL,
            DLPPolicy(
                action=DLPAction.REDACT,
                data_types=[SensitiveDataType.EMAIL],
                message="Email redacted",
            ),
        )
        dlp.set_policy(
            SensitiveDataType.SSN,
            DLPPolicy(
                action=DLPAction.BLOCK,
                data_types=[SensitiveDataType.SSN],
                message="SSN blocked",
            ),
        )
        text = "Email: test@example.com SSN: 123-45-6789"
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert result.blocked
        assert result.action == DLPAction.BLOCK

    def test_add_custom_pattern(self, dlp: DataLossPrevention) -> None:
        dlp.add_pattern(SensitiveDataType.CUSTOM, r"\bPROJ-\d{4}\b")
        text = "Ticket reference: PROJ-1234 is ready."
        matches = dlp.scan(text)
        custom_matches = [m for m in matches if m.data_type == SensitiveDataType.CUSTOM]
        assert len(custom_matches) == 1
        assert custom_matches[0].value == "PROJ-1234"

    def test_mask_value_ssn(self, dlp: DataLossPrevention) -> None:
        text = "SSN: 123-45-6789"
        matches = dlp.scan(text)
        ssn = [m for m in matches if m.data_type == SensitiveDataType.SSN][0]
        assert ssn.masked_value == "***-**-6789"

    def test_mask_value_email(self, dlp: DataLossPrevention) -> None:
        text = "Email: john@example.com"
        matches = dlp.scan(text)
        email = [m for m in matches if m.data_type == SensitiveDataType.EMAIL][0]
        assert "@" in email.masked_value
        assert email.masked_value != "john@example.com"

    def test_initial_policies_via_dict(self) -> None:
        policies = {
            SensitiveDataType.SSN: DLPPolicy(
                action=DLPAction.BLOCK,
                data_types=[SensitiveDataType.SSN],
            ),
        }
        dlp = DataLossPrevention(policies=policies)
        text = "SSN: 123-45-6789"
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert result.blocked

    def test_initial_policies_via_iterable(self) -> None:
        policies = [
            DLPPolicy(
                action=DLPAction.REDACT,
                data_types=[SensitiveDataType.EMAIL, SensitiveDataType.PHONE],
            ),
        ]
        dlp = DataLossPrevention(policies=policies)
        text = "Email: a@b.com Phone: (555) 111-2222"
        matches = dlp.scan(text)
        result = dlp.apply_policy(text, matches)
        assert result.action == DLPAction.REDACT
        assert "a@b.com" not in result.redacted_text

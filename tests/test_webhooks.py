# mypy: ignore-errors
"""Tests for the outbound webhook dispatcher (Phase 7.2).

Covers signing, subscription filtering, retry/backoff, audit integration, and
the security-alert bridge. Network calls are stubbed via the ``http_post``
injection point so the tests run hermetically.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from doctoragent.config import AegisConfig, IntegrationsConfig
from doctoragent.integrations.webhooks import (
    WEBHOOK_EVENT_WILDCARD,
    WebhookDispatcher,
    WebhookEndpoint,
    WebhookError,
    attach_security_alert_webhook,
    sign_payload,
    verify_payload,
)
from doctoragent.security.audit_log import AuditLogger

# ── Helpers ─────────────────────────────────────────────────────────────────


def _config(
    *,
    enabled: bool = True,
    endpoints: list[dict[str, Any]] | None = None,
    default_secret: str = "shared-secret",
    max_retries: int = 3,
) -> IntegrationsConfig:
    cfg = AegisConfig()
    cfg.integrations.webhooks_enabled = enabled
    cfg.integrations.webhook_endpoints = endpoints or []
    cfg.integrations.webhook_default_secret = default_secret
    cfg.integrations.webhook_max_retries = max_retries
    return cfg.integrations


def _audit(tmp_path: pytest.TempPathFactory) -> AuditLogger:
    cfg = AegisConfig()
    cfg.paths.logs = tmp_path / "logs"
    return AuditLogger(cfg, hmac_key=b"k" * 32)


class _PostRecorder:
    """Stub for the http_post injection point. Records every call."""

    def __init__(
        self,
        *,
        statuses: list[int] | None = None,
        raise_on: list[int] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._statuses = list(statuses or [])
        self._raise_on = set(raise_on or [])
        self._raise_exc = raise_exc or RuntimeError("boom")
        self._call_idx = 0

    def __call__(self, **kwargs: Any) -> Any:
        idx = self._call_idx
        self._call_idx += 1
        self.calls.append(kwargs)
        if idx in self._raise_on:
            raise self._raise_exc
        status = self._statuses[idx] if idx < len(self._statuses) else 200
        return status, ""


# ── Endpoint parsing ────────────────────────────────────────────────────────


def test_endpoint_from_config_entry_uses_default_secret() -> None:
    ep = WebhookEndpoint.from_config_entry(
        {"url": "https://hooks.example.com/cb"},
        default_secret="fallback",
    )
    assert ep.url == "https://hooks.example.com/cb"
    assert ep.secret == "fallback"
    assert ep.events == [WEBHOOK_EVENT_WILDCARD]


def test_endpoint_from_config_entry_rejects_non_http_url() -> None:
    with pytest.raises(WebhookError, match="http"):
        WebhookEndpoint.from_config_entry({"url": "ftp://x"})


def test_endpoint_from_config_entry_rejects_missing_url() -> None:
    with pytest.raises(WebhookError, match="missing 'url'"):
        WebhookEndpoint.from_config_entry({})


def test_endpoint_subscribes_to_wildcard_or_explicit() -> None:
    wildcard = WebhookEndpoint(url="https://x", events=["*"])
    explicit = WebhookEndpoint(url="https://x", events=["classified", "security_alert"])
    assert wildcard.subscribes_to("classified")
    assert wildcard.subscribes_to("anything")
    assert explicit.subscribes_to("classified")
    assert not explicit.subscribes_to("sync_round_complete")
    # Empty events list behaves as wildcard for convenience.
    empty = WebhookEndpoint(url="https://x", events=[])
    assert empty.subscribes_to("anything")


# ── Signing ─────────────────────────────────────────────────────────────────


def test_sign_and_verify_round_trip() -> None:
    body = b'{"event_id":"abc"}'
    sig = sign_payload("s3cr3t", body)
    assert sig.startswith("sha256=")
    assert verify_payload("s3cr3t", body, sig)


def test_verify_rejects_wrong_secret() -> None:
    body = b"payload"
    sig = sign_payload("right", body)
    assert not verify_payload("wrong", body, sig)


def test_verify_rejects_tampered_body() -> None:
    sig = sign_payload("s", b"original")
    assert not verify_payload("s", b"tampered", sig)


# ── Dispatcher: disabled / empty ────────────────────────────────────────────


def test_disabled_dispatcher_is_noop() -> None:
    cfg = _config(enabled=False)
    d = WebhookDispatcher(cfg)
    assert d.endpoints == []
    assert d.dispatch("classified", {"x": 1}) == 0


def test_enabled_dispatcher_with_no_endpoints_is_noop() -> None:
    cfg = _config(enabled=True, endpoints=[])
    d = WebhookDispatcher(cfg)
    assert d.dispatch("classified", {"x": 1}) == 0


# ── Dispatcher: delivery ────────────────────────────────────────────────────


def test_dispatch_delivers_to_subscribed_endpoint() -> None:
    cfg = _config(
        endpoints=[
            {"url": "https://hooks.example.com/a", "events": ["classified"], "secret": "k1"},
        ],
    )
    recorder = _PostRecorder(statuses=[200])
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda _s: None)
    n = d.dispatch("classified", {"task_id": "t1", "category": "work"})
    assert n == 1
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    # Body is canonical JSON and matches what was signed.
    body = call["body"]
    parsed = json.loads(body)
    assert parsed["event_type"] == "classified"
    assert parsed["payload"]["category"] == "work"
    # Signature header is present and verifies against the body.
    sig = call["headers"]["X-DoctorAgent-Signature"]
    assert verify_payload("k1", body, sig)
    # Auxiliary headers are set.
    assert call["headers"]["X-DoctorAgent-Event-Type"] == "classified"
    assert call["headers"]["X-DoctorAgent-Event-Id"] == parsed["event_id"]


def test_dispatch_skips_unsubscribed_endpoint() -> None:
    cfg = _config(
        endpoints=[
            {"url": "https://hooks.example.com/a", "events": ["classified"]},
            {"url": "https://hooks.example.com/b", "events": ["sync_round_complete"]},
        ],
    )
    recorder = _PostRecorder(statuses=[200, 200])
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda _s: None)
    n = d.dispatch("classified", {"x": 1})
    assert n == 1  # only one endpoint subscribed
    assert len(recorder.calls) == 1
    assert "hooks.example.com/a" in recorder.calls[0]["url"]


def test_dispatch_retries_on_5xx_then_succeeds() -> None:
    cfg = _config(
        endpoints=[{"url": "https://x", "secret": "k"}],
        max_retries=3,
    )
    recorder = _PostRecorder(statuses=[500, 502, 200])
    sleeps: list[float] = []
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda s: sleeps.append(s))
    n = d.dispatch("classified", {"x": 1})
    assert n == 1
    assert len(recorder.calls) == 3
    assert len(sleeps) == 2  # backoff between the 3 attempts
    # Exponential: 1.0, 2.0
    assert sleeps == [1.0, 2.0]
    rec = d.history[-1]
    assert rec.success is True
    assert rec.attempts == 3
    assert rec.status_code == 200


def test_dispatch_does_not_retry_on_4xx() -> None:
    cfg = _config(
        endpoints=[{"url": "https://x", "secret": "k"}],
        max_retries=3,
    )
    recorder = _PostRecorder(statuses=[422])
    sleeps: list[float] = []
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda s: sleeps.append(s))
    d.dispatch("classified", {"x": 1})
    assert len(recorder.calls) == 1  # no retry
    assert sleeps == []
    rec = d.history[-1]
    assert rec.success is False
    assert rec.status_code == 422


def test_dispatch_retries_on_429() -> None:
    cfg = _config(
        endpoints=[{"url": "https://x", "secret": "k"}],
        max_retries=2,
    )
    recorder = _PostRecorder(statuses=[429, 200])
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda _s: None)
    d.dispatch("classified", {"x": 1})
    assert len(recorder.calls) == 2
    assert d.history[-1].success is True


def test_dispatch_retries_on_network_exception() -> None:
    cfg = _config(
        endpoints=[{"url": "https://x", "secret": "k"}],
        max_retries=3,
    )
    recorder = _PostRecorder(statuses=[200], raise_on=[0, 1])
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda _s: None)
    d.dispatch("classified", {"x": 1})
    assert len(recorder.calls) == 3
    rec = d.history[-1]
    assert rec.success is True
    assert rec.attempts == 3


def test_dispatch_terminal_failure_records_error() -> None:
    cfg = _config(
        endpoints=[{"url": "https://x", "secret": "k"}],
        max_retries=2,
    )
    recorder = _PostRecorder(statuses=[503, 503])
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda _s: None)
    d.dispatch("classified", {"x": 1})
    rec = d.history[-1]
    assert rec.success is False
    assert rec.attempts == 2
    assert "503" in rec.last_error


# ── History ─────────────────────────────────────────────────────────────────


def test_history_caps_at_limit() -> None:
    cfg = _config(endpoints=[{"url": "https://x", "secret": "k"}], max_retries=1)
    recorder = _PostRecorder(statuses=[200])
    d = WebhookDispatcher(cfg, http_post=recorder, sleep=lambda _s: None)
    d._history_limit = 5  # small cap for the test
    for i in range(10):
        d.dispatch("classified", {"i": i})
    assert len(d.history) == 5


# ── Audit integration ───────────────────────────────────────────────────────


def test_successful_dispatch_emits_webhook_dispatched_audit(
    tmp_path: pytest.TempPathFactory,
) -> None:
    cfg = _config(endpoints=[{"url": "https://x", "secret": "k"}])
    audit = _audit(tmp_path)
    recorder = _PostRecorder(statuses=[200])
    d = WebhookDispatcher(cfg, audit_logger=audit, http_post=recorder, sleep=lambda _s: None)
    d.dispatch("classified", {"task_id": "t1"})
    events = audit.query(event_type="webhook_dispatched")
    assert len(events) == 1
    assert events[0]["details"]["endpoint"] == "https://x"
    assert events[0]["details"]["status_code"] == 200


def test_failed_dispatch_emits_webhook_failed_audit(
    tmp_path: pytest.TempPathFactory,
) -> None:
    cfg = _config(endpoints=[{"url": "https://x", "secret": "k"}], max_retries=1)
    audit = _audit(tmp_path)
    recorder = _PostRecorder(statuses=[503])
    d = WebhookDispatcher(cfg, audit_logger=audit, http_post=recorder, sleep=lambda _s: None)
    d.dispatch("classified", {"task_id": "t1"})
    failed = audit.query(event_type="webhook_failed")
    assert len(failed) == 1
    assert failed[0]["details"]["error"]
    dispatched = audit.query(event_type="webhook_dispatched")
    assert dispatched == []


# ── Security-alert bridge ───────────────────────────────────────────────────


def test_attach_security_alert_webhook_forwards_alerts(
    tmp_path: pytest.TempPathFactory,
) -> None:
    cfg = _config(endpoints=[{"url": "https://x", "secret": "k"}])
    audit = _audit(tmp_path)
    recorder = _PostRecorder(statuses=[200])
    d = WebhookDispatcher(cfg, audit_logger=audit, http_post=recorder, sleep=lambda _s: None)
    attach_security_alert_webhook(audit, d)
    # Trigger an alert rule: 3 failed decryptions on the same task → CRITICAL.
    for _ in range(3):
        audit.log("decrypted", {"success": False, "task_id": "t1"})
    # The alert callback fires and dispatches a "security_alert" event.
    assert len(recorder.calls) >= 1
    call = recorder.calls[0]
    parsed = json.loads(call["body"])
    assert parsed["event_type"] == "security_alert"
    assert parsed["payload"]["severity"] == "CRITICAL"
    assert parsed["payload"]["source_event"] == "decrypted"


def test_attach_security_alert_webhook_does_not_raise_on_dispatch_failure(
    tmp_path: pytest.TempPathFactory,
) -> None:
    cfg = _config(endpoints=[{"url": "https://x", "secret": "k"}], max_retries=1)
    audit = _audit(tmp_path)
    recorder = _PostRecorder(statuses=[503])
    d = WebhookDispatcher(cfg, audit_logger=audit, http_post=recorder, sleep=lambda _s: None)
    attach_security_alert_webhook(audit, d)
    # Firing the alert must not propagate the dispatch failure.
    for _ in range(3):
        audit.log("decrypted", {"success": False, "task_id": "t1"})
    # Audit still records the underlying critical alert (decryption failures).
    decrypted = audit.query(event_type="decrypted")
    assert len(decrypted) == 3


# ── Runtime endpoint management ─────────────────────────────────────────────


def test_add_endpoint_registers_at_runtime() -> None:
    cfg = _config(enabled=False)
    d = WebhookDispatcher(cfg)
    assert d.endpoints == []
    d.add_endpoint(WebhookEndpoint(url="https://runtime", secret="k"))
    assert len(d.endpoints) == 1


# ── Canonical payload stability ─────────────────────────────────────────────


def test_canonical_payload_is_key_sorted_compact() -> None:
    """The signed body must be deterministic so receivers can recompute it."""
    from doctoragent.integrations.webhooks import _canonical_payload

    body = _canonical_payload(
        {"b": 1, "a": 2, "nested": {"z": 1, "y": 2}},
    )
    # Keys are sorted, no spaces after separators.
    assert b'"a":2' in body
    assert b'"b":1' in body
    assert b'"nested":{"y":2,"z":1}' in body
    assert b", " not in body  # no space after comma

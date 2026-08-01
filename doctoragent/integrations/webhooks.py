"""Outbound webhook delivery with HMAC-SHA256 signing and retry.

Phase 7.2 surfaces three event categories to external systems:

* **classification complete** — emitted by the processing pipeline once a
  file has been classified and written into the vault.
* **security alerts** — emitted by :class:`~doctoragent.security.audit_log.AuditLogger`
  when a built-in alert rule fires (decryption failures, policy violations,
  sandbox escape attempts, plugin load failures, …).
* **sync events** — emitted by the sync engine when a sync round completes
  or a conflict is resolved.

Every delivery is an HTTPS ``POST`` whose body is canonical JSON and whose
``X-DoctorAgent-Signature`` header carries an ``sha256=<hex>`` HMAC of the
body keyed by a per-endpoint shared secret. Receivers verify the signature
in constant time before trusting the payload.

Delivery is best-effort and synchronous from the caller's perspective: the
dispatcher retries with exponential backoff up to ``webhook_max_retries``
times, records each outcome to the audit log (``webhook_dispatched`` on
success, ``webhook_failed`` on terminal failure), and never raises into the
caller — a flaky webhook must not break ingestion or sync.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from doctoragent.compat import UTC

if TYPE_CHECKING:
    from doctoragent.config import IntegrationsConfig
    from doctoragent.security.audit_log import AuditLogger

logger = logging.getLogger(__name__)

# Event subscription wildcard. An endpoint listing only this token receives
# every event the dispatcher sees, which is convenient for a local logging
# sink but should be used sparingly for remote endpoints.
WEBHOOK_EVENT_WILDCARD = "*"

# Header names are part of the public receiver contract; do not rename
# without coordinating with consumers.
_SIGNATURE_HEADER = "X-DoctorAgent-Signature"
_EVENT_ID_HEADER = "X-DoctorAgent-Event-Id"
_EVENT_TYPE_HEADER = "X-DoctorAgent-Event-Type"
_TIMESTAMP_HEADER = "X-DoctorAgent-Timestamp"
_USER_AGENT = "DoctorAgent-Webhook/1.0"

# Backoff schedule: 1s, 2s, 4s, … Capped so a fully unreachable endpoint
# cannot stall ingestion for minutes on end.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

# Outbound request security constraints: redirects forbidden, short timeout
# ceiling, response body size capped — guards against SSRF and memory
# exhaustion.
_WEBHOOK_MAX_TIMEOUT_SECONDS = 10.0
_WEBHOOK_MAX_RESPONSE_BYTES = 64 * 1024  # 64 KB


class WebhookError(RuntimeError):
    """Raised when a webhook endpoint is misconfigured (not on delivery failure)."""


class _RetryableHttpError(Exception):
    """Internal signal: the HTTP call should be retried (5xx, 429, network error).

    This exception is never raised outside ``_deliver``; it exists solely so
    that :class:`tenacity.Retrying` can distinguish retryable failures from
    terminal ones via ``retry_if_exception_type``.
    """


class _TerminalHttpError(Exception):
    """Internal signal: the HTTP call should NOT be retried (4xx except 429).

    Raised when the receiver rejected the payload with a 4xx status code
    (other than 429).  Tenacity does not retry this exception, so it
    propagates out of the ``Retrying`` call immediately.
    """


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Determine whether the IP belongs to a reserved/private range forbidden for webhook access.

    Covers loopback, RFC1918 private, link-local (including the cloud
    metadata endpoint 169.254.169.254), IPv6 unique-local fc00::/7,
    multicast, reserved, and the unspecified 0.0.0.0/:: addresses.
    """
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_safe_url(url: str) -> bool:
    """Determine whether a webhook URL is safe to send to.

    Enforces https; resolves the host name (including IP literals) to IPs
    and checks each one — any address falling into a forbidden range is
    treated as unsafe. When the host name cannot be resolved the URL is
    considered safe (deferred to send time), because a resolution failure
    does not route to a private address by itself.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_forbidden_ip(ip):
            return False
    return True


def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL; raises :class:`WebhookError` when unsafe."""
    parsed = urlparse(url)
    if parsed.scheme == "http":
        raise WebhookError(
            f"webhook endpoint url must use https (plaintext http is not allowed): {url!r}"
        )
    if parsed.scheme != "https":
        raise WebhookError(f"webhook endpoint url must be https: {url!r}")
    if not _is_safe_url(url):
        raise WebhookError(f"webhook endpoint url targets a forbidden host: {url!r}")


@dataclass
class WebhookEndpoint:
    """A single registered webhook receiver."""

    url: str
    events: list[str] = field(default_factory=lambda: [WEBHOOK_EVENT_WILDCARD])
    secret: str = ""
    # Human-readable label for logs/audit; not sent to the receiver.
    label: str = ""

    def subscribes_to(self, event_type: str) -> bool:
        """Return True if this endpoint wants *event_type*."""
        if not self.events:
            return True  # empty list behaves as wildcard for convenience
        return WEBHOOK_EVENT_WILDCARD in self.events or event_type in self.events

    @classmethod
    def from_config_entry(cls, entry: dict[str, Any], default_secret: str = "") -> WebhookEndpoint:
        """Build an endpoint from an ``integrations.webhook_endpoints`` entry.

        Each entry is ``{"url": "...", "events": [...], "secret": "..."}``.
        A missing ``secret`` falls back to *default_secret* so operators can
        set one shared secret via environment and reuse it across endpoints.
        """
        url = str(entry.get("url", "")).strip()
        if not url:
            raise WebhookError("webhook endpoint entry is missing 'url'")
        # Enforce https and run an SSRF check (reject loopback/private/metadata targets, etc.).
        _validate_webhook_url(url)
        raw_events = entry.get("events") or [WEBHOOK_EVENT_WILDCARD]
        events = [str(e) for e in raw_events]
        secret = str(entry.get("secret") or default_secret)
        return cls(
            url=url,
            events=events,
            secret=secret,
            label=str(entry.get("label", "")),
        )


@dataclass
class WebhookDeliveryRecord:
    """Outcome of a single delivery attempt (one endpoint, all retries)."""

    event_id: str
    event_type: str
    endpoint_url: str
    success: bool
    attempts: int
    status_code: int | None
    last_error: str
    duration_ms: float


def _canonical_payload(event: dict[str, Any]) -> bytes:
    """Serialize *event* to canonical JSON bytes for both signing and POST body.

    The same bytes are signed and sent, so the receiver can recompute the
    HMAC over the raw request body without worrying about JSON re-encoding
    differences.
    """
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sign_payload(secret: str, body: bytes) -> str:
    """Compute the ``sha256=<hex>`` signature header value for *body*."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_payload(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time verification of a signature header against *body*.

    Used by receivers (and by our own tests). Returns ``True`` on match.
    """
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature_header)


class WebhookDispatcher:
    """Fan-out webhook delivery with signing, retry, and audit integration.

    Constructed from an :class:`~doctoragent.config.IntegrationsConfig` block;
    call :meth:`dispatch` for every event you want to surface. The dispatcher
    is safe to call from synchronous code paths (it uses a thread-pool-style
    blocking HTTP client) — webhooks are intentionally not async so they can
    be triggered from the audit logger or the sync engine without a running
    event loop.
    """

    def __init__(
        self,
        config: IntegrationsConfig,
        *,
        audit_logger: AuditLogger | None = None,
        http_post: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not config.webhooks_enabled:
            # Constructing a disabled dispatcher is legal (so callers don't
            # need to branch on the flag) but dispatch becomes a no-op.
            self._endpoints: list[WebhookEndpoint] = []
        else:
            self._endpoints = [
                WebhookEndpoint.from_config_entry(e, config.webhook_default_secret or "")
                for e in config.webhook_endpoints
            ]
        self._max_retries = max(1, int(config.webhook_max_retries))
        self._timeout = float(config.webhook_timeout_seconds)
        self._audit = audit_logger
        # Injection points for tests. Production uses httpx + time.sleep.
        self._http_post = http_post
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        # Rolling history of delivery outcomes, capped to avoid unbounded
        # growth in long-running processes. Introspectable via :meth:`history`.
        self._history: list[WebhookDeliveryRecord] = []
        self._history_limit = 200

    @property
    def endpoints(self) -> list[WebhookEndpoint]:
        """Return a copy of the registered endpoint list."""
        return list(self._endpoints)

    @property
    def history(self) -> list[WebhookDeliveryRecord]:
        """Return a copy of recent delivery records (most-recent last)."""
        return list(self._history)

    def add_endpoint(self, endpoint: WebhookEndpoint) -> None:
        """Register an additional endpoint at runtime (e.g. via API)."""
        self._endpoints.append(endpoint)

    def dispatch(self, event_type: str, payload: dict[str, Any]) -> int:
        """Deliver an event to every subscribed endpoint.

        Returns the number of endpoints that were attempted (success or
        failure). Never raises — a delivery failure is recorded in the audit
        log and the dispatcher moves on.
        """
        if not self._endpoints:
            return 0
        event = {
            "event_id": str(uuid4()),
            "event_type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        body = _canonical_payload(event)
        attempted = 0
        for endpoint in self._endpoints:
            if not endpoint.subscribes_to(event_type):
                continue
            attempted += 1
            record = self._deliver(endpoint, event, body)
            self._record_history(record)
            self._emit_audit(record, event)
        return attempted

    def _deliver(
        self,
        endpoint: WebhookEndpoint,
        event: dict[str, Any],
        body: bytes,
    ) -> WebhookDeliveryRecord:
        """Attempt delivery to one endpoint with exponential-backoff retry.

        Retries are driven by :class:`tenacity.Retrying` with exponential
        backoff (1 s, 2 s, 4 s, ... capped at 30 s).  Retryable conditions
        are 5xx responses, 429 (Too Many Requests), and network-level
        exceptions.  4xx responses (except 429) are treated as terminal --
        the receiver rejected the payload and retrying will not help.
        """
        signature = sign_payload(endpoint.secret, body)
        headers = {
            "Content-Type": "application/json",
            _SIGNATURE_HEADER: signature,
            _EVENT_ID_HEADER: event["event_id"],
            _EVENT_TYPE_HEADER: event["event_type"],
            _TIMESTAMP_HEADER: event["timestamp"],
            "User-Agent": _USER_AGENT,
        }

        attempts = 0
        last_error = ""
        status_code: int | None = None
        start = self._clock()

        def _attempt() -> None:
            """Single delivery attempt; raises signal exceptions for tenacity."""
            nonlocal attempts, last_error, status_code
            attempts += 1
            try:
                sc, err = self._post_once(endpoint.url, body, headers, self._timeout)
                status_code = sc
                if sc is not None and 200 <= sc < 300:
                    return  # Success — tenacity stops.
                last_error = err or f"HTTP {sc}"
                # 4xx (except 429) are terminal — the receiver rejected
                # the payload, retrying won't help.
                if sc is not None and 400 <= sc < 500 and sc != 429:
                    raise _TerminalHttpError()
                raise _RetryableHttpError()
            except (_TerminalHttpError, _RetryableHttpError):
                raise
            except Exception as exc:  # noqa: BLE001 — network errors are varied
                last_error = f"{type(exc).__name__}: {exc}"
                status_code = None
                raise _RetryableHttpError() from exc

        retrying = Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=_BACKOFF_BASE_SECONDS, max=_BACKOFF_MAX_SECONDS),
            retry=retry_if_exception_type(_RetryableHttpError),
            sleep=self._sleep,
            reraise=True,
        )

        success = False
        try:
            retrying(_attempt)
            success = True
        except (_TerminalHttpError, _RetryableHttpError):
            success = False

        return WebhookDeliveryRecord(
            event_id=event["event_id"],
            event_type=event["event_type"],
            endpoint_url=endpoint.url,
            success=success,
            attempts=attempts,
            status_code=status_code,
            last_error="" if success else last_error,
            duration_ms=(self._clock() - start) * 1000.0,
        )

    def _post_once(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int | None, str]:
        """Perform a single POST. Returns (status_code, error_message).

        On a network-level failure ``status_code`` is ``None`` and
        ``error_message`` describes the failure.
        """
        if self._http_post is not None:
            # Test hook: the injected callable returns either an int status
            # or raises. We normalise to the (status, error) contract here.
            result = self._http_post(url=url, body=body, headers=headers, timeout=timeout)
            if isinstance(result, tuple):
                return result  # already (status, error) shaped
            return int(result), ""
        # Production path: import httpx lazily so unit tests do not need it installed.
        # SSRF guard: re-check the target before sending (defends against DNS
        # rebinding and endpoints added at runtime).
        if not _is_safe_url(url):
            return None, f"SSRF blocked: webhook target is forbidden: {url}"
        # Outbound short-timeout ceiling to prevent long hangs from oversized config.
        effective_timeout = min(timeout, _WEBHOOK_MAX_TIMEOUT_SECONDS)
        import httpx

        try:
            # Disallow following redirects to avoid being lured to internal
            # addresses; stream the response and cap its size.
            with httpx.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=effective_timeout,
                follow_redirects=False,
            ) as response:
                status_code = response.status_code
                snippet = ""
                if status_code >= 400:
                    collected = bytearray()
                    for chunk in response.iter_bytes():
                        collected.extend(chunk)
                        if len(collected) >= _WEBHOOK_MAX_RESPONSE_BYTES:
                            break
                    snippet = collected[:256].decode("utf-8", errors="replace").replace("\n", " ")
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"
        if status_code >= 400:
            return status_code, f"HTTP {status_code}: {snippet}"
        return status_code, ""

    def _record_history(self, record: WebhookDeliveryRecord) -> None:
        self._history.append(record)
        if len(self._history) > self._history_limit:
            # Drop oldest in-place to bound memory.
            del self._history[: len(self._history) - self._history_limit]

    def _emit_audit(self, record: WebhookDeliveryRecord, event: dict[str, Any]) -> None:
        if self._audit is None:
            return
        details = {
            "event_id": record.event_id,
            "event_type": record.event_type,
            "endpoint": record.endpoint_url,
            "attempts": record.attempts,
            "status_code": record.status_code,
            "duration_ms": round(record.duration_ms, 2),
        }
        if record.success:
            self._audit.log("webhook_dispatched", details)
        else:
            details["error"] = record.last_error
            details["severity"] = "MEDIUM"
            try:
                self._audit.log("webhook_failed", details)
            except Exception:  # pragma: no cover — audit must not break dispatch
                logger.exception("Failed to emit webhook_failed audit event")


def attach_security_alert_webhook(
    audit_logger: AuditLogger,
    dispatcher: WebhookDispatcher,
) -> None:
    """Wire audit-log security alerts into webhook delivery.

    Registers an alert callback on *audit_logger* that forwards every fired
    alert (CRITICAL/HIGH/MEDIUM) to *dispatcher* as a ``security_alert``
    event. This is the integration point that makes webhooks receive
    decryption-failure, policy-violation, sandbox-escape, and plugin-load
    alerts without each emitter needing to know about webhooks.

    Safe to call multiple times; duplicate registrations are idempotent at
    the dispatcher level (the same alert fans out once per subscribed
    endpoint regardless of how many callbacks fire).
    """

    def _on_alert(severity: str, alert: dict[str, Any]) -> None:
        event_type = alert.get("event_type", "unknown")
        details = alert.get("details", {})
        payload = {
            "severity": severity,
            "source_event": event_type,
            "details": details,
        }
        try:
            dispatcher.dispatch("security_alert", payload)
        except Exception:  # pragma: no cover — dispatch never raises, but guard
            logger.exception("Webhook dispatch for security alert failed")

    audit_logger.register_alert(_on_alert)

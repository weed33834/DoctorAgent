"""API hardening middleware: per-token rate limiting and request size caps.

This module implements a small, dependency-free **token-bucket rate limiter**
plus a **request body size guard**, exposed as Starlette middleware so they
apply uniformly to every route without touching individual endpoint signatures.

Why not ``slowapi``?
``slowapi`` is an excellent library, but it is an optional dependency that is
not part of the project's install set.  Adding it would widen the dependency
footprint and risk CI breakage on minimal installs.  The token-bucket
implementation here is ~60 lines, thread-safe, and covers the requirements
(100 req/min default, 10 req/min for sensitive operations, 429 + Retry-After).

Rate-limit model
----------------
* Limits are applied **per API token** (the bearer token from the
  ``Authorization`` header).  Unauthenticated / local requests are bucketed by
  client IP so a single misbehaving loopback client cannot exhaust a shared
  bucket.
* Two profiles exist:
    - ``default``  — 100 requests / minute (read & general endpoints).
    - ``sensitive`` — 10 requests / minute (delete / config / classify /
      agent / sync-trigger / audit-export / backup / webhooks-test /
      inbox-submit / tenant-create / connection mutations).
* When a bucket is exhausted the middleware short-circuits with HTTP 429 and a
  ``Retry-After`` header (seconds until the next token is available).

Request size guard
------------------
Requests whose ``Content-Length`` exceeds ``max_body_bytes`` (default 50 MiB)
are rejected with HTTP 413 *before* the body is read, protecting the server
from memory-exhaustion attempts.  ``/docs``, ``/openapi.json`` and the
audit-export / file-download streaming responses are unaffected (they are
responses, not request bodies).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Default quotas (requests per minute).
DEFAULT_RPM: int = 100
SENSITIVE_RPM: int = 10

# Default request body size cap: 50 MiB.
DEFAULT_MAX_BODY_BYTES: int = 50 * 1024 * 1024

# Paths (by suffix/segment match) treated as sensitive for rate-limiting.
# Matching is intentionally conservative: a path is sensitive if it contains
# any of these segments, so ``/api/v1/config`` and ``/config`` both match.
_SENSITIVE_SEGMENTS: tuple[str, ...] = (
    "/vault/classify",
    "/vault/agent",
    "/sync/trigger",
    "/audit/export",
    "/backup/remote",
    "/webhooks/test",
    "/inbox/submit",
    "/tenants",  # POST /tenants is sensitive; GET is not, but a single
    # slightly-tighter bucket for tenant reads is harmless and simpler than
    # splitting by method.
    "/config",
)

# Sensitive *method+segment* combos where the segment alone is ambiguous
# (e.g. GET /connections is read-only but POST/DELETE are sensitive).  We
# encode these as (method-prefix, segment) tuples.
_SENSITIVE_METHOD_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("POST", "/connections"),
    ("DELETE", "/connections"),
    ("/connections/", "/test"),  # POST /connections/{id}/test
    ("POST", "/vault/files/batch"),
    ("POST", "/inbox/submit/batch"),
)


def _is_sensitive(method: str, path: str) -> bool:
    """Return ``True`` if *method*+*path* should use the sensitive quota."""
    if any(seg in path for seg in _SENSITIVE_SEGMENTS):
        return True
    for m, seg in _SENSITIVE_METHOD_SEGMENTS:
        if m.startswith("/"):
            # Both the path prefix (m) and the trailing segment (seg) must be
            # present — e.g. ``("/connections/", "/test")`` matches
            # ``POST /connections/{id}/test`` but NOT ``/api/v1/test``.
            if m in path and seg in path:
                return True
        elif method.upper() == m and seg in path:
            return True
    return False


class _TokenBucket:
    """A minimal token-bucket with monotonic-clock refill."""

    __slots__ = ("capacity", "refill_per_second", "_tokens", "_last", "_lock")

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, amount: float = 1.0) -> tuple[bool, float]:
        """Try to consume *amount* tokens.

        Returns ``(allowed, retry_after_seconds)``.  ``retry_after_seconds`` is
        ``0.0`` when allowed, otherwise the wait until enough tokens refill.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._last = now
            if self._tokens >= amount:
                self._tokens -= amount
                return True, 0.0
            deficit = amount - self._tokens
            retry = deficit / self.refill_per_second if self.refill_per_second > 0 else 1.0
            return False, retry


class RateLimiter:
    """Thread-safe registry of per-key token buckets.

    Buckets are created lazily on first use and never explicitly evicted; in a
    long-running server they could grow with the number of distinct tokens, but
    each bucket is tiny (~100 bytes) and the API token space is small in
    practice.  Call :meth:`reset` between tests to isolate state.
    """

    def __init__(
        self,
        *,
        default_rpm: int = DEFAULT_RPM,
        sensitive_rpm: int = SENSITIVE_RPM,
    ) -> None:
        self._default_rpm = default_rpm
        self._sensitive_rpm = sensitive_rpm
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, sensitive: bool) -> tuple[bool, float, int]:
        """Check the bucket for *key*.

        Returns ``(allowed, retry_after_seconds, limit_rpm)``.
        """
        profile = "sensitive" if sensitive else "default"
        rpm = self._sensitive_rpm if sensitive else self._default_rpm
        cache_key = (profile, key)
        with self._lock:
            bucket = self._buckets.get(cache_key)
            if bucket is None:
                # Capacity = rpm (burst up to a full minute's quota); refill
                # spreads the quota evenly across the minute.
                bucket = _TokenBucket(capacity=float(rpm), refill_per_second=rpm / 60.0)
                self._buckets[cache_key] = bucket
        allowed, retry = bucket.consume(1.0)
        return allowed, retry, rpm

    def reset(self) -> None:
        """Drop all buckets (used to isolate test state)."""
        with self._lock:
            self._buckets.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "default_rpm": self._default_rpm,
                "sensitive_rpm": self._sensitive_rpm,
                "tracked_keys": len(self._buckets),
            }


# ---------------------------------------------------------------------------
# Starlette middleware factories
# ---------------------------------------------------------------------------

# Type alias for an ASGI app.
ASGIApp = Callable[..., Awaitable[Any]]


def _extract_identity(scope: dict[str, Any]) -> str:
    """Derive a rate-limit key from the request (token > client IP > 'anon')."""
    # Prefer the bearer token so limits are truly per-API-token.
    for raw in scope.get("headers", []):
        if len(raw) != 2:
            continue
        name = raw[0]
        if name == b"authorization":
            value = raw[1].decode("latin-1", errors="ignore")
            if value.lower().startswith("bearer "):
                return "token:" + value[7:].strip()
            return "auth:" + value[:32]
    client = scope.get("client")
    if client:
        host = client.get("host") if isinstance(client, dict) else getattr(client, "host", None)
        if host:
            return "ip:" + host
    return "anon"


def _scope_path(scope: dict[str, Any]) -> str:
    return scope.get("path", "") or ""


def _is_http(scope: dict[str, Any]) -> bool:
    return scope.get("type") == "http"


class RateLimitMiddleware:
    """ASGI middleware applying :class:`RateLimiter` to HTTP requests.

    WebSocket connections and the documentation endpoints (``/docs``,
    ``/openapi.json``, ``/redoc``) bypass the limiter so the interactive docs
    and real-time channels are never starved.
    """

    _BYPASS_PREFIXES: tuple[str, ...] = ("/docs", "/redoc", "/openapi.json")

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter | None = None,
        *,
        default_rpm: int = DEFAULT_RPM,
        sensitive_rpm: int = SENSITIVE_RPM,
    ) -> None:
        self.app = app
        self.limiter = limiter or RateLimiter(default_rpm=default_rpm, sensitive_rpm=sensitive_rpm)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not _is_http(scope):
            await self.app(scope, receive, send)
            return
        path = _scope_path(scope)
        if any(path == p or path.startswith(p) for p in self._BYPASS_PREFIXES):
            await self.app(scope, receive, send)
            return
        if path == "/health" or path.endswith("/health"):
            # Health probes should never be rate-limited (k8s liveness checks).
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        key = _extract_identity(scope)
        sensitive = _is_sensitive(method, path)
        allowed, retry, limit = self.limiter.check(key, sensitive=sensitive)
        if not allowed:
            await self._send_429(send, retry, limit, sensitive)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_429(send: Any, retry: float, limit: int, sensitive: bool) -> None:
        import json

        retry_int = max(1, int(retry) + 1)
        body = json.dumps(
            {
                "detail": "Rate limit exceeded",
                "limit_rpm": limit,
                "profile": "sensitive" if sensitive else "default",
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_int).encode("ascii")),
                    (b"x-ratelimit-limit", str(limit).encode("ascii")),
                    (b"x-ratelimit-remaining", b"0"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class RequestSizeLimitMiddleware:
    """ASGI middleware rejecting oversized HTTP request bodies (HTTP 413)."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not _is_http(scope):
            await self.app(scope, receive, send)
            return
        # Only enforce on methods that carry a body.
        method = scope.get("method", "GET").upper()
        if method in ("POST", "PUT", "PATCH"):
            cl = 0
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    try:
                        cl = int(value.decode("ascii"))
                    except ValueError:
                        cl = 0
                    break
            if cl and cl > self.max_body_bytes:
                await self._send_413(send, self.max_body_bytes)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_413(send: Any, limit: int) -> None:
        import json

        body = json.dumps({"detail": f"Request body too large (limit {limit} bytes)"}).encode(
            "utf-8"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

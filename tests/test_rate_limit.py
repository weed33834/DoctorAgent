"""Tests for the API rate-limiting and request-size middleware.

Covers the token-bucket math, the sensitive-path classifier, per-token
identity extraction, the 429/413 ASGI responses, and the bypass prefixes.
These are production-critical: a regression here either lets a single
client exhaust server resources (no limit) or blocks legitimate traffic
(false 429).
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from doctoragent.api.rate_limit import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_RPM,
    SENSITIVE_RPM,
    RateLimiter,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    _TokenBucket,
    _extract_identity,
    _is_sensitive,
    _scope_path,
)

# ---------------------------------------------------------------------------
# _TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_consume_within_capacity_allowed(self) -> None:
        bucket = _TokenBucket(capacity=5, refill_per_second=1.0)
        for _ in range(5):
            allowed, retry = bucket.consume()
            assert allowed is True
            assert retry == 0.0

    def test_consume_beyond_capacity_denied_with_retry(self) -> None:
        bucket = _TokenBucket(capacity=1, refill_per_second=1.0)
        bucket.consume()  # exhaust
        allowed, retry = bucket.consume()
        assert allowed is False
        assert retry > 0.0

    def test_refill_after_wait(self) -> None:
        bucket = _TokenBucket(capacity=1, refill_per_second=100.0)
        bucket.consume()  # exhaust
        time.sleep(0.05)  # 50ms → ~5 tokens refilled at 100/s
        allowed, _ = bucket.consume()
        assert allowed is True

    def test_capacity_caps_refill(self) -> None:
        bucket = _TokenBucket(capacity=3, refill_per_second=1000.0)
        time.sleep(0.01)  # would refill 10 tokens, but capped at 3
        for _ in range(3):
            assert bucket.consume()[0] is True
        assert bucket.consume()[0] is False

    def test_zero_refill_rate_never_replenishes(self) -> None:
        bucket = _TokenBucket(capacity=1, refill_per_second=0.0)
        bucket.consume()
        allowed, retry = bucket.consume()
        assert allowed is False
        # Guard against division by zero.
        assert retry == 1.0

    def test_thread_safe_concurrent_consume(self) -> None:
        # Concurrent consumes must not corrupt state or over-allow.
        import threading

        bucket = _TokenBucket(capacity=100, refill_per_second=0.0)
        allowed_count = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal allowed_count
            for _ in range(50):
                a, _ = bucket.consume()
                with lock:
                    if a:
                        allowed_count += 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exactly capacity (100) tokens consumed, never more.
        assert allowed_count == 100


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_default_profile_allows_within_rpm(self) -> None:
        limiter = RateLimiter(default_rpm=5)
        for _ in range(5):
            allowed, _, limit = limiter.check("key1", sensitive=False)
            assert allowed is True
        assert limit == 5

    def test_sensitive_profile_uses_lower_rpm(self) -> None:
        limiter = RateLimiter(default_rpm=100, sensitive_rpm=2)
        for _ in range(2):
            allowed, _, _ = limiter.check("key1", sensitive=True)
            assert allowed is True
        allowed, _, _ = limiter.check("key1", sensitive=True)
        assert allowed is False

    def test_separate_buckets_per_key(self) -> None:
        limiter = RateLimiter(default_rpm=1)
        limiter.check("token-A", sensitive=False)
        # Token B has its own bucket.
        allowed, _, _ = limiter.check("token-B", sensitive=False)
        assert allowed is True

    def test_separate_buckets_per_profile_same_key(self) -> None:
        limiter = RateLimiter(default_rpm=1, sensitive_rpm=1)
        limiter.check("key", sensitive=False)  # exhaust default bucket
        # Sensitive bucket is independent.
        allowed, _, _ = limiter.check("key", sensitive=True)
        assert allowed is True

    def test_reset_clears_all_buckets(self) -> None:
        limiter = RateLimiter(default_rpm=1)
        limiter.check("key", sensitive=False)
        assert limiter.stats()["tracked_keys"] == 1
        limiter.reset()
        assert limiter.stats()["tracked_keys"] == 0
        # After reset, the key can consume again.
        assert limiter.check("key", sensitive=False)[0] is True

    def test_stats_reports_config(self) -> None:
        limiter = RateLimiter(default_rpm=42, sensitive_rpm=7)
        stats = limiter.stats()
        assert stats["default_rpm"] == 42
        assert stats["sensitive_rpm"] == 7
        assert stats["tracked_keys"] == 0


# ---------------------------------------------------------------------------
# _is_sensitive classifier
# ---------------------------------------------------------------------------


class TestIsSensitive:
    @pytest.mark.parametrize(
        "method,path,expected",
        [
            # Default (non-sensitive) paths.
            ("GET", "/api/v1/vault/files", False),
            ("GET", "/api/v1/vault/search", False),
            ("GET", "/api/v1/connections", False),
            ("GET", "/api/v1/audit/logs", False),
            # Sensitive segments.
            ("GET", "/api/v1/config", True),
            ("POST", "/api/v1/config", True),
            ("GET", "/api/v1/vault/agent", True),
            ("POST", "/api/v1/sync/trigger", True),
            ("GET", "/api/v1/audit/export", True),
            ("POST", "/api/v1/vault/classify", True),
            ("POST", "/api/v1/tenants", True),
            # Sensitive method+segment combos.
            ("POST", "/api/v1/connections", True),
            ("DELETE", "/api/v1/connections/c1", True),
            ("POST", "/api/v1/connections/c1/test", True),
            ("POST", "/api/v1/vault/files/batch", True),
            ("POST", "/api/v1/inbox/submit/batch", True),
        ],
    )
    def test_classifier(self, method: str, path: str, expected: bool) -> None:
        assert _is_sensitive(method, path) is expected


# ---------------------------------------------------------------------------
# _extract_identity
# ---------------------------------------------------------------------------


class TestExtractIdentity:
    def test_prefers_bearer_token(self) -> None:
        scope: dict[str, Any] = {
            "headers": [(b"authorization", b"Bearer sk-secret-token")],
            "client": {"host": "1.2.3.4"},
        }
        assert _extract_identity(scope) == "token:sk-secret-token"

    def test_falls_back_to_client_ip(self) -> None:
        scope: dict[str, Any] = {
            "headers": [],
            "client": {"host": "10.0.0.1"},
        }
        assert _extract_identity(scope) == "ip:10.0.0.1"

    def test_anon_when_nothing_available(self) -> None:
        scope: dict[str, Any] = {"headers": [], "client": None}
        assert _extract_identity(scope) == "anon"

    def test_non_bearer_auth_uses_auth_prefix(self) -> None:
        scope: dict[str, Any] = {
            "headers": [(b"authorization", b"Basic dXNlcjpwYXNz")],
            "client": None,
        }
        identity = _extract_identity(scope)
        assert identity.startswith("auth:")


# ---------------------------------------------------------------------------
# RateLimitMiddleware (ASGI)
# ---------------------------------------------------------------------------


async def _ok_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app that returns 200 OK."""
    body = b"ok"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _make_request(
    method: str = "GET",
    path: str = "/api/v1/test",
    headers: list[tuple[bytes, bytes]] | None = None,
    content_length: int | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": {"host": "127.0.0.1"},
    }
    if content_length is not None:
        scope["headers"].append((b"content-length", str(content_length).encode()))
    return scope


async def _capture_response(
    middleware: Any, scope: dict[str, Any]
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Run *middleware* with *scope* and capture the response."""
    status: list[int] = []
    headers: list[tuple[bytes, bytes]] = []
    body_parts: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])
            headers.extend(message.get("headers", []))
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await middleware(scope, receive, send)
    header_dict = {k: v for k, v in headers}
    return status[0] if status else 0, header_dict, b"".join(body_parts)


class TestRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_bypasses_docs(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=0)
        status, _, _ = await _capture_response(mw, _make_request(path="/docs"))
        assert status == 200

    @pytest.mark.asyncio
    async def test_bypasses_openapi(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=0)
        status, _, _ = await _capture_response(mw, _make_request(path="/openapi.json"))
        assert status == 200

    @pytest.mark.asyncio
    async def test_bypasses_health(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=0)
        status, _, _ = await _capture_response(mw, _make_request(path="/health"))
        assert status == 200

    @pytest.mark.asyncio
    async def test_bypasses_lifespan(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=0)
        # Non-HTTP scope passes through.
        scope: dict[str, Any] = {"type": "lifespan", "headers": []}
        status, _, _ = await _capture_response(mw, scope)
        assert status == 200

    @pytest.mark.asyncio
    async def test_allows_within_limit(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=5)
        for _ in range(5):
            status, _, _ = await _capture_response(mw, _make_request())
            assert status == 200

    @pytest.mark.asyncio
    async def test_returns_429_when_exhausted(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=1)
        await _capture_response(mw, _make_request())  # exhaust
        status, headers, body = await _capture_response(mw, _make_request())
        assert status == 429
        assert b"retry-after" in headers
        assert b"x-ratelimit-limit" in headers
        assert b"x-ratelimit-remaining" in headers
        assert headers[b"x-ratelimit-remaining"] == b"0"
        assert b"no-store" == headers.get(b"cache-control")
        data = json.loads(body)
        assert data["detail"] == "Rate limit exceeded"
        assert data["limit_rpm"] == 1

    @pytest.mark.asyncio
    async def test_sensitive_path_uses_lower_limit(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=100, sensitive_rpm=1)
        await _capture_response(mw, _make_request(path="/api/v1/config"))  # exhaust
        status, _, body = await _capture_response(mw, _make_request(path="/api/v1/config"))
        assert status == 429
        data = json.loads(body)
        assert data["profile"] == "sensitive"
        assert data["limit_rpm"] == 1

    @pytest.mark.asyncio
    async def test_per_token_bucketing(self) -> None:
        mw = RateLimitMiddleware(_ok_app, default_rpm=1)
        # Token A exhausts its bucket.
        await _capture_response(
            mw, _make_request(headers=[(b"authorization", b"Bearer token-A")])
        )
        # Token B is unaffected.
        status, _, _ = await _capture_response(
            mw, _make_request(headers=[(b"authorization", b"Bearer token-B")])
        )
        assert status == 200


# ---------------------------------------------------------------------------
# RequestSizeLimitMiddleware
# ---------------------------------------------------------------------------


class TestRequestSizeLimitMiddleware:
    @pytest.mark.asyncio
    async def test_get_passthrough(self) -> None:
        mw = RequestSizeLimitMiddleware(_ok_app, max_body_bytes=10)
        status, _, _ = await _capture_response(mw, _make_request(method="GET"))
        assert status == 200

    @pytest.mark.asyncio
    async def test_post_within_limit_allowed(self) -> None:
        mw = RequestSizeLimitMiddleware(_ok_app, max_body_bytes=100)
        status, _, _ = await _capture_response(
            mw, _make_request(method="POST", content_length=50)
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_post_over_limit_returns_413(self) -> None:
        mw = RequestSizeLimitMiddleware(_ok_app, max_body_bytes=100)
        status, headers, body = await _capture_response(
            mw, _make_request(method="POST", content_length=200)
        )
        assert status == 413
        data = json.loads(body)
        assert "too large" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_malformed_content_length_passes_through(self) -> None:
        # A non-numeric Content-Length is treated as 0 → not rejected.
        mw = RequestSizeLimitMiddleware(_ok_app, max_body_bytes=1)
        scope = _make_request(method="POST")
        scope["headers"] = [(b"content-length", b"not-a-number")]
        status, _, _ = await _capture_response(mw, scope)
        assert status == 200

    @pytest.mark.asyncio
    async def test_default_limit_is_50mib(self) -> None:
        assert DEFAULT_MAX_BODY_BYTES == 50 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_patch_enforced(self) -> None:
        mw = RequestSizeLimitMiddleware(_ok_app, max_body_bytes=10)
        status, _, _ = await _capture_response(
            mw, _make_request(method="PATCH", content_length=100)
        )
        assert status == 413

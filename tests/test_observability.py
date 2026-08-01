"""Tests for the doctoragent.observability package.

These tests exercise both the rich path (structlog / prometheus-client /
opentelemetry installed) and the graceful fallback path (simulated by
flipping the module-level availability flags). The package must never
raise ``ImportError`` and every public helper must remain callable when an
optional backing library is absent.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from doctoragent.observability import (
    configure_logging,
    configure_tracing,
    generate_latest_metrics,
    get_metrics,
    get_tracer,
    instrument_app,
)
from doctoragent.observability import logging as obs_logging
from doctoragent.observability import metrics as obs_metrics
from doctoragent.observability import tracing as obs_tracing

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("debug", [True, False])
@pytest.mark.parametrize("json_output", [True, False])
def test_configure_logging_structlog_path_does_not_raise(
    debug: bool, json_output: bool
) -> None:
    """configure_logging with structlog available never raises."""
    configure_logging(debug=debug, json_output=json_output)


def test_configure_logging_stdlib_fallback_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When structlog is reported unavailable, the stdlib fallback is used."""
    monkeypatch.setattr(obs_logging, "_STRUCTLOG_AVAILABLE", False)
    # Snapshot and restore root handler state so the test is isolated.
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        configure_logging(debug=False)
        # basicConfig is idempotent; the key assertion is no exception. With
        # pre-existing handlers (pytest's) basicConfig leaves them alone.
        assert isinstance(root.handlers, list)
    finally:
        root.handlers = saved_handlers


def test_configure_logging_bridges_stdlib_getlogger() -> None:
    """A stdlib ``logging.getLogger`` call flows through the configured chain."""
    child = logging.getLogger("doctoragent._observability_test")
    records: list[logging.LogRecord] = []

    class _CollectingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CollectingHandler()
    child.addHandler(handler)
    child.setLevel(logging.DEBUG)
    try:
        configure_logging(debug=True)
        child.info("bridged message %s", "ok")
    finally:
        child.removeHandler(handler)
    assert any("bridged message" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_generate_latest_metrics_returns_bytes() -> None:
    """generate_latest_metrics always returns bytes (empty when prom missing)."""
    out = generate_latest_metrics()
    assert isinstance(out, bytes)


def test_generate_latest_metrics_has_content_when_prometheus_available() -> None:
    """When prometheus_client is installed the exposition is non-empty."""
    if not obs_metrics._PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    assert len(generate_latest_metrics()) > 0


def test_counter_inc_reflected_in_get_metrics() -> None:
    """A Counter.inc() is observable through get_metrics()."""
    # Use a unique path so the assertion is robust against other tests that
    # may have incremented the same counter in the shared process registry.
    unique = "obs-test-" + str(id(object()))
    obs_metrics.doctoragent_http_requests_total.labels(
        method="GET", path=unique, status="200"
    ).inc()
    snapshot = get_metrics()
    assert "doctoragent_http_requests_total" in snapshot
    matched = [
        s
        for s in snapshot["doctoragent_http_requests_total"]
        if s["labels"].get("path") == unique
    ]
    assert matched, "inc'd label set must appear in the metrics snapshot"
    assert matched[0]["value"] >= 1.0


def test_get_metrics_returns_dict() -> None:
    """get_metrics returns a dict mapping metric name -> samples."""
    snapshot = get_metrics()
    assert isinstance(snapshot, dict)


def test_metrics_noop_fallback_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """When prometheus is unavailable the no-op stubs still record values."""
    # Build a fresh no-op counter directly so we don't disturb the shared
    # prometheus registry used elsewhere in the test session.
    counter = obs_metrics._NoopCounter(
        "doctoragent_test_fallback_counter", "help", ("method",)
    )
    counter.labels(method="GET").inc()
    counter.labels(method="GET").inc(2.0)
    info = counter.describe()
    assert info["name"] == "doctoragent_test_fallback_counter"
    sample = info["samples"][0]
    assert sample["labels"] == {"method": "GET"}
    assert sample["value"] == 3.0


def test_histogram_observe_noop_fallback() -> None:
    """The no-op Histogram stub records count + sum on observe()."""
    hist = obs_metrics._NoopHistogram("doctoragent_test_hist", "help", ("path",))
    hist.labels(path="/x").observe(0.5)
    hist.labels(path="/x").observe(1.5)
    info = hist.describe()
    sample = info["samples"][0]
    assert sample["labels"] == {"path": "/x"}
    assert sample["value"] == 2.0  # sum
    assert sample["count"] == 2


def test_generate_latest_metrics_empty_when_prometheus_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_latest_metrics returns empty bytes without prometheus_client."""
    monkeypatch.setattr(obs_metrics, "_PROMETHEUS_AVAILABLE", False)
    assert generate_latest_metrics() == b""


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


def test_configure_tracing_does_not_raise() -> None:
    """configure_tracing is safe to call with defaults."""
    configure_tracing()


def test_configure_tracing_with_endpoint_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configure_tracing tolerates a configured endpoint even without the
    OTLP exporter installed."""
    monkeypatch.setenv(
        "DOCTORAGENT_OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://localhost:4318/v1/traces",
    )
    configure_tracing(service_name="doctoragent-test")


def test_configure_tracing_noop_without_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the OpenTelemetry SDK is unavailable configure_tracing is a no-op."""
    monkeypatch.setattr(obs_tracing, "_OTEL_AVAILABLE", False)
    configure_tracing()  # must not raise


def test_get_tracer_returns_object() -> None:
    """get_tracer returns a tracer (real or no-op)."""
    tracer = get_tracer()
    assert tracer is not None


def test_get_tracer_noop_supports_context_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-op tracer's start_as_current_span is a usable context manager."""
    monkeypatch.setattr(obs_tracing, "_OTEL_AVAILABLE", False)
    tracer = get_tracer()
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("key", "value")
        span.set_status("ok")
        span.record_exception(RuntimeError("boom"))
        span.add_event("evt")
        span.end()


def test_instrument_app_none_is_noop() -> None:
    """instrument_app(None) must not raise."""
    instrument_app(None)


def test_instrument_app_noop_without_otel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instrument_app with a real app is a no-op when OTel is unavailable."""
    monkeypatch.setattr(obs_tracing, "_OTEL_AVAILABLE", False)

    class _FakeApp:
        pass

    instrument_app(_FakeApp())  # must not raise


# ---------------------------------------------------------------------------
# Integration: FastAPI /metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_served_when_fastapi_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """GET /metrics returns 200 and Prometheus exposition bytes."""
    import os

    from doctoragent.api.server import is_available

    if not is_available():
        pytest.skip("FastAPI not installed")

    from unittest.mock import MagicMock

    monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from doctoragent.api.server import create_app
    from doctoragent.config import AegisConfig

    config = AegisConfig()
    config.paths.inbox = tmp_path / "Inbox"
    config.paths.vault = tmp_path / "Vault"
    config.paths.index = tmp_path / "Index"
    config.paths.logs = tmp_path / "Logs"
    for p in [config.paths.inbox, config.paths.vault, config.paths.index, config.paths.logs]:
        p.mkdir(parents=True, exist_ok=True)
    config.paths.connections = tmp_path / "Config" / "connections.json"
    config.paths.connections.parent.mkdir(parents=True, exist_ok=True)

    agent = MagicMock()
    agent.task_store.list_recent.return_value = []
    agent.task_store.list_vault_files.return_value = []
    agent.task_store.get.return_value = None
    agent.master_key_provider = MagicMock()
    agent.master_key_provider.get_key.return_value = os.urandom(32)
    del agent._sync_engine
    del agent.search

    async def _search(*args: object, **kwargs: object) -> list[Any]:
        return []

    agent.search = _search

    app = create_app(config, agent)
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})
    response = client.get("/metrics")
    assert response.status_code == 200
    assert isinstance(response.content, bytes)
    assert response.headers["content-type"].startswith("text/plain")


def test_http_middleware_increments_request_counter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The HTTP metrics middleware increments ``doctoragent_http_requests_total``
    for every request that flows through the FastAPI app."""
    from doctoragent.api.server import is_available

    if not is_available():
        pytest.skip("FastAPI not installed")

    import os
    from unittest.mock import MagicMock

    monkeypatch.setenv("DOCTORAGENT_API_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from doctoragent.api.server import create_app
    from doctoragent.config import AegisConfig
    from doctoragent.observability.metrics import get_metrics

    config = AegisConfig()
    config.paths.inbox = tmp_path / "Inbox"
    config.paths.vault = tmp_path / "Vault"
    config.paths.index = tmp_path / "Index"
    config.paths.logs = tmp_path / "Logs"
    for p in [config.paths.inbox, config.paths.vault, config.paths.index, config.paths.logs]:
        p.mkdir(parents=True, exist_ok=True)
    config.paths.connections = tmp_path / "Config" / "connections.json"
    config.paths.connections.parent.mkdir(parents=True, exist_ok=True)

    agent = MagicMock()
    agent.task_store.list_recent.return_value = []
    agent.task_store.list_vault_files.return_value = []
    agent.task_store.get.return_value = None
    agent.master_key_provider = MagicMock()
    agent.master_key_provider.get_key.return_value = os.urandom(32)
    del agent._sync_engine
    del agent.search

    async def _search(*args: object, **kwargs: object) -> list[Any]:
        return []

    agent.search = _search

    app = create_app(config, agent)
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})

    # Hit /health twice — the middleware should observe both.
    for _ in range(2):
        client.get("/health")

    snapshot = get_metrics()
    # Prometheus exposition falls back to the in-process stub when the SDK
    # is missing; either way, the counter must have been incremented.
    matched = [
        s
        for s in snapshot.get("doctoragent_http_requests_total", [])
        if s["labels"].get("path") == "/health" and s["labels"].get("status") == "200"
    ]
    assert matched, f"expected /health request in metrics snapshot: {snapshot}"
    assert matched[0]["value"] >= 2.0

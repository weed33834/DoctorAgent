"""Observability stack for DoctorAgent.

This package wires together three pillars of observability:

* **Structured logging** — :func:`configure_logging` configures
  :mod:`structlog` (when available) with a JSON/console renderer and bridges
  stdlib ``logging.getLogger(__name__)`` calls through the same processor
  chain so existing call sites keep working unchanged.
* **Metrics** — :mod:`doctoragent.observability.metrics` defines a small set of
  Prometheus counters/histograms (``doctoragent_*``) plus a ``/metrics``-friendly
  :func:`~doctoragent.observability.metrics.generate_latest_metrics` helper.
* **Tracing** — :func:`configure_tracing` sets up an OpenTelemetry
  ``TracerProvider`` (with an OTLP exporter when configured), and
  :func:`instrument_app` wires FastAPI auto-instrumentation.

All public helpers degrade gracefully when their optional backing library
(``structlog`` / ``prometheus-client`` / ``opentelemetry``) is not installed:
imports of this package never raise ``ImportError`` for end users, and the
helpers fall back to stdlib logging, no-op metrics, or a no-op tracer.
"""

from doctoragent.observability.langfuse import (
    LangfuseConfig,
    configure_langfuse,
    flush_langfuse,
    is_langfuse_enabled,
    langfuse_context,
    observe,
)
from doctoragent.observability.logging import configure_logging
from doctoragent.observability.metrics import generate_latest_metrics, get_metrics
from doctoragent.observability.tracing import configure_tracing, get_tracer, instrument_app

__all__ = [
    "LangfuseConfig",
    "configure_langfuse",
    "configure_logging",
    "configure_tracing",
    "flush_langfuse",
    "generate_latest_metrics",
    "get_metrics",
    "get_tracer",
    "instrument_app",
    "is_langfuse_enabled",
    "langfuse_context",
    "observe",
]

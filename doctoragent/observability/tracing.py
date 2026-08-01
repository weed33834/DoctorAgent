"""OpenTelemetry tracing for DoctorAgent with a graceful no-op fallback.

When :mod:`opentelemetry` is installed we configure a global
:class:`~opentelemetry.sdk.trace.TracerProvider` with a resource identifying
the DoctorAgent service and, when an OTLP endpoint is provided (either via the
``endpoint`` argument or the ``DOCTORAGENT_OTEL_EXPORTER_OTLP_ENDPOINT``
environment variable), attach a :class:`~opentelemetry.sdk.trace.export.BatchSpanProcessor`
backed by an OTLP HTTP exporter.

When OpenTelemetry is not installed (or only the API is present without the
SDK), :func:`get_tracer` returns a no-op tracer that is safe to use as a
context manager, and :func:`instrument_app` is a no-op.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_OTEL_ENDPOINT_ENV = "DOCTORAGENT_OTEL_EXPORTER_OTLP_ENDPOINT"

# The OpenTelemetry SDK is an optional ``server`` extra. Probe for the
# packages we actually use; if any is missing we fall back to no-ops.
_OTEL_AVAILABLE = False
_OTLP_AVAILABLE = False
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:
    trace = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]

if _OTEL_AVAILABLE:
    try:
        # The OTLP exporter lives in a separate distribution
        # (opentelemetry-exporter-otlp). It is optional even when the SDK is
        # installed.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        _OTLP_AVAILABLE = True
    except ImportError:
        OTLPSpanExporter = None  # type: ignore[assignment]


def configure_tracing(
    service_name: str = "doctoragent",
    endpoint: str | None = None,
) -> None:
    """Configure the global OpenTelemetry tracer provider.

    Parameters
    ----------
    service_name:
        The OTel ``service.name`` resource attribute. Defaults to ``"doctoragent"``.
    endpoint:
        Optional OTLP HTTP endpoint (e.g. ``http://otel-collector:4318/v1/traces``).
        When ``None`` (the default) the value of the
        ``DOCTORAGENT_OTEL_EXPORTER_OTLP_ENDPOINT`` environment variable is used.
        When no endpoint is configured at all, spans are still recorded
        in-process but not exported anywhere — useful for local debugging.

    This function is idempotent and safe to call when OpenTelemetry is not
    installed (it silently does nothing).
    """
    if not _OTEL_AVAILABLE:
        logger.debug("OpenTelemetry SDK not available; tracing disabled")
        return

    resolved_endpoint = endpoint or os.environ.get(_OTEL_ENDPOINT_ENV)
    provider = _build_provider(service_name, resolved_endpoint)
    _set_provider(provider)


def _build_provider(service_name: str, endpoint: str | None) -> Any:
    """Construct a TracerProvider with optional OTLP exporter."""
    assert TracerProvider is not None and Resource is not None  # type checkers
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": _doctoragent_version(),
        }
    )
    provider = TracerProvider(resource=resource)

    if endpoint and _OTLP_AVAILABLE:
        assert OTLPSpanExporter is not None and BatchSpanProcessor is not None
        try:
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OpenTelemetry OTLP exporter configured to %s", endpoint)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Failed to configure OTLP exporter to %s: %s", endpoint, exc)
    elif endpoint and not _OTLP_AVAILABLE:
        logger.warning(
            "OTLP endpoint configured (%s) but opentelemetry-exporter-otlp is not "
            "installed; spans will not be exported",
            endpoint,
        )

    return provider


def _set_provider(provider: Any) -> None:
    """Install the tracer provider, tolerating repeat configuration."""
    assert trace is not None
    try:
        trace.set_tracer_provider(provider)
    except Exception as exc:  # pragma: no cover — set_tracer_provider guards internally
        # Calling set_tracer_provider more than once is a no-op in practice but
        # we swallow any error to stay idempotent for tests / reconfiguration.
        logger.debug("Tracer provider already set: %s", exc)


def get_tracer(module_name: str = "doctoragent") -> Any:
    """Return a tracer.

    With OpenTelemetry installed this delegates to
    :func:`opentelemetry.trace.get_tracer`. Otherwise it returns a no-op
    tracer whose ``start_as_current_span``/``start_span`` are context-manager
    safe and never raise.
    """
    if _OTEL_AVAILABLE:
        assert trace is not None
        return trace.get_tracer(module_name)
    return _NoopTracer()


def instrument_app(app: Any) -> None:
    """Instrument a FastAPI application for automatic tracing.

    When the ``opentelemetry-instrumentation-fastapi`` package is available
    this calls :meth:`FastAPIInstrumentor.instrument_app`. Otherwise (or when
    *app* is ``None``) it is a no-op that never raises.
    """
    if app is None:
        return
    if not _OTEL_AVAILABLE:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        logger.debug("FastAPIInstrumentor not available; skipping app instrumentation")
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("FastAPI instrumentation failed: %s", exc)


class _NoopSpan:
    """A no-op span returned by :class:`_NoopTracer`."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass

    def set_status(self, status: Any) -> None:  # noqa: ARG002
        pass

    def record_exception(self, exception: BaseException) -> None:  # noqa: ARG002
        pass

    def add_event(self, name: str, attributes: Any = None) -> None:  # noqa: ARG002
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:  # noqa: ARG002
        return None


class _NoopTracer:
    """A no-op tracer used when OpenTelemetry is not installed.

    ``start_as_current_span`` returns a null context manager so it can be used
    in ``with`` blocks regardless of the runtime configuration.
    """

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NoopSpan:  # noqa: ARG002
        return _NoopSpan()

    def start_as_current_span(
        self, name: str, *args: Any, **kwargs: Any
    ) -> contextlib.AbstractContextManager[Any]:  # noqa: ARG002
        return contextlib.nullcontext(_NoopSpan())


def _doctoragent_version() -> str:
    """Best-effort lookup of the installed DoctorAgent version for the resource."""
    try:
        from doctoragent import __version__

        return __version__
    except Exception:  # pragma: no cover — defensive
        return "unknown"

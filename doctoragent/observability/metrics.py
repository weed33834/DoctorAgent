"""Prometheus metrics for DoctorAgent with a graceful no-op fallback.

When :mod:`prometheus_client` is available we register a small set of
``doctoragent_*`` counters/histograms on the default :data:`~prometheus_client.REGISTRY`
and expose them via :func:`generate_latest_metrics` (Prometheus exposition
format, suitable for a ``/metrics`` endpoint).

When ``prometheus_client`` is **not** installed, every metric object is
replaced by a tiny in-process stub that still supports ``.labels(...).inc()``
/ ``.observe(...)`` so callers don't need to know whether Prometheus is
present. In that mode :func:`generate_latest_metrics` returns empty bytes
and :func:`get_metrics` returns the stubbed in-memory snapshot for debugging.
"""

from __future__ import annotations

import threading
from typing import Any

_PROMETHEUS_AVAILABLE = False
try:
    from prometheus_client import (
        REGISTRY,
        Counter,
        Histogram,
        generate_latest,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only on minimal installs
    REGISTRY = None  # type: ignore[assignment]
    Counter = None  # type: ignore[assignment]
    Histogram = None  # type: ignore[assignment]
    generate_latest = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# No-op stubs (used when prometheus_client is missing)
# ---------------------------------------------------------------------------


class _NoopMetric:
    """Shared base for the in-process no-op Counter/Histogram.

    Values are recorded but never exposed via a scrape endpoint; they are
    only queryable through :func:`get_metrics` for debugging.
    """

    def __init__(self, name: str, description: str, labelnames: tuple[str, ...]) -> None:
        self._name = name
        self._description = description
        self._labelnames = labelnames
        # labels-key (tuple aligned with labelnames) -> sample dict
        self._samples: dict[tuple[str, ...], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _key(self, labelvalues: Any) -> tuple[str, ...]:
        if isinstance(labelvalues, dict):
            return tuple(str(labelvalues.get(n, "")) for n in self._labelnames)
        return tuple(str(v) for v in labelvalues)

    def _entry(self, labelvalues: Any) -> dict[str, Any]:
        key = self._key(labelvalues)
        with self._lock:
            entry = self._samples.get(key)
            if entry is None:
                entry = {"labels": dict(zip(self._labelnames, key, strict=True))}
                self._samples[key] = entry
            return entry

    def labels(self, *args: Any, **kwargs: Any) -> _NoopMetricChild:
        labelvalues = kwargs if kwargs else args
        return _NoopMetricChild(self, labelvalues)

    def describe(self) -> dict[str, Any]:
        with self._lock:
            samples = []
            for entry in self._samples.values():
                # Copy the recorded fields (labels + value, plus count/sum for
                # histograms) so callers see everything the stub tracked.
                snapshot_entry = {"labels": dict(entry["labels"])}
                for key in ("value", "count", "sum"):
                    if key in entry:
                        snapshot_entry[key] = entry[key]
                samples.append(snapshot_entry)
        return {"name": self._name, "help": self._description, "samples": samples}


class _NoopCounter(_NoopMetric):
    """In-process Counter stub."""

    def _inc(self, labelvalues: Any, amount: float = 1.0) -> None:
        entry = self._entry(labelvalues)
        with self._lock:
            entry["value"] = float(entry.get("value", 0.0)) + float(amount)

    # Mirror prometheus_client's API: Counter().inc() is also valid (no labels).
    def inc(self, amount: float = 1.0) -> None:  # pragma: no cover - convenience
        self._inc((), amount)


class _NoopHistogram(_NoopMetric):
    """In-process Histogram stub — records count + sum (no buckets)."""

    def _observe(self, labelvalues: Any, value: float) -> None:
        entry = self._entry(labelvalues)
        with self._lock:
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["sum"] = float(entry.get("sum", 0.0)) + float(value)
            entry["value"] = entry["sum"]

    def observe(self, value: float) -> None:  # pragma: no cover - convenience
        self._observe((), value)


class _NoopMetricChild:
    """Label-set view returned by ``.labels(...)`` on a no-op metric."""

    def __init__(self, parent: _NoopMetric, labelvalues: Any) -> None:
        self._parent = parent
        self._labelvalues = labelvalues

    def inc(self, amount: float = 1.0) -> None:
        if isinstance(self._parent, _NoopCounter):
            self._parent._inc(self._labelvalues, amount)

    def observe(self, value: float) -> None:
        if isinstance(self._parent, _NoopHistogram):
            self._parent._observe(self._labelvalues, value)


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


def _make_counter(name: str, description: str, labelnames: tuple[str, ...]) -> Any:
    if _PROMETHEUS_AVAILABLE:
        return Counter(name, description, labelnames)
    return _NoopCounter(name, description, labelnames)


def _make_histogram(name: str, description: str, labelnames: tuple[str, ...]) -> Any:
    if _PROMETHEUS_AVAILABLE:
        return Histogram(name, description, labelnames)
    return _NoopHistogram(name, description, labelnames)


# Public metric objects. Callers use ``doctoragent_http_requests_total.labels(...).inc()``
# regardless of whether prometheus_client is installed.
doctoragent_http_requests_total: Any = _make_counter(
    "doctoragent_http_requests_total",
    "Total HTTP requests handled by the DoctorAgent API server.",
    ("method", "path", "status"),
)

doctoragent_http_request_duration_seconds: Any = _make_histogram(
    "doctoragent_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ("method", "path"),
)

doctoragent_agent_iterations: Any = _make_counter(
    "doctoragent_agent_iterations",
    "Agent reasoning iterations completed.",
    ("task_id", "outcome"),
)

doctoragent_llm_tokens_total: Any = _make_counter(
    "doctoragent_llm_tokens_total",
    "LLM tokens consumed by DoctorAgent.",
    ("model", "kind"),
)

doctoragent_encryption_ops_total: Any = _make_counter(
    "doctoragent_encryption_ops_total",
    "Encryption/decryption operations performed.",
    ("op",),
)

doctoragent_errors_total: Any = _make_counter(
    "doctoragent_errors_total",
    "Errors observed by component.",
    ("component",),
)


_ALL_METRICS: tuple[Any, ...] = (
    doctoragent_http_requests_total,
    doctoragent_http_request_duration_seconds,
    doctoragent_agent_iterations,
    doctoragent_llm_tokens_total,
    doctoragent_encryption_ops_total,
    doctoragent_errors_total,
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def generate_latest_metrics() -> bytes:
    """Return the Prometheus exposition format for the default registry.

    Returns empty ``bytes`` when ``prometheus_client`` is not installed so the
    ``/metrics`` endpoint stays available but emits nothing.
    """
    if not _PROMETHEUS_AVAILABLE:
        return b""
    return generate_latest()  # type: ignore[misc]


def get_metrics() -> dict[str, Any]:
    """Return a snapshot of all ``doctoragent_*`` metrics for debugging.

    The structure is::

        {
            "doctoragent_http_requests_total": [
                {"labels": {"method": "GET", ...}, "value": 3.0},
                ...
            ],
            ...
        }

    With ``prometheus_client`` installed the snapshot is sampled from the
    default registry (only ``doctoragent_*`` samples are returned, to keep the
    payload focused). Without it, the in-process stub state is returned.
    """
    if _PROMETHEUS_AVAILABLE:
        return _snapshot_from_registry()

    snapshot: dict[str, list[dict[str, Any]]] = {}
    for metric in _ALL_METRICS:
        if isinstance(metric, _NoopMetric):
            info = metric.describe()
            samples = [
                {"labels": dict(s["labels"]), "value": float(s.get("value", 0.0))}
                for s in info["samples"]
            ]
            if samples:
                snapshot[info["name"]] = samples
    return snapshot


def _snapshot_from_registry() -> dict[str, Any]:
    """Sample doctoragent_* metrics from the default prometheus registry."""
    assert REGISTRY is not None  # for type checkers
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for family in REGISTRY.collect():
        # Each MetricFamily has a `.name` (the metric name without _total) and
        # a list of samples whose `.name` carries the exposition name.
        for sample in family.samples:
            if not sample.name.startswith("doctoragent_"):
                continue
            snapshot.setdefault(sample.name, []).append(
                {
                    "labels": dict(sample.labels),
                    "value": float(sample.value),
                }
            )
    return snapshot

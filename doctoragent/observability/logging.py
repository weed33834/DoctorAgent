"""Structured logging configuration with a graceful structlog fallback.

When :mod:`structlog` is available we configure it to render JSON (production)
or human-readable console output (debug/dev), and bridge stdlib
``logging.getLogger(__name__)`` calls through structlog's
:class:`~structlog.stdlib.ProcessorFormatter` pipeline so existing call sites
keep working unchanged.

When structlog is missing we fall back to a plain ``logging.basicConfig``
that matches the previous behaviour (``%(asctime)s - %(name)s - %(levelname)s
- %(message)s``).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_STRUCTLOG_AVAILABLE = False
try:
    import structlog

    _STRUCTLOG_AVAILABLE = True
except ImportError:  # pragma: no cover — structlog is a hard dependency, but be safe
    structlog = None  # type: ignore[assignment]

_DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def configure_logging(debug: bool = False, json_output: bool = True) -> None:
    """Configure root logging for the DoctorAgent process.

    Parameters
    ----------
    debug:
        When ``True`` the root level is set to ``DEBUG`` and structlog renders
        pretty console output (dev-friendly). When ``False`` the level is
        ``INFO`` and rendering follows *json_output*.
    json_output:
        Force JSON output. Defaults to ``True`` so production deployments get
        structured JSON logs by default; pass ``False`` together with
        ``debug=True`` for coloured console rendering during local
        development. Ignored when structlog is not installed.
    """
    level = logging.DEBUG if debug else logging.INFO
    if _STRUCTLOG_AVAILABLE:
        _configure_structlog(level=level, debug=debug, json_output=json_output)
        return

    # Fallback: keep the legacy basicConfig behaviour intact.
    root = logging.getLogger()
    if root.handlers:
        # Preserve caller-configured handlers; just relax the level if needed.
        root.setLevel(min(root.level, level))
    else:
        logging.basicConfig(level=level, format=_DEFAULT_FORMAT)


def _configure_structlog(*, level: int, debug: bool, json_output: bool) -> None:
    """Wire structlog + stdlib logging together through one formatter.

    Both structlog's own loggers and legacy stdlib ``logging.getLogger`` calls
    end up flowing through the same :class:`~structlog.stdlib.ProcessorFormatter`
    so output stays uniform.
    """
    assert structlog is not None  # narrowed for type checkers

    if debug and not json_output:
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    # structlog → stdlib bridge: structlog loggers become stdlib loggers whose
    # records are pre-processed and then handed to the shared ProcessorFormatter.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Mirror the legacy branching: when a caller (e.g. pytest's log capture,
    # or a host application) has already attached handlers, preserve them and
    # only relax the level so we never clobber their capture pipeline. When no
    # handler is configured yet (typical production start) install the
    # structured-log handler so output is uniformly JSON/console-rendered.
    if root.handlers:
        root.setLevel(min(root.level, level))
    else:
        root.addHandler(handler)
        root.setLevel(level)

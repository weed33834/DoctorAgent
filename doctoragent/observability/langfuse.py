"""Langfuse tracing integration.

Langfuse (https://langfuse.com) is an open-source LLM observability platform
that records traces (nested spans), generations (LLM calls with token usage)
and scores (evaluations) for AI applications. The official ``langfuse`` SDK
provides a ``@observe()`` decorator that auto-instruments any function —
nested calls become nested spans, ``langfuse.{update_current_trace, ...}``
lets you attach metadata.

This module wires Langfuse into DoctorAgent's observability stack with the
same graceful-degradation policy used by the rest of the package:

* When ``langfuse`` is installed AND configured (host + public/secret keys),
  :func:`observe` is the real Langfuse decorator and :func:`configure_langfuse`
  initialises the global client.
* When ``langfuse`` is NOT installed, every helper is a no-op and the
  decorator returns the function unchanged — so clinical agents / providers /
  the workflow can be ``@observe``-decorated unconditionally without breaking
  minimal installs or offline use.

Configuration sources (in priority order):
1. ``LangfuseConfig`` dataclass passed to :func:`configure_langfuse`
2. Environment variables ``LANGFUSE_HOST`` / ``LANGFUSE_PUBLIC_KEY`` /
   ``LANGFUSE_SECRET_KEY`` (the SDK's own defaults — we don't reinvent)
3. ``DOCTORAGENT_LANGFUSE_*`` env vars (project convention)

The ``@observe()`` decorator is applied to the highest-value observability
points:
- :func:`run_clinical_workflow` — top-level trace per clinical decision
- :meth:`ClinicalAgent.run` / :meth:`ModelProvider.chat_completion` —
  nested generations with token usage

PII / PHI safety: Langfuse is an EXTERNAL service. The decorator captures
function args by default — clinical code MUST mark sensitive args via
``langfuse_context().update_current_observation(metadata=...)`` or pass
``@observe(capture_input=False)`` for endpoints that receive raw patient
data. See ``docs/CLINICAL_CAPABILITIES.md`` § PHI handling.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

__all__ = [
    "LangfuseConfig",
    "configure_langfuse",
    "flush_langfuse",
    "get_langfuse",
    "is_langfuse_enabled",
    "langfuse_context",
    "observe",
]

F = TypeVar("F", bound=Callable[..., Any])

# Detection happens once at import — the langfuse import is wrapped so this
# module never fails when the SDK is absent. ``langfuse_context`` is imported
# separately because its path moved between SDK v2 (``langfuse.context``)
# and v3 (``langfuse``), and a missing helper should not disable the whole
# integration.
try:
    from langfuse import Langfuse  # type: ignore[import-not-found]

    _LANGFUSE_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised on minimal installs
    _LANGFUSE_AVAILABLE = False
    Langfuse = None  # type: ignore[assignment, misc]

# langfuse_context lives in different places across SDK versions.
_lf_langfuse_context: Any = None
if _LANGFUSE_AVAILABLE:
    for _ctx_path in ("langfuse.context", "langfuse"):
        try:
            _ctx_mod = __import__(_ctx_path, fromlist=["langfuse_context"])
            _lf_langfuse_context = getattr(_ctx_mod, "langfuse_context", None)
            if _lf_langfuse_context is not None:
                break
        except ImportError:
            continue
        except AttributeError:
            continue


# Singleton client; None until configure_langfuse() is called (or when langfuse
# is unavailable / not configured). Module-level so callers don't have to
# thread it through every function — mirrors the OTel tracer pattern.
_client: Any = None
_configured: bool = False
# The config most recently passed to configure_langfuse(). Kept so the
# ``@observe`` decorator can resolve the effective mask flags at *call* time
# (not decoration time) — operators may pass ``LangfuseConfig(mask_inputs=True)``
# programmatically without touching env vars, and toggling masking at runtime
# (e.g. via a reconfigure) takes effect for subsequent calls.
_active_config: LangfuseConfig | None = None
# The config used to construct the current ``_client``. Compared against the
# next ``configure_langfuse`` call so a credential-stable reconfigure (e.g.
# only the mask flags changed) reuses the existing OTel-backed client instead
# of building a redundant one (v4 registers a process-global provider once).
_client_config: LangfuseConfig | None = None


@dataclass
class LangfuseConfig:
    """Configuration for the Langfuse client.

    When ``host`` / ``public_key`` / ``secret_key`` are all set the client is
    initialised; otherwise :func:`configure_langfuse` is a no-op and
    :func:`is_langfuse_enabled` returns ``False``.

    ``flush_at`` controls the SDK's async flush threshold; ``timeout`` is the
    network timeout for the background uploader.
    """

    host: str | None = None
    public_key: str | None = None
    secret_key: str | None = None
    flush_at: int = 15
    timeout: int = 30
    # When True the decorator still runs but captures no inputs/outputs —
    # use this for endpoints that receive raw PHI.
    mask_inputs: bool = False
    mask_outputs: bool = False

    @classmethod
    def from_env(cls) -> LangfuseConfig:
        """Build a config from environment variables.

        Prefers ``LANGFUSE_*`` (the SDK's native vars) so users can reuse
        existing deployment manifests; falls back to ``DOCTORAGENT_LANGFUSE_*``
        for consistency with the project's own env-var convention.
        """
        return cls(
            host=(os.environ.get("LANGFUSE_HOST") or os.environ.get("DOCTORAGENT_LANGFUSE_HOST")),
            public_key=(
                os.environ.get("LANGFUSE_PUBLIC_KEY")
                or os.environ.get("DOCTORAGENT_LANGFUSE_PUBLIC_KEY")
            ),
            secret_key=(
                os.environ.get("LANGFUSE_SECRET_KEY")
                or os.environ.get("DOCTORAGENT_LANGFUSE_SECRET_KEY")
            ),
            mask_inputs=_env_bool("DOCTORAGENT_LANGFUSE_MASK_INPUTS", False),
            mask_outputs=_env_bool("DOCTORAGENT_LANGFUSE_MASK_OUTPUTS", False),
        )

    def is_complete(self) -> bool:
        return bool(self.host and self.public_key and self.secret_key)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _credentials_match(a: LangfuseConfig, b: LangfuseConfig) -> bool:
    """True when two configs target the same Langfuse project (host + keys)."""
    return (a.host, a.public_key, a.secret_key) == (
        b.host,
        b.public_key,
        b.secret_key,
    )


def _drop_client(client: Any) -> None:
    """Release a Langfuse client reference without tearing it down.

    Deliberately NOT calling ``shutdown()``/``flush()`` here:

    * Langfuse v4 registers a **process-global** OTel ``TracerProvider`` on
      the first ``Langfuse()`` construction. ``client.shutdown()`` tears
      that global provider down, after which every subsequent
      ``Langfuse()`` / ``@observe`` call is silently misrouted (the SDK
      logs a warning and drops spans). Calling shutdown mid-process
      therefore bricks tracing for the rest of the run.
    * ``flush()`` against an unreachable host blocks for the OTel
      ``BatchSpanProcessor`` retry/backoff window (up to ~15 s), which
      hangs request handlers and the test runner.

    The previous client's background uploader runs on daemon threads that
    drain their queue and exit with the process, so simply dropping the
    reference is the safe, non-blocking choice. The real leak mitigation
    is :func:`configure_langfuse`'s credential-reuse short-circuit, which
    avoids constructing redundant clients (and thus redundant providers)
    when only the mask flags change at runtime.
    """
    # Intentionally a no-op beyond the implicit drop performed by the caller
    # reassigning ``_client``. Kept as a named hook so the policy is
    # discoverable and adjustable in one place.
    return None


def configure_langfuse(config: LangfuseConfig | None = None) -> bool:
    """Initialise the global Langfuse client.

    Returns ``True`` when Langfuse is enabled (SDK present + config complete),
    ``False`` otherwise (no-op). The active config is always recorded as
    ``_active_config`` so the ``@observe`` decorator can honour
    ``mask_inputs`` / ``mask_outputs`` at call time.

    Resource-safety (the historical "reconfigure leak"): when the new config
    targets the *same* Langfuse project (identical host + public/secret
    keys), the existing client is **reused** — only ``_active_config`` is
    updated. This is the common runtime case (toggling a mask flag) and
    avoids spinning up a second OTel tracer provider, which v4 would
    silently ignore anyway. Only a genuine credential change (or the first
    call) constructs a fresh client; the previous reference is then dropped
    via :func:`_drop_client` (see its docstring for why we don't shutdown).

    This MUST be called once at app startup (e.g. in ``create_app`` or the
    CLI entrypoint) before any ``@observe``-decorated function runs, so the
    decorator sees an initialised client.
    """
    global _client, _configured, _active_config, _client_config
    _configured = True
    cfg = config or LangfuseConfig.from_env()
    # Always record the active config — even when incomplete — so the mask
    # flags are respected regardless of whether tracing is enabled.
    _active_config = cfg
    if not _LANGFUSE_AVAILABLE:
        logger.debug("langfuse SDK not installed; tracing disabled")
        _drop_client(_client)
        _client = None
        _client_config = None
        return False
    if not cfg.is_complete():
        logger.debug(
            "langfuse config incomplete (host/public/secret keys missing); tracing disabled"
        )
        _drop_client(_client)
        _client = None
        _client_config = None
        return False
    # Reuse the existing client when credentials are unchanged. Toggling
    # only mask flags (the common runtime case) must NOT construct a new
    # client — v4 sets a process-global OTel provider once and a second
    # Langfuse() would be silently ignored, so its spans would be
    # misrouted to the original (now-stale) provider.
    if (
        _client is not None
        and _client_config is not None
        and _credentials_match(_client_config, cfg)
    ):
        # Mask config already reflected via _active_config above.
        return True
    # Credentials changed (or first call): build a new client. The
    # previous reference is dropped (see _drop_client for the v4 rationale).
    _drop_client(_client)
    try:
        _client = Langfuse(  # type: ignore[misc]
            host=cfg.host,
            public_key=cfg.public_key,
            secret_key=cfg.secret_key,
            flush_at=cfg.flush_at,
            timeout=cfg.timeout,
        )
        _client_config = cfg
        logger.info("Langfuse tracing enabled (host=%s)", cfg.host)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash the app on telemetry
        logger.warning("Langfuse initialisation failed; tracing disabled: %s", exc)
        _client = None
        _client_config = None
        return False


def is_langfuse_enabled() -> bool:
    """Return True iff the SDK is installed AND a client was configured."""
    return _LANGFUSE_AVAILABLE and _client is not None


def get_langfuse() -> Any:
    """Return the global Langfuse client (or ``None`` when disabled)."""
    return _client


def flush_langfuse() -> None:
    """Flush pending traces synchronously.

    Call this in a shutdown handler (FastAPI ``shutdown`` event, CLI
    ``finally``) so traces aren't lost when the process exits before the
    background uploader's next tick.
    """
    if not is_langfuse_enabled():
        return
    try:
        _client.flush()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        logger.debug("langfuse flush failed", exc_info=True)


def langfuse_context() -> Any:
    """Return the Langfuse observation context (or ``None`` when disabled).

    Used inside ``@observe``-decorated functions to attach metadata, scores,
    or to mark the current trace's user/session::

        @observe()
        async def run_clinical_workflow(...):
            ctx = langfuse_context()
            if ctx:
                ctx.update_current_trace(
                    user_id=patient_context.get("patient_id"),
                    metadata={"hook": "patient-view"},
                )
    """
    if not is_langfuse_enabled() or _lf_langfuse_context is None:
        return None
    try:
        return _lf_langfuse_context()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# observe() decorator — the key integration point
# --------------------------------------------------------------------------- #
def _resolve_active_config() -> LangfuseConfig:
    """Return the runtime config the decorator should honour right now.

    Prefers the config passed to :func:`configure_langfuse` (``_active_config``)
    so programmatic configuration wins; falls back to env-derived config so
    masking still works before ``configure_langfuse`` runs (decorators apply
    at import time, before startup).
    """
    return _active_config or LangfuseConfig.from_env()


def observe(
    *,
    name: str | None = None,
    capture_input: bool = True,
    capture_output: bool = True,
) -> Callable[[F], F]:
    """Wrap a function so Langfuse records it as a trace span.

    Mirrors ``langfuse.observe``: when the SDK is enabled the wrapper
    records the call (args, return value, duration, exceptions); when
    disabled the function is returned unchanged so production code never
    pays a tracing tax on minimal installs.

    Args:
        name: Span name (defaults to the function's qualified name).
        capture_input: When ``False`` the args are NOT recorded — use this
            for endpoints that receive raw PHI.
        capture_output: When ``False`` the return value is NOT recorded.

    PHI masking: the global ``mask_inputs`` / ``mask_outputs`` flags from
    :func:`configure_langfuse` (or the ``DOCTORAGENT_LANGFUSE_MASK_*`` env
    vars) are resolved at **call time**, not decoration time. When masking
    is active, the effective capture flag is forced to ``False`` even if
    the call site declared ``capture_input=True`` — this is the
    deployment-level kill switch for raw patient data.

    Usage::

        from doctoragent.observability.langfuse import observe

        @observe(name="clinical_workflow")
        async def run_clinical_workflow(...):
            ...

    The decorator works for both sync and async functions (Langfuse's SDK
    handles both via ``inspect.iscoroutinefunction``).
    """
    if not _LANGFUSE_AVAILABLE:
        # No SDK → no-op. functools.wraps preserves the signature so FastAPI
        # dependency injection and introspection still work.
        def _decorator(func: F) -> F:
            return func

        return _decorator

    # Import the real decorator lazily so this module's import side-effects
    # stay minimal (langfuse context init happens in configure_langfuse).
    from langfuse import observe as _lf_observe

    def _decorator(func: F) -> F:
        # Cache of langfuse-decorated variants keyed by the *effective*
        # (capture_input, capture_output) pair. Resolving the mask flags at
        # call time (rather than decoration time) means toggling
        # ``configure_langfuse(LangfuseConfig(mask_inputs=True))`` at runtime
        # takes effect for subsequent calls without re-importing modules.
        variants: dict[tuple[bool, bool], Callable[..., Any]] = {}

        def _variant(cap_in: bool, cap_out: bool) -> Callable[..., Any]:
            key = (cap_in, cap_out)
            cached = variants.get(key)
            if cached is None:
                cached = _lf_observe(  # type: ignore[no-any-return]
                    name=name,
                    capture_input=cap_in,
                    capture_output=cap_out,
                )(func)
                variants[key] = cached
            return cached

        def _effective_flags() -> tuple[bool, bool]:
            cfg = _resolve_active_config()
            cap_in = capture_input and not cfg.mask_inputs
            cap_out = capture_output and not cfg.mask_outputs
            return cap_in, cap_out

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                # When tracing isn't configured the langfuse decorator is a
                # no-op anyway; short-circuit to avoid variant build + env
                # reads on every call of a hot path.
                if not is_langfuse_enabled():
                    return await func(*args, **kwargs)
                cap_in, cap_out = _effective_flags()
                return await _variant(cap_in, cap_out)(*args, **kwargs)

            return _async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not is_langfuse_enabled():
                return func(*args, **kwargs)
            cap_in, cap_out = _effective_flags()
            return _variant(cap_in, cap_out)(*args, **kwargs)

        return _sync_wrapper  # type: ignore[return-value]

    return _decorator


def reset_langfuse_for_tests() -> None:
    """Reset the module-level client (test isolation only)."""
    global _client, _configured, _active_config, _client_config
    _drop_client(_client)
    _client = None
    _configured = False
    _active_config = None
    _client_config = None

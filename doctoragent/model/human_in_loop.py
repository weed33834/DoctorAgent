"""Human-in-the-Loop (HITL) module for agent execution.

Allows pausing agent execution at critical decision points to request
human confirmation, feedback, or guidance.

Features:
- Configurable breakpoints (before tool execution, before final answer, custom)
- Approval workflow with timeout
- Feedback incorporation
- Audit trail of all HITL interactions

The :class:`HITLManager` is thread-safe: :meth:`request_approval` blocks the
calling (agent) thread on a :class:`threading.Event` until a human responds
via :meth:`process_response` (typically from a UI/API thread) or the request
times out. Breakpoints and per-action auto-approval can be configured so that
low-risk actions proceed without human intervention.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any
from uuid import uuid4

from doctoragent.compat import UTC, StrEnum

logger = logging.getLogger(__name__)


# Sentinel used to distinguish "no breakpoint registered" from a registered
# breakpoint whose condition is ``None``.
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Enums & data models
# ---------------------------------------------------------------------------


class BreakpointType(StrEnum):
    """Well-known points in the agent lifecycle where a HITL pause can occur."""

    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    BEFORE_FINAL_ANSWER = "before_final_answer"
    BEFORE_DESTRUCTIVE_ACTION = "before_destructive_action"
    CUSTOM = "custom"


class RequestStatus(StrEnum):
    """Lifecycle status of a :class:`HITLRequest`."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


@dataclass
class HITLResponse:
    """A human's response to a HITL approval request.

    Attributes
    ----------
    approved:
        Whether the proposed action may proceed.
    feedback:
        Optional free-text guidance / justification from the reviewer.
    modified_action:
        Optional replacement action the reviewer wants the agent to use
        instead of the originally proposed one.
    """

    approved: bool = False
    feedback: str = ""
    modified_action: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "approved": self.approved,
            "feedback": self.feedback,
            "modified_action": self.modified_action,
        }


@dataclass
class HITLRequest:
    """A single human-in-the-loop approval request.

    Attributes
    ----------
    request_id:
        Unique identifier for the request.
    breakpoint_type:
        The :class:`BreakpointType` (or custom string) that triggered it.
    context:
        Arbitrary context describing the pending action (tool name, args,
        draft answer, ...).
    description:
        Human-readable explanation of what is being asked.
    timestamp:
        ISO-8601 creation timestamp (UTC).
    status:
        Current :class:`RequestStatus`.
    response:
        The :class:`HITLResponse` once the human has replied (``None`` while
        pending).
    timeout_seconds:
        How long :meth:`HITLManager.request_approval` will block before
        timing out.
    """

    request_id: str = dc_field(default_factory=lambda: uuid4().hex)
    breakpoint_type: BreakpointType | str = BreakpointType.CUSTOM
    context: Any = None
    description: str = ""
    timestamp: str = dc_field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: RequestStatus = RequestStatus.PENDING
    response: HITLResponse | None = None
    timeout_seconds: float = 120.0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (for the audit trail / export)."""
        return {
            "request_id": self.request_id,
            "breakpoint_type": str(self.breakpoint_type),
            "context": self._safe_context(),
            "description": self.description,
            "timestamp": self.timestamp,
            "status": str(self.status),
            "approved": self.response.approved if self.response else None,
            "feedback": self.response.feedback if self.response else "",
            "modified_action": (self.response.modified_action if self.response else None),
            "timeout_seconds": self.timeout_seconds,
        }

    def _safe_context(self) -> Any:
        """Return a JSON-serialisable view of the context for the audit trail."""
        if self.context is None:
            return None
        try:
            json.dumps(self.context, ensure_ascii=False)
            return self.context
        except (TypeError, ValueError):
            return str(self.context)


# ---------------------------------------------------------------------------
# HITL manager
# ---------------------------------------------------------------------------


class HITLManager:
    """Manages human-in-the-loop breakpoints for an agent run.

    Breakpoints are registered against an *action type* (typically a
    :class:`BreakpointType` member, but any hashable string is accepted) with
    an optional *condition* callable. When the agent reaches a potential
    pause point it calls :meth:`should_pause`; if a pause is required,
    :meth:`request_approval` blocks until a human responds via
    :meth:`process_response` or the request times out.

    Parameters
    ----------
    auto_approve:
        When ``True`` every breakpoint is auto-approved without blocking
        (useful for tests / autonomous runs). Individual actions can still be
        toggled via :meth:`set_auto_approve`.
    default_timeout:
        Default blocking timeout (seconds) for :meth:`request_approval`.
    """

    def __init__(
        self,
        auto_approve: bool = False,
        default_timeout: float = 120,
    ) -> None:
        self._auto_approve = bool(auto_approve)
        self._default_timeout = float(default_timeout)
        # action_type -> condition callable (or None for unconditional)
        self._breakpoints: dict[str, Callable[[Any], bool] | None] = {}
        # action_type -> auto-approve flag (per-action override)
        self._auto_approve_actions: dict[str, bool] = {}
        # request_id -> HITLRequest (insertion-ordered = history)
        self._requests: dict[str, HITLRequest] = {}
        # request_id -> threading.Event (only for pending requests)
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Breakpoint configuration
    # ------------------------------------------------------------------

    def register_breakpoint(
        self,
        action_type: BreakpointType | str,
        condition: Callable[[Any], bool] | None = None,
    ) -> None:
        """Register a breakpoint for *action_type*.

        Parameters
        ----------
        action_type:
            The action key to pause on (typically a :class:`BreakpointType`).
        condition:
            Optional callable ``condition(context) -> bool``; the breakpoint
            only fires when it returns ``True``. When ``None`` the breakpoint
            fires unconditionally.
        """
        key = str(action_type)
        with self._lock:
            self._breakpoints[key] = condition
        logger.debug("Registered breakpoint for %r (condition=%s)", key, condition is not None)

    def unregister_breakpoint(self, action_type: BreakpointType | str) -> bool:
        """Remove a previously registered breakpoint. Returns ``True`` if removed."""
        key = str(action_type)
        with self._lock:
            return self._breakpoints.pop(key, _UNSET) is not _UNSET

    def set_auto_approve(self, action_type: BreakpointType | str, auto: bool) -> None:
        """Configure auto-approval for a specific *action_type*.

        When *auto* is ``True``, requests for this action type are approved
        immediately without blocking on a human response.
        """
        key = str(action_type)
        with self._lock:
            self._auto_approve_actions[key] = bool(auto)
        logger.debug("Auto-approve for %r set to %s", key, auto)

    def _is_auto_approved(self, action_type: BreakpointType | str) -> bool:
        """Whether *action_type* should be auto-approved (global or per-action)."""
        key = str(action_type)
        with self._lock:
            per_action = self._auto_approve_actions.get(key, False)
            return self._auto_approve or per_action

    # ------------------------------------------------------------------
    # Pause decision
    # ------------------------------------------------------------------

    def should_pause(self, action_type: BreakpointType | str, context: Any) -> bool:
        """Check whether execution should pause for *action_type*.

        Returns ``False`` when the action is auto-approved, when no breakpoint
        is registered for it, or when its condition evaluates to ``False``.
        Fails safe (returns ``True``) when a condition callable raises.
        """
        if self._is_auto_approved(action_type):
            return False
        key = str(action_type)
        with self._lock:
            condition = self._breakpoints.get(key, _UNSET)
        if condition is _UNSET:
            return False
        if condition is None:
            return True
        try:
            return bool(condition(context))
        except Exception as e:  # noqa: BLE001
            logger.warning("Breakpoint condition for %r raised: %s; pausing for safety.", key, e)
            return True

    # ------------------------------------------------------------------
    # Approval workflow
    # ------------------------------------------------------------------

    def request_approval(
        self,
        breakpoint_type: BreakpointType | str,
        context: Any,
        description: str,
    ) -> HITLRequest:
        """Create a HITL request and block until a human responds or it times out.

        Parameters
        ----------
        breakpoint_type:
            The breakpoint type that triggered the request.
        context:
            Context describing the pending action.
        description:
            Human-readable explanation of what approval is being asked for.

        Returns the :class:`HITLRequest` with its final :attr:`HITLRequest.status`
        (``APPROVED`` / ``REJECTED`` / ``TIMEOUT``) and
        :attr:`HITLRequest.response` populated.
        """
        timeout = self._default_timeout
        request = HITLRequest(
            breakpoint_type=breakpoint_type,
            context=context,
            description=description,
            status=RequestStatus.PENDING,
            response=None,
            timeout_seconds=timeout,
        )
        event = threading.Event()
        with self._lock:
            self._requests[request.request_id] = request
            self._events[request.request_id] = event

        # Auto-approve short-circuit (also honoured when called directly).
        if self._is_auto_approved(breakpoint_type):
            auto_response = HITLResponse(
                approved=True, feedback="自动批准（auto-approve）", modified_action=None
            )
            self.process_response(request.request_id, auto_response)
            return request

        # Block until the human responds or the timeout elapses.
        event.wait(timeout=timeout)
        with self._lock:
            self._events.pop(request.request_id, None)
            if request.status == RequestStatus.PENDING:
                # No response arrived in time.
                request.status = RequestStatus.TIMEOUT
                logger.warning("HITL request %s timed out after %ss", request.request_id, timeout)
        return request

    def process_response(self, request_id: str, response: HITLResponse) -> bool:
        """Process a human *response* for a pending request.

        Wakes the thread blocked in :meth:`request_approval`. Returns
        ``True`` if the response was applied, ``False`` if the request is
        unknown or no longer pending (e.g. already timed out).
        """
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                logger.warning("process_response: unknown request %r", request_id)
                return False
            if request.status != RequestStatus.PENDING:
                logger.warning(
                    "process_response: request %r is already %s", request_id, request.status
                )
                return False
            request.response = response
            request.status = RequestStatus.APPROVED if response.approved else RequestStatus.REJECTED
            event = self._events.get(request_id)

        # Signal outside the lock to avoid holding it while the waiter wakes.
        if event is not None:
            event.set()
        logger.info("HITL request %s %s", request_id, request.status)
        return True

    # ------------------------------------------------------------------
    # Introspection & audit
    # ------------------------------------------------------------------

    def get_pending_requests(self) -> list[HITLRequest]:
        """Return all requests currently awaiting a human response."""
        with self._lock:
            return [req for req in self._requests.values() if req.status == RequestStatus.PENDING]

    def get_history(self) -> list[HITLRequest]:
        """Return all HITL interactions in chronological order."""
        with self._lock:
            return list(self._requests.values())

    def get_request(self, request_id: str) -> HITLRequest | None:
        """Look up a single request by id."""
        with self._lock:
            return self._requests.get(request_id)

    def export_audit_trail(self) -> list[dict[str, Any]]:
        """Export the full HITL audit trail as a list of plain dicts.

        Each entry is the :meth:`HITLRequest.to_dict` of a recorded request,
        safe for JSON serialisation.
        """
        with self._lock:
            requests = list(self._requests.values())
        return [req.to_dict() for req in requests]

    def export_audit_trail_json(self) -> str:
        """Export the audit trail as a JSON string."""
        try:
            return json.dumps(self.export_audit_trail(), ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialise audit trail: %s", e)
            return "[]"

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def clear_history(self) -> None:
        """Remove all recorded requests (and pending events)."""
        with self._lock:
            self._requests.clear()
            self._events.clear()

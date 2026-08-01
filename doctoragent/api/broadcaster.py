"""Async event broadcaster for real-time WebSocket / SSE fan-out.

Internal components (API endpoints, the pipeline, the sync engine, …) publish
events via :meth:`EventBroadcaster.publish`.  Every connected WebSocket client
and every SSE subscriber automatically receives a copy.

The broadcaster is intentionally dependency-free and thread-safe:

* Subscriptions are :class:`asyncio.Queue` objects that live in the server's
  main event loop.
* ``publish`` may be called from **any thread** (e.g. from a pipeline running
  in ``asyncio.to_thread``).  When the loop is running, events are handed to
  subscribers via ``loop.call_soon_threadsafe``; when no loop is attached yet
  (e.g. during startup before the first request) events are silently dropped.
* A per-subscriber queue cap protects the server from a slow client: if a
  subscriber's queue is full, new events for it are dropped (with a debug log)
  rather than blocking the publisher or unbounding memory.

This is a deliberately simple in-process broadcaster — it does not persist
events and does not survive a restart.  For a multi-process deployment a
Redis/NATS pub-sub backend would be wired in behind the same interface.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Per-subscriber queue capacity.  A slow client that falls >MAX_QUEUE behind
# starts losing events rather than consuming unbounded server memory.
MAX_QUEUE_SIZE: int = 256


class EventBroadcaster:
    """Fan-out broker for in-process real-time events.

    Lifecycle
    ---------
    The broadcaster is created once in ``create_app`` and stored on
    ``app.state.broadcaster``.  The lifespan handler calls :meth:`attach_loop`
    so publishers running in worker threads can hand events back into the loop.
    """

    def __init__(self, *, queue_size: int = MAX_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Loop binding
    # ------------------------------------------------------------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Bind (or clear) the event loop used for thread-safe publication."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its dedicated queue.

        The caller (an SSE generator or WebSocket handler) is responsible for
        calling :meth:`unsubscribe` when it finishes to avoid leaking queues.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.add(q)
        logger.debug("EventBroadcaster: subscriber added (%d total)", len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue previously returned by :meth:`subscribe`."""
        with self._lock:
            self._subscribers.discard(q)
        logger.debug("EventBroadcaster: subscriber removed (%d total)", len(self._subscribers))

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------

    def publish(self, event_type: str, data: Any | None = None) -> int:
        """Broadcast an event to every subscriber.

        Returns the number of subscribers the event was delivered to (best
        effort — a subscriber whose queue is full is counted as a drop, not a
        delivery).  Safe to call from any thread.
        """
        event = self._format_event(event_type, data)
        loop = self._loop
        with self._lock:
            subscribers = list(self._subscribers)
        if not subscribers:
            return 0
        # No loop yet (startup/shutdown): drop silently.
        if loop is None:
            return 0
        delivered = 0
        for q in subscribers:
            try:
                loop.call_soon_threadsafe(self._safe_put, q, event)
                delivered += 1
            except RuntimeError:
                # Loop closed between the check and the call — drop.
                continue
        return delivered

    async def publish_async(self, event_type: str, data: Any | None = None) -> int:
        """Async publish helper for callers already running in the event loop."""
        event = self._format_event(event_type, data)
        with self._lock:
            subscribers = list(self._subscribers)
        if not subscribers:
            return 0
        delivered = 0
        for q in subscribers:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                logger.debug("EventBroadcaster: subscriber queue full, dropping event")
            except Exception:  # noqa: BLE001
                logger.debug("EventBroadcaster: publish_async put failed", exc_info=True)
        return delivered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_put(self, q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        """Put *event* into *q* without blocking; drop on full."""
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("EventBroadcaster: subscriber queue full, dropping event")
        except Exception:  # noqa: BLE001 — never let a bad subscriber kill the loop
            logger.debug("EventBroadcaster: subscriber put failed", exc_info=True)

    @staticmethod
    def _format_event(event_type: str, data: Any | None) -> dict[str, Any]:
        """Build the canonical event envelope broadcast to subscribers."""
        import time as _time

        return {
            "type": event_type,
            "data": data,
            "timestamp": _time.time(),
        }

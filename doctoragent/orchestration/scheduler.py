"""Priority-based task scheduler with resource awareness.

Features:
- Priority queue with weighted scheduling
- Resource limits (concurrent tasks, memory, CPU)
- Task deduplication
- Deadline awareness
- Backpressure handling
- Scheduled/recurring task support
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any

from doctoragent.compat import UTC, StrEnum
from doctoragent.orchestration.dag_engine import TaskStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


class Priority(StrEnum):
    """Scheduling priority levels.

    Members carry a numeric ``weight`` (higher = scheduled sooner) used by the
    scheduler's scoring function. The underlying string values are the decimal
    representations of those weights so the enum round-trips sensibly through
    JSON while remaining a true :class:`StrEnum`.
    """

    LOW = "1"
    NORMAL = "5"
    HIGH = "10"
    URGENT = "20"
    CRITICAL = "50"

    @property
    def weight(self) -> int:
        """Numeric weight used for ordering (higher wins)."""
        return int(self.value)


# Bonus added to the score of a task whose deadline has already passed.
_OVERDUE_BONUS = 1000.0
# Maximum bonus contributed by a future deadline (closer deadlines approach
# this cap).
_MAX_DEADLINE_BONUS = 500.0
# Default maximum number of queued tasks before backpressure kicks in.
_DEFAULT_MAX_QUEUE = 1000
# How often (seconds) the run loop wakes itself up even without an explicit
# signal, so deadline-driven re-evaluation is not starved.
_LOOP_POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ResourceLimits:
    """Cluster-wide resource ceilings the scheduler must respect.

    Attributes
    ----------
    max_concurrent:
        Hard cap on the number of tasks running at the same time.
    max_cpu:
        Total CPU units (cores) available to scheduled tasks.
    max_memory_mb:
        Total memory (megabytes) available to scheduled tasks.
    max_gpu:
        Total GPU devices available to scheduled tasks. ``0`` (the default)
        means GPU tracking is disabled: the scheduler neither reserves nor
        gates on GPUs, preserving legacy behaviour. Set to a positive
        integer to enable GPU-aware gating — tasks then declare their GPU
        need via ``ScheduledTask.resource_cost["gpu"]`` (an int count).
    """

    max_concurrent: int = 4
    max_cpu: float = 4.0
    max_memory_mb: float = 4096.0
    max_gpu: int = 0


@dataclass
class ScheduledTask:
    """A unit of work submitted to the :class:`TaskScheduler`.

    Attributes
    ----------
    id:
        Unique identifier used for deduplication and status lookup.
    name:
        Human-readable label.
    callable:
        Async callable invoked as ``await task.callable(task)``. Restored as
        ``None`` by :meth:`from_dict`.
    priority:
        Scheduling :class:`Priority`.
    deadline:
        Optional UTC deadline. Tasks closer to (or past) their deadline receive
        a higher effective score.
    resource_cost:
        Per-resource cost this task consumes while running. Conventional keys
        are ``"cpu"``, ``"memory"`` (megabytes) and ``"gpu"`` (device count,
        only gated when :attr:`ResourceLimits.max_gpu` > 0); arbitrary keys
        are accepted but only those participate in resource gating.
    tags:
        Free-form tags for grouping/filtering.
    created_at / scheduled_at / started_at:
        Lifecycle timestamps (UTC).
    status:
        Current :class:`TaskStatus`.
    result / error:
        Outcome of the execution.
    """

    id: str
    name: str
    callable: Callable[..., Awaitable[Any]] | None = None
    priority: Priority = Priority.NORMAL
    deadline: datetime | None = None
    resource_cost: dict[str, float] = dc_field(default_factory=dict)
    tags: list[str] = dc_field(default_factory=list)
    created_at: datetime = dc_field(default_factory=lambda: datetime.now(UTC))
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (callable excluded)."""
        return {
            "id": self.id,
            "name": self.name,
            "priority": str(self.priority),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "resource_cost": dict(self.resource_cost),
            "tags": list(self.tags),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "status": str(self.status),
            "result": self.result,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TaskScheduler:
    """Priority- and resource-aware async task scheduler.

    Tasks are submitted to an internal heap ordered by a composite score that
    blends :class:`Priority.weight` with deadline urgency (overdue tasks are
    boosted most). The :meth:`_run_loop` coroutine, started by :meth:`start`,
    repeatedly picks the highest-scoring task that fits within the current
    :class:`ResourceLimits` and dispatches it as a background asyncio task.

    The scheduler is thread-safe for submission / inspection / configuration
    calls (guarded by a lock) and deduplicates submissions by task id. When the
    queue grows beyond ``max_queue_size`` submissions are rejected to apply
    backpressure.
    """

    def __init__(
        self,
        resource_limits: ResourceLimits | None = None,
        *,
        max_queue_size: int = _DEFAULT_MAX_QUEUE,
    ) -> None:
        self._limits: ResourceLimits = resource_limits or ResourceLimits()
        self._max_queue_size: int = max_queue_size

        # Heap entries are (-score, seq, task_id); negated score turns the
        # min-heap into a max-heap, and ``seq`` guarantees stable ordering so
        # tasks are never compared directly.
        self._heap: list[tuple[float, int, str]] = []
        self._seq: int = 0
        self._tasks: dict[str, ScheduledTask] = {}
        self._running: set[str] = set()
        self._cancelled_ids: set[str] = set()
        self._bg_tasks: set[asyncio.Task[None]] = set()

        self._lock = threading.Lock()
        self._wakeup: asyncio.Event = asyncio.Event()
        self._running_flag: bool = False
        self._loop_task: asyncio.Task[None] | None = None

        # Metrics.
        self._started_count: int = 0
        self._completed_count: int = 0
        self._failed_count: int = 0
        self._cancelled_count: int = 0
        self._total_wait_seconds: float = 0.0
        self._scheduler_started_at: datetime | None = None

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(self, task: ScheduledTask) -> None:
        """Submit a single task to the scheduling queue.

        Deduplicates by id: a task whose id is already known is silently
        skipped. Raises :class:`RuntimeError` when the queue is full
        (backpressure).
        """
        with self._lock:
            if task.id in self._tasks:
                logger.warning("Task %r already known; skipping (dedup)", task.id)
                return
            if len(self._heap) >= self._max_queue_size:
                raise RuntimeError(
                    f"Scheduler queue full ({self._max_queue_size}); rejecting task {task.id!r}"
                )
            if task.created_at is None:
                task.created_at = datetime.now(UTC)
            task.status = TaskStatus.PENDING
            self._tasks[task.id] = task
            score = self._score(task)
            heapq.heappush(self._heap, (-score, self._seq, task.id))
            self._seq += 1
        logger.debug(
            "Submitted task %r (priority=%s, score=%.2f)",
            task.id,
            task.priority,
            score,
        )
        self._wakeup.set()

    def submit_many(self, tasks: list[ScheduledTask]) -> None:
        """Submit several tasks at once (dedup applies per id)."""
        for task in tasks:
            self.submit(task)

    # ------------------------------------------------------------------
    # Scoring & resource gating
    # ------------------------------------------------------------------

    def _score(self, task: ScheduledTask) -> float:
        """Compute the scheduling score for *task* (higher = sooner).

        Combines the priority weight with a deadline-urgency bonus: overdue
        tasks receive a large flat bonus, while future-deadline tasks receive a
        bonus that grows as the deadline approaches (capped).
        """
        score = float(task.priority.weight)
        if task.deadline is not None:
            now = datetime.now(UTC)
            remaining = (task.deadline - now).total_seconds()
            if remaining <= 0:
                score += _OVERDUE_BONUS
            else:
                score += min(_MAX_DEADLINE_BONUS, _MAX_DEADLINE_BONUS / max(remaining, 1.0))
        return score

    def _can_schedule(self, task: ScheduledTask) -> bool:
        """Whether *task* fits within the current resource limits.

        Must be called while holding ``self._lock`` (reads shared running set).
        """
        if len(self._running) >= self._limits.max_concurrent:
            return False
        used_cpu = sum(self._tasks[tid].resource_cost.get("cpu", 0.0) for tid in self._running)
        used_mem = sum(self._tasks[tid].resource_cost.get("memory", 0.0) for tid in self._running)
        task_cpu = task.resource_cost.get("cpu", 0.0)
        task_mem = task.resource_cost.get("memory", 0.0)
        if used_cpu + task_cpu > self._limits.max_cpu:
            return False
        if used_mem + task_mem > self._limits.max_memory_mb:
            return False
        # GPU gating is opt-in: only enforced when max_gpu > 0. When 0
        # (default) GPU requirements are ignored, preserving legacy behaviour.
        if self._limits.max_gpu > 0:
            used_gpu = sum(self._tasks[tid].resource_cost.get("gpu", 0) for tid in self._running)
            task_gpu = task.resource_cost.get("gpu", 0)
            if used_gpu + task_gpu > self._limits.max_gpu:
                return False
        return True

    def has_gpu_capacity(self, requested: int = 0) -> bool:
        """Whether *requested* GPU devices are currently available.

        Returns ``True`` when GPU tracking is disabled (``max_gpu == 0``) so
        callers can treat "no GPU pool configured" as "not gated". When
        ``max_gpu > 0`` the check accounts for the GPU demand of all
        currently-running tasks.

        Safe to call from any thread; acquires the scheduler lock briefly.
        """
        if self._limits.max_gpu <= 0:
            return True
        if requested <= 0:
            return True
        with self._lock:
            used_gpu = sum(self._tasks[tid].resource_cost.get("gpu", 0) for tid in self._running)
        return used_gpu + requested <= self._limits.max_gpu

    def _pick_next(self) -> ScheduledTask | None:
        """Pop the highest-scoring task that fits current resources.

        Tasks that do not fit are set aside and pushed back onto the heap so a
        later call (after resources free up) can reconsider them. Cancelled or
        already-dispatched entries are discarded (lazy deletion).
        """
        skipped: list[tuple[float, int, str]] = []
        picked: ScheduledTask | None = None
        with self._lock:
            while self._heap:
                neg_score, seq, task_id = heapq.heappop(self._heap)
                task = self._tasks.get(task_id)
                if task is None or task_id in self._cancelled_ids:
                    continue  # lazy deletion
                if task.status != TaskStatus.PENDING:
                    continue  # already handled elsewhere
                if self._can_schedule(task):
                    picked = task
                    break
                skipped.append((neg_score, seq, task_id))
            for item in skipped:
                heapq.heappush(self._heap, item)
        return picked

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the scheduler's background run loop.

        Safe to call once per scheduler instance; calling again while running
        is a no-op.
        """
        if self._running_flag:
            logger.debug("TaskScheduler already running")
            return
        self._running_flag = True
        self._scheduler_started_at = datetime.now(UTC)
        self._loop_task = asyncio.create_task(self._run_loop(), name="task-scheduler-loop")
        logger.info("Task scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler and wait for in-flight tasks to finish.

        Pending (not-yet-started) tasks are marked :attr:`TaskStatus.CANCELLED`.
        """
        self._running_flag = False
        self._wakeup.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=30.0)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._loop_task = None
        # Cancel any still-pending tasks.
        with self._lock:
            for task in self._tasks.values():
                if task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.CANCELLED
                    self._cancelled_count += 1
        # Allow running background tasks to finish.
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        logger.info("Task scheduler stopped")

    async def _run_loop(self) -> None:
        """Main scheduling loop.

        Repeatedly dispatches every resource-feasible task, then waits for a
        wakeup signal (new submission or task completion) or a short timeout
        before re-evaluating.
        """
        logger.info("Task scheduler loop started")
        while self._running_flag:
            while self._running_flag:
                task = self._pick_next()
                if task is None:
                    break
                self._dispatch(task)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=_LOOP_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()
        logger.info("Task scheduler loop stopped")

    def _dispatch(self, task: ScheduledTask) -> None:
        """Mark *task* as running and launch its background coroutine."""
        with self._lock:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(UTC)
            task.scheduled_at = task.scheduled_at or task.started_at
            self._running.add(task.id)
            self._started_count += 1
            self._total_wait_seconds += max(
                0.0,
                (task.started_at - task.created_at).total_seconds(),
            )
        if task.callable is None:
            self._finalize(task, error="task has no callable attached")
            return
        bg = asyncio.create_task(self._run_task(task), name=f"scheduled-{task.id}")
        self._bg_tasks.add(bg)
        bg.add_done_callback(self._bg_tasks.discard)

    async def _run_task(self, task: ScheduledTask) -> None:
        """Execute a dispatched task and record its outcome."""
        try:
            result = await task.callable(task)  # type: ignore[misc]
        except asyncio.CancelledError:
            self._finalize(task, cancelled=True)
            raise
        except Exception as exc:  # noqa: BLE001
            self._finalize(task, error=str(exc))
            logger.exception("Scheduled task %r failed", task.id)
        else:
            self._finalize(task, result=result)
            logger.debug("Scheduled task %r completed", task.id)

    def _finalize(
        self,
        task: ScheduledTask,
        *,
        result: Any = None,
        error: str | None = None,
        cancelled: bool = False,
    ) -> None:
        """Update a finished task's status and refresh metrics."""
        with self._lock:
            self._running.discard(task.id)
            if cancelled:
                task.status = TaskStatus.CANCELLED
                self._cancelled_count += 1
            elif error is not None:
                task.status = TaskStatus.FAILED
                task.error = error
                self._failed_count += 1
            else:
                task.status = TaskStatus.SUCCESS
                task.result = result
                self._completed_count += 1
        self._wakeup.set()

    # ------------------------------------------------------------------
    # Cancellation & configuration
    # ------------------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued or running task.

        Queued (pending) tasks are dropped from the heap via lazy deletion and
        marked cancelled. Running tasks are flagged; their coroutine is allowed
        to observe the status and exit on its own. Returns ``True`` if the task
        was known.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                task.status = TaskStatus.CANCELLED
                self._cancelled_ids.add(task_id)
                self._cancelled_count += 1
                self._running.discard(task_id)
        logger.info("Cancelled task %r", task_id)
        self._wakeup.set()
        return True

    def set_resource_limits(self, limits: ResourceLimits) -> None:
        """Update the resource limits dynamically.

        Tighter limits apply only to *future* dispatches; already-running tasks
        are not preempted. Looser limits may immediately allow more tasks to be
        dispatched on the next loop iteration.
        """
        with self._lock:
            self._limits = limits
        logger.info(
            "Resource limits updated: max_concurrent=%d, max_cpu=%s, max_memory_mb=%s, max_gpu=%s",
            limits.max_concurrent,
            limits.max_cpu,
            limits.max_memory_mb,
            limits.max_gpu,
        )
        self._wakeup.set()

    # ------------------------------------------------------------------
    # Inspection & metrics
    # ------------------------------------------------------------------

    def get_queue_status(self) -> dict[str, Any]:
        """Return a snapshot of queue depth and running counts."""
        with self._lock:
            queued = sum(
                1
                for _, _, tid in self._heap
                if tid in self._tasks
                and tid not in self._cancelled_ids
                and self._tasks[tid].status == TaskStatus.PENDING
            )
            running = len(self._running)
            total = len(self._tasks)
        return {
            "queued": queued,
            "running": running,
            "total_known": total,
            "max_concurrent": self._limits.max_concurrent,
            "max_queue_size": self._max_queue_size,
        }

    def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        """Return the status dict for a specific task, or ``None`` if unknown."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return task.to_dict()

    def get_metrics(self) -> dict[str, Any]:
        """Return aggregate scheduler metrics."""
        with self._lock:
            started = self._started_count
            completed = self._completed_count
            failed = self._failed_count
            cancelled = self._cancelled_count
            total_wait = self._total_wait_seconds
            running = len(self._running)
            queued = sum(
                1
                for _, _, tid in self._heap
                if tid in self._tasks
                and tid not in self._cancelled_ids
                and self._tasks[tid].status == TaskStatus.PENDING
            )
            started_at = self._scheduler_started_at

        avg_wait = (total_wait / started) if started else 0.0
        throughput = 0.0
        if started_at is not None:
            elapsed = (datetime.now(UTC) - started_at).total_seconds()
            throughput = (completed / elapsed) if elapsed > 0 else 0.0

        return {
            "started": started,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "running": running,
            "queued": queued,
            "avg_wait_seconds": round(avg_wait, 6),
            "throughput_per_second": round(throughput, 6),
            "started_at": started_at.isoformat() if started_at else None,
        }

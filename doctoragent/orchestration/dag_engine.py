"""DAG workflow engine for complex task orchestration.

Supports:
- Define tasks with dependencies (DAG structure)
- Topological execution order
- Parallel execution of independent tasks
- Conditional branching
- Retry policies per task
- Progress tracking and state persistence
- Timeout and cancellation

Inspired by Airflow/Prefect but lightweight and embedded.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any

from doctoragent.compat import UTC, StrEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task status
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    """Lifecycle status of a single DAG task.

    Members are strings so they serialise cleanly to JSON / audit logs.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


# Tasks whose status is one of these are considered "terminal": they will not
# change state again unless explicitly reset.
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED}
)

# A dependency in one of these states prevents a dependent task from running;
# the dependent is marked ``SKIPPED`` instead.
_BLOCKING_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED})


# ---------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------


@dataclass
class DAGTask:
    """A single node in the execution DAG.

    Attributes
    ----------
    id:
        Unique identifier for the task (referenced by other tasks'
        ``dependencies``).
    name:
        Human-readable display name.
    callable:
        Async callable invoked as ``await task.callable(task)``. The callable
        receives the :class:`DAGTask` instance so it can read ``task.id``,
        ``task.params`` and (via closures) any shared state it needs. It is
        ``None`` when the task is reconstructed from a serialised dict via
        :meth:`from_dict`; such a task cannot be executed but its results and
        status are preserved.
    dependencies:
        IDs of tasks that must complete successfully before this task may
        start.
    retry_count:
        Number of retries performed so far (reset semantics are owned by the
        engine).
    max_retries:
        Maximum number of *additional* attempts after the first failure. A
        value of ``0`` means "try exactly once".
    timeout_seconds:
        Per-attempt timeout. ``None`` disables the timeout.
    condition:
        Optional callable ``condition(task) -> bool`` evaluated once before the
        first execution attempt. When it returns a falsy value the task is
        marked :attr:`TaskStatus.SKIPPED` without running.
    params:
        Free-form parameters made available to the callable.
    status:
        Current :class:`TaskStatus`.
    result:
        Return value of the callable on success.
    error:
        Human-readable error message on failure.
    started_at / completed_at:
        UTC timestamps bounding the task's execution.
    """

    id: str
    name: str
    callable: Callable[..., Awaitable[Any]] | None = None
    dependencies: list[str] = dc_field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    timeout_seconds: float | None = None
    condition: Callable[[DAGTask], bool] | None = None
    params: dict[str, Any] = dc_field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the task to a plain dict.

        The ``callable`` and ``condition`` callables are **not** serialised
        (they are not JSON-representable); :meth:`from_dict` restores them as
        ``None``. The caller is responsible for re-attaching callables after
        deserialisation if the task still needs to be executed.
        """
        return {
            "id": self.id,
            "name": self.name,
            "dependencies": list(self.dependencies),
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "params": dict(self.params),
            "status": str(self.status),
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DAGTask:
        """Reconstruct a :class:`DAGTask` from :meth:`to_dict` output.

        The ``callable`` and ``condition`` callables cannot be serialised, so
        they are restored as ``None``. Re-attach them manually if the task must
        still be executed.
        """
        status_value = data.get("status", TaskStatus.PENDING)
        try:
            status = TaskStatus(status_value)
        except ValueError:
            status = TaskStatus.PENDING

        started = data.get("started_at")
        completed = data.get("completed_at")
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            callable=None,
            dependencies=list(data.get("dependencies", [])),
            retry_count=int(data.get("retry_count", 0)),
            max_retries=int(data.get("max_retries", 3)),
            timeout_seconds=data.get("timeout_seconds"),
            condition=None,
            params=dict(data.get("params", {})),
            status=status,
            result=data.get("result"),
            error=data.get("error"),
            started_at=datetime.fromisoformat(started) if started else None,
            completed_at=datetime.fromisoformat(completed) if completed else None,
        )


# ---------------------------------------------------------------------------
# DAG engine
# ---------------------------------------------------------------------------


class DAGEngine:
    """Execute a directed acyclic graph of :class:`DAGTask` objects.

    The engine validates the graph (no cycles, all dependencies resolved),
    computes a topological order and runs tasks in *waves*: at each step every
    task whose dependencies have all succeeded is launched concurrently with
    :func:`asyncio.gather`. Tasks whose dependencies fail or are skipped are
    themselves marked :attr:`TaskStatus.SKIPPED`, propagating the abort
    downstream.

    Per-task retry policies, timeouts, conditional execution and cancellation
    are all supported. All mutable state (task statuses, timestamps) is guarded
    by a :class:`threading.Lock` so status queries and cancellation requests
    can be issued safely from other threads while a DAG is executing.
    """

    def __init__(self, tasks: list[DAGTask] | None = None) -> None:
        self._tasks: dict[str, DAGTask] = {}
        self._lock = threading.Lock()
        self._cancelled: bool = False
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None
        for task in tasks or []:
            self.add_task(task)

    # ------------------------------------------------------------------
    # Graph mutation
    # ------------------------------------------------------------------

    def add_task(self, task: DAGTask) -> None:
        """Add a task to the DAG.

        Raises :class:`ValueError` if a task with the same id already exists.
        """
        with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"Task with id {task.id!r} already exists in the DAG")
            self._tasks[task.id] = task
        logger.debug("Added task %r (%s) to DAG", task.id, task.name)

    def remove_task(self, task_id: str) -> bool:
        """Remove a task from the DAG.

        Returns ``True`` if the task was present and removed. A warning is
        logged if any remaining tasks still depend on the removed task (they
        will be flagged by :meth:`validate`).
        """
        with self._lock:
            removed = self._tasks.pop(task_id, None)
        if removed is None:
            return False
        dependents = [t.id for t in self._tasks.values() if task_id in t.dependencies]
        if dependents:
            logger.warning(
                "Removed task %r which is still referenced by: %s",
                task_id,
                ", ".join(dependents),
            )
        logger.debug("Removed task %r from DAG", task_id)
        return True

    # ------------------------------------------------------------------
    # Validation & ordering
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Validate the DAG structure.

        Checks that:
        - every dependency references an existing task,
        - no task depends on itself, and
        - the graph contains no cycles (via Kahn's algorithm).

        Raises :class:`ValueError` on the first violation found.
        """
        with self._lock:
            snapshot = list(self._tasks.values())
        for task in snapshot:
            for dep in task.dependencies:
                if dep == task.id:
                    raise ValueError(f"Task {task.id!r} depends on itself")
                if dep not in self._tasks:
                    raise ValueError(f"Task {task.id!r} depends on unknown task {dep!r}")
        # _topological_order raises if a cycle is detected.
        self._topological_order()
        logger.debug("DAG validated: %d tasks, no cycles", len(snapshot))

    def _topological_order(self) -> list[str]:
        """Return task ids in a valid topological execution order.

        Uses Kahn's algorithm. Raises :class:`ValueError` if the graph contains
        a cycle (the returned order would then cover fewer nodes than the total).
        """
        with self._lock:
            tasks = dict(self._tasks)

        in_degree: dict[str, int] = dict.fromkeys(tasks, 0)
        for task in tasks.values():
            for dep in task.dependencies:
                if dep in in_degree:
                    in_degree[task.id] += 1

        queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for tid, task in tasks.items():
                if node in task.dependencies:
                    in_degree[tid] -= 1
                    if in_degree[tid] == 0:
                        queue.append(tid)

        if len(order) != len(tasks):
            unresolved = [tid for tid in tasks if tid not in order]
            raise ValueError(f"DAG contains a cycle; unresolved tasks: {unresolved}")
        return order

    # ------------------------------------------------------------------
    # Readiness & failure propagation
    # ------------------------------------------------------------------

    def get_ready_tasks(self) -> list[DAGTask]:
        """Return tasks that are ready to execute.

        A task is ready when it is :attr:`TaskStatus.PENDING` and **all** of
        its dependencies are :attr:`TaskStatus.SUCCESS`.
        """
        with self._lock:
            snapshot = list(self._tasks.values())
        ready: list[DAGTask] = []
        for task in snapshot:
            if task.status != TaskStatus.PENDING:
                continue
            if not task.dependencies:
                ready.append(task)
                continue
            deps_ok = all(
                dep in self._tasks and self._tasks[dep].status == TaskStatus.SUCCESS
                for dep in task.dependencies
            )
            if deps_ok:
                ready.append(task)
        return ready

    def _propagate_failures(self) -> None:
        """Mark pending tasks whose dependencies did not succeed as SKIPPED.

        This cascades aborts through the graph: a task depending on a
        FAILED/CANCELLED/SKIPPED task cannot run and is itself skipped.
        """
        with self._lock:
            for task in self._tasks.values():
                if task.status != TaskStatus.PENDING:
                    continue
                for dep_id in task.dependencies:
                    dep = self._tasks.get(dep_id)
                    if dep is None:
                        continue
                    if dep.status in _BLOCKING_STATUSES:
                        task.status = TaskStatus.SKIPPED
                        task.error = f"Dependency {dep_id!r} did not succeed (status={dep.status})"
                        task.completed_at = datetime.now(UTC)
                        logger.info(
                            "Task %r skipped due to dependency %r (%s)",
                            task.id,
                            dep_id,
                            dep.status,
                        )
                        break

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _is_complete(self) -> bool:
        """Whether every task has reached a terminal state."""
        with self._lock:
            return all(
                task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING)
                for task in self._tasks.values()
            )

    async def execute(self) -> dict[str, Any]:
        """Execute the entire DAG.

        Tasks are run in waves: each wave launches every currently-ready task
        concurrently via :func:`asyncio.gather`, then the next wave is computed
        from the updated statuses. Failures cascade downstream (dependents are
        skipped) and the whole DAG can be cancelled mid-flight via
        :meth:`cancel`.

        Returns the result of :meth:`get_status` after execution.
        """
        self.validate()
        with self._lock:
            self._cancelled = False
            self._start_time = datetime.now(UTC)
        logger.info("DAG execution started (%d tasks)", len(self._tasks))
        try:
            while not self._is_complete():
                if self._cancelled:
                    logger.info("DAG execution cancelled by request")
                    break
                self._propagate_failures()
                if self._cancelled:
                    break
                ready = self.get_ready_tasks()
                if not ready:
                    # Nothing ready and nothing running (waves are awaited
                    # synchronously) -> any remaining pending tasks are blocked.
                    break
                await asyncio.gather(
                    *(self.execute_with_retry(t) for t in ready),
                    return_exceptions=True,
                )
        finally:
            with self._lock:
                self._end_time = datetime.now(UTC)
        logger.info("DAG execution finished")
        return self.get_status()

    async def execute_with_retry(self, task: DAGTask) -> None:
        """Execute a single task honouring its retry/timeout/condition policy.

        The task's ``condition`` is evaluated once up front; if falsy the task
        is marked :attr:`TaskStatus.SKIPPED`. Otherwise the callable is invoked
        up to ``1 + max_retries`` times. On permanent failure the task is
        marked :attr:`TaskStatus.FAILED`; cancellation is respected at every
        boundary.
        """
        # Evaluate the gating condition exactly once.
        if task.condition is not None:
            try:
                should_run = bool(task.condition(task))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Condition for task %r raised %s; skipping task", task.id, exc)
                should_run = False
            if not should_run:
                with self._lock:
                    task.status = TaskStatus.SKIPPED
                    task.completed_at = datetime.now(UTC)
                logger.info("Task %r skipped by condition", task.id)
                return

        if task.callable is None:
            with self._lock:
                task.status = TaskStatus.FAILED
                task.error = "task has no callable attached"
                task.completed_at = datetime.now(UTC)
            logger.error("Task %r has no callable; marking FAILED", task.id)
            return

        attempt = 0
        last_error: BaseException | None = None
        while True:
            if self._cancelled or task.status == TaskStatus.CANCELLED:
                with self._lock:
                    if task.status not in _TERMINAL_STATUSES:
                        task.status = TaskStatus.CANCELLED
                        task.completed_at = datetime.now(UTC)
                return

            with self._lock:
                task.status = TaskStatus.RUNNING
                if task.started_at is None:
                    task.started_at = datetime.now(UTC)
                task.retry_count = attempt

            try:
                coro = task.callable(task)
                if task.timeout_seconds is not None:
                    result = await asyncio.wait_for(coro, timeout=task.timeout_seconds)
                else:
                    result = await coro
            except asyncio.CancelledError:
                with self._lock:
                    task.status = TaskStatus.CANCELLED
                    task.completed_at = datetime.now(UTC)
                logger.info("Task %r cancelled", task.id)
                raise
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning("Task %r timed out on attempt %d", task.id, attempt)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("Task %r failed on attempt %d: %s", task.id, attempt, exc)
            else:
                with self._lock:
                    if task.status == TaskStatus.CANCELLED:
                        return
                    task.status = TaskStatus.SUCCESS
                    task.result = result
                    task.completed_at = datetime.now(UTC)
                logger.info("Task %r succeeded on attempt %d", task.id, attempt)
                return

            # Retry budget.
            if attempt < task.max_retries:
                attempt += 1
                continue

            with self._lock:
                if task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.FAILED
                    task.error = str(last_error) if last_error else "unknown error"
                    task.completed_at = datetime.now(UTC)
            logger.error(
                "Task %r failed permanently after %d attempts: %s",
                task.id,
                attempt + 1,
                last_error,
            )
            return

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self, task_id: str | None = None) -> None:
        """Cancel a specific task or the entire DAG.

        When ``task_id`` is ``None`` every non-terminal task is marked
        :attr:`TaskStatus.CANCELLED` and the engine's cancelled flag is set so
        :meth:`execute` stops dispatching new waves. Otherwise only the named
        task (if currently pending or running) is cancelled.
        """
        with self._lock:
            if task_id is None:
                self._cancelled = True
                for task in self._tasks.values():
                    if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                        task.status = TaskStatus.CANCELLED
                        task.completed_at = datetime.now(UTC)
                logger.info("DAG cancelled: all non-terminal tasks marked CANCELLED")
            else:
                task = self._tasks.get(task_id)
                if task is None:
                    logger.warning("Cannot cancel unknown task %r", task_id)
                    return
                if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                    task.status = TaskStatus.CANCELLED
                    task.completed_at = datetime.now(UTC)
                    logger.info("Task %r cancelled", task_id)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return the overall DAG status and per-task statuses."""
        with self._lock:
            counts: dict[TaskStatus, int] = {}
            tasks: dict[str, str] = {}
            for tid, task in self._tasks.items():
                counts[task.status] = counts.get(task.status, 0) + 1
                tasks[tid] = str(task.status)

        overall = self._overall_status(counts)
        return {
            "overall": overall,
            "total_tasks": len(self._tasks),
            "status_counts": {str(k): v for k, v in counts.items()},
            "tasks": tasks,
            "started_at": self._start_time.isoformat() if self._start_time else None,
            "completed_at": self._end_time.isoformat() if self._end_time else None,
            "cancelled": self._cancelled,
        }

    @staticmethod
    def _overall_status(counts: dict[TaskStatus, int]) -> str:
        """Derive a single overall status from per-status counts."""
        if counts.get(TaskStatus.RUNNING, 0) > 0:
            return "running"
        if counts.get(TaskStatus.PENDING, 0) > 0:
            return "pending"
        if counts.get(TaskStatus.FAILED, 0) > 0:
            return "failed"
        if counts.get(TaskStatus.CANCELLED, 0) > 0:
            return "cancelled"
        return "completed"

    def get_execution_graph(self) -> dict[str, Any]:
        """Return the execution graph as a serialisable dict of nodes/edges."""
        with self._lock:
            snapshot = list(self._tasks.values())
        nodes = [
            {
                "id": task.id,
                "name": task.name,
                "status": str(task.status),
                "dependencies": list(task.dependencies),
            }
            for task in snapshot
        ]
        edges = [{"from": dep, "to": task.id} for task in snapshot for dep in task.dependencies]
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the engine state (including all tasks) to a dict."""
        with self._lock:
            return {
                "tasks": [task.to_dict() for task in self._tasks.values()],
                "cancelled": self._cancelled,
                "started_at": (self._start_time.isoformat() if self._start_time else None),
                "completed_at": (self._end_time.isoformat() if self._end_time else None),
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DAGEngine:
        """Reconstruct a :class:`DAGEngine` from :meth:`to_dict` output.

        Task callables cannot be serialised, so restored tasks have
        ``callable=None``. Re-attach callables before re-executing.
        """
        tasks = [DAGTask.from_dict(t) for t in data.get("tasks", [])]
        engine = cls(tasks)
        engine._cancelled = bool(data.get("cancelled", False))
        started = data.get("started_at")
        completed = data.get("completed_at")
        engine._start_time = datetime.fromisoformat(started) if started else None
        engine._end_time = datetime.fromisoformat(completed) if completed else None
        return engine

# mypy: ignore-errors
"""Tests for the orchestration modules.

Covers:
- DAG Engine (task dependencies, parallel execution, cycle detection,
  retry, conditional skipping, cancellation, serialisation)
- Task Scheduler (priority ordering, resource limits, deduplication,
  cancellation, metrics)
- Lifecycle Hooks (registration, priority ordering, proceed=False abort,
  enable/disable)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from doctoragent.orchestration.dag_engine import (
    DAGTask,
    DAGEngine,
    TaskStatus,
)
from doctoragent.orchestration.scheduler import (
    Priority,
    ResourceLimits,
    ScheduledTask,
    TaskScheduler,
)
from doctoragent.orchestration.lifecycle_hooks import (
    Hook,
    HookContext,
    HookType,
    LifecycleHookManager,
)


# ---------------------------------------------------------------------------
# DAG Engine
# ---------------------------------------------------------------------------

class TestDAGEngine:
    """Tests for :class:`DAGEngine`."""

    @staticmethod
    async def _simple_task(task: DAGTask) -> str:
        """A trivial async callable that returns the task id."""
        await asyncio.sleep(0)
        return task.id

    @staticmethod
    async def _failing_task(task: DAGTask) -> None:
        """A callable that always raises."""
        raise RuntimeError("intentional failure")

    def test_add_task(self) -> None:
        engine = DAGEngine()
        task = DAGTask(id="a", name="Task A", callable=self._simple_task)
        engine.add_task(task)
        assert engine.get_status()["total_tasks"] == 1

    def test_add_duplicate_task_raises(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="Task A"))
        with pytest.raises(ValueError, match="already exists"):
            engine.add_task(DAGTask(id="a", name="Duplicate"))

    def test_remove_task(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="Task A"))
        assert engine.remove_task("a") is True
        assert engine.remove_task("nonexistent") is False

    @pytest.mark.asyncio
    async def test_simple_linear_dag(self) -> None:
        """A → B → C: tasks execute in dependency order."""
        engine = DAGEngine()
        results: list[str] = []

        async def task_a(task: DAGTask) -> str:
            results.append("a")
            return "a"

        async def task_b(task: DAGTask) -> str:
            results.append("b")
            return "b"

        async def task_c(task: DAGTask) -> str:
            results.append("c")
            return "c"

        engine.add_task(DAGTask(id="a", name="A", callable=task_a))
        engine.add_task(DAGTask(id="b", name="B", callable=task_b, dependencies=["a"]))
        engine.add_task(DAGTask(id="c", name="C", callable=task_c, dependencies=["b"]))
        status = await engine.execute()
        assert results == ["a", "b", "c"]
        assert status["tasks"]["a"] == str(TaskStatus.SUCCESS)
        assert status["tasks"]["b"] == str(TaskStatus.SUCCESS)
        assert status["tasks"]["c"] == str(TaskStatus.SUCCESS)

    @pytest.mark.asyncio
    async def test_parallel_execution(self) -> None:
        """A → {B, C} → D: B and C run in parallel."""
        engine = DAGEngine()
        order: list[str] = []

        async def make_task(name: str) -> Any:
            async def _run(task: DAGTask) -> str:
                order.append(name)
                await asyncio.sleep(0.01)
                return name
            return _run

        engine.add_task(DAGTask(id="a", name="A", callable=await make_task("a")))
        engine.add_task(DAGTask(id="b", name="B", callable=await make_task("b"), dependencies=["a"]))
        engine.add_task(DAGTask(id="c", name="C", callable=await make_task("c"), dependencies=["a"]))
        engine.add_task(DAGTask(id="d", name="D", callable=await make_task("d"), dependencies=["b", "c"]))
        status = await engine.execute()
        assert order[0] == "a"
        # B and C can be in either order, but both before D.
        assert set(order[1:3]) == {"b", "c"}
        assert order[-1] == "d"
        assert all(
            status["tasks"][t] == str(TaskStatus.SUCCESS) for t in ("a", "b", "c", "d")
        )

    def test_cycle_detection(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A", dependencies=["c"]))
        engine.add_task(DAGTask(id="b", name="B", dependencies=["a"]))
        engine.add_task(DAGTask(id="c", name="C", dependencies=["b"]))
        with pytest.raises(ValueError, match="cycle"):
            engine.validate()

    def test_validate_missing_dependency(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A", dependencies=["ghost"]))
        with pytest.raises(ValueError, match="unknown task"):
            engine.validate()

    def test_validate_self_dependency(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A", dependencies=["a"]))
        with pytest.raises(ValueError, match="depends on itself"):
            engine.validate()

    def test_get_ready_tasks(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A", callable=self._simple_task))
        engine.add_task(DAGTask(id="b", name="B", callable=self._simple_task, dependencies=["a"]))
        ready = engine.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "a"
        # Mark 'a' as SUCCESS.
        engine._tasks["a"].status = TaskStatus.SUCCESS
        ready = engine.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "b"

    @pytest.mark.asyncio
    async def test_retry_on_failure(self) -> None:
        attempt_count = 0

        async def flaky_task(task: DAGTask) -> str:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise RuntimeError("not yet")
            return "success"

        engine = DAGEngine()
        engine.add_task(
            DAGTask(
                id="flaky",
                name="Flaky",
                callable=flaky_task,
                max_retries=3,
            )
        )
        status = await engine.execute()
        assert status["tasks"]["flaky"] == str(TaskStatus.SUCCESS)
        assert attempt_count == 3

    @pytest.mark.asyncio
    async def test_permanent_failure_after_retries(self) -> None:
        engine = DAGEngine()
        engine.add_task(
            DAGTask(
                id="always-fails",
                name="Fails",
                callable=self._failing_task,
                max_retries=1,
            )
        )
        status = await engine.execute()
        assert status["tasks"]["always-fails"] == str(TaskStatus.FAILED)
        task = engine._tasks["always-fails"]
        assert task.error is not None
        assert task.retry_count == 1

    @pytest.mark.asyncio
    async def test_failure_cascades_skip(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A", callable=self._failing_task, max_retries=0))
        engine.add_task(DAGTask(id="b", name="B", callable=self._simple_task, dependencies=["a"]))
        engine.add_task(DAGTask(id="c", name="C", callable=self._simple_task, dependencies=["b"]))
        status = await engine.execute()
        assert status["tasks"]["a"] == str(TaskStatus.FAILED)
        assert status["tasks"]["b"] == str(TaskStatus.SKIPPED)
        assert status["tasks"]["c"] == str(TaskStatus.SKIPPED)

    @pytest.mark.asyncio
    async def test_condition_skips_task(self) -> None:
        ran = False

        async def should_not_run(task: DAGTask) -> str:
            nonlocal ran
            ran = True
            return "ran"

        engine = DAGEngine()
        engine.add_task(
            DAGTask(
                id="conditional",
                name="Conditional",
                callable=should_not_run,
                condition=lambda t: False,
            )
        )
        status = await engine.execute()
        assert status["tasks"]["conditional"] == str(TaskStatus.SKIPPED)
        assert ran is False

    @pytest.mark.asyncio
    async def test_condition_allows_task(self) -> None:
        ran = False

        async def should_run(task: DAGTask) -> str:
            nonlocal ran
            ran = True
            return "ran"

        engine = DAGEngine()
        engine.add_task(
            DAGTask(
                id="conditional",
                name="Conditional",
                callable=should_run,
                condition=lambda t: True,
            )
        )
        status = await engine.execute()
        assert status["tasks"]["conditional"] == str(TaskStatus.SUCCESS)
        assert ran is True

    @pytest.mark.asyncio
    async def test_cancel_during_execution(self) -> None:
        started = False

        async def slow_task(task: DAGTask) -> str:
            nonlocal started
            started = True
            await asyncio.sleep(10)
            return "done"

        engine = DAGEngine()
        engine.add_task(DAGTask(id="slow", name="Slow", callable=slow_task))
        # Cancel after a short delay.
        async def canceller() -> None:
            await asyncio.sleep(0.05)
            engine.cancel()

        await asyncio.gather(engine.execute(), canceller())
        status = engine.get_status()
        assert status["cancelled"] is True
        assert started is True

    def test_cancel_specific_task(self) -> None:
        engine = DAGEngine()
        task = DAGTask(id="a", name="A", callable=self._simple_task)
        engine.add_task(task)
        engine.cancel("a")
        assert task.status == TaskStatus.CANCELLED

    def test_cancel_unknown_task_is_noop(self) -> None:
        engine = DAGEngine()
        # Should not raise.
        engine.cancel("nonexistent")

    @pytest.mark.asyncio
    async def test_no_callable_marks_failed(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="no-cb", name="NoCallback"))
        status = await engine.execute()
        assert status["tasks"]["no-cb"] == str(TaskStatus.FAILED)

    def test_to_dict_and_from_dict_round_trip(self) -> None:
        engine = DAGEngine()
        engine.add_task(
            DAGTask(id="a", name="A", callable=self._simple_task, params={"k": "v"})
        )
        engine.add_task(
            DAGTask(id="b", name="B", callable=self._simple_task, dependencies=["a"])
        )
        data = engine.to_dict()
        assert len(data["tasks"]) == 2

        restored = DAGEngine.from_dict(data)
        assert restored.get_status()["total_tasks"] == 2
        assert restored._tasks["a"].name == "A"
        assert restored._tasks["a"].params == {"k": "v"}
        assert "a" in restored._tasks["b"].dependencies
        # Callables are not serialised.
        assert restored._tasks["a"].callable is None

    def test_get_execution_graph(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A"))
        engine.add_task(DAGTask(id="b", name="B", dependencies=["a"]))
        graph = engine.get_execution_graph()
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1
        assert graph["edges"][0] == {"from": "a", "to": "b"}

    def test_get_status_overall(self) -> None:
        engine = DAGEngine()
        engine.add_task(DAGTask(id="a", name="A"))
        status = engine.get_status()
        assert status["overall"] == "pending"

    @pytest.mark.asyncio
    async def test_timeout_marks_failed(self) -> None:
        async def slow(task: DAGTask) -> None:
            await asyncio.sleep(5)

        engine = DAGEngine()
        engine.add_task(
            DAGTask(id="t", name="Timeout", callable=slow, timeout_seconds=0.05, max_retries=0)
        )
        status = await engine.execute()
        assert status["tasks"]["t"] == str(TaskStatus.FAILED)


# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------

class TestTaskScheduler:
    """Tests for :class:`TaskScheduler`."""

    @staticmethod
    async def _noop(task: ScheduledTask) -> str:
        await asyncio.sleep(0)
        return "ok"

    def test_submit(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=1))
        scheduler.submit(ScheduledTask(id="t1", name="Task 1", callable=self._noop))
        status = scheduler.get_queue_status()
        assert status["total_known"] == 1

    def test_submit_dedup(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=1))
        scheduler.submit(ScheduledTask(id="t1", name="Task 1", callable=self._noop))
        # Same id should be silently skipped.
        scheduler.submit(ScheduledTask(id="t1", name="Duplicate", callable=self._noop))
        assert scheduler.get_queue_status()["total_known"] == 1

    def test_submit_full_queue_raises(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=1), max_queue_size=2)
        scheduler.submit(ScheduledTask(id="t1", name="1"))
        scheduler.submit(ScheduledTask(id="t2", name="2"))
        with pytest.raises(RuntimeError, match="queue full"):
            scheduler.submit(ScheduledTask(id="t3", name="3"))

    @pytest.mark.asyncio
    async def test_priority_ordering(self) -> None:
        """Higher priority tasks are dispatched first."""
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=1))
        execution_order: list[str] = []

        async def record(task: ScheduledTask) -> str:
            execution_order.append(task.id)
            await asyncio.sleep(0.01)
            return "ok"

        scheduler.submit(ScheduledTask(id="low", name="Low", callable=record, priority=Priority.LOW))
        scheduler.submit(ScheduledTask(id="high", name="High", callable=record, priority=Priority.HIGH))
        scheduler.submit(ScheduledTask(id="normal", name="Normal", callable=record, priority=Priority.NORMAL))
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()
        # HIGH should execute before NORMAL before LOW.
        assert execution_order[0] == "high"
        assert execution_order.index("high") < execution_order.index("normal")
        assert execution_order.index("normal") < execution_order.index("low")

    @pytest.mark.asyncio
    async def test_resource_limits_enforced(self) -> None:
        """Tasks exceeding CPU limit are held back."""
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=10, max_cpu=2.0))
        running_concurrent = 0
        max_concurrent_seen = 0

        async def heavy(task: ScheduledTask) -> str:
            nonlocal running_concurrent, max_concurrent_seen
            running_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, running_concurrent)
            await asyncio.sleep(0.05)
            running_concurrent -= 1
            return "ok"

        for i in range(4):
            scheduler.submit(
                ScheduledTask(
                    id=f"cpu-{i}",
                    name=f"CPU {i}",
                    callable=heavy,
                    resource_cost={"cpu": 1.5},
                )
            )
        await scheduler.start()
        await asyncio.sleep(0.3)
        await scheduler.stop()
        # max_cpu=2.0, each task costs 1.5, so only 1 at a time.
        assert max_concurrent_seen <= 1

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=1))
        ran = False

        async def blocker(task: ScheduledTask) -> str:
            await asyncio.sleep(0.1)
            return "ok"

        async def victim(task: ScheduledTask) -> str:
            nonlocal ran
            ran = True
            return "ok"

        scheduler.submit(ScheduledTask(id="blocker", name="Blocker", callable=blocker))
        scheduler.submit(ScheduledTask(id="victim", name="Victim", callable=victim))
        # Cancel the victim before it runs.
        assert scheduler.cancel("victim") is True
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()
        assert ran is False
        assert scheduler.get_task_status("victim")["status"] == str(TaskStatus.CANCELLED)

    def test_cancel_unknown_task_returns_false(self) -> None:
        scheduler = TaskScheduler()
        assert scheduler.cancel("ghost") is False

    @pytest.mark.asyncio
    async def test_metrics(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=2))

        async def quick(task: ScheduledTask) -> str:
            return "ok"

        scheduler.submit(ScheduledTask(id="m1", name="M1", callable=quick))
        scheduler.submit(ScheduledTask(id="m2", name="M2", callable=quick))
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()
        metrics = scheduler.get_metrics()
        assert metrics["started"] >= 2
        assert metrics["completed"] >= 2

    @pytest.mark.asyncio
    async def test_failed_task_recorded(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=2))

        async def fails(task: ScheduledTask) -> None:
            raise ValueError("boom")

        scheduler.submit(ScheduledTask(id="f1", name="F1", callable=fails))
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()
        metrics = scheduler.get_metrics()
        assert metrics["failed"] >= 1
        assert scheduler.get_task_status("f1")["status"] == str(TaskStatus.FAILED)

    @pytest.mark.asyncio
    async def test_set_resource_limits(self) -> None:
        scheduler = TaskScheduler(ResourceLimits(max_concurrent=1))
        scheduler.set_resource_limits(ResourceLimits(max_concurrent=5))
        status = scheduler.get_queue_status()
        assert status["max_concurrent"] == 5

    def test_priority_weight(self) -> None:
        assert Priority.CRITICAL.weight > Priority.URGENT.weight
        assert Priority.URGENT.weight > Priority.HIGH.weight
        assert Priority.HIGH.weight > Priority.NORMAL.weight
        assert Priority.NORMAL.weight > Priority.LOW.weight

    def test_get_task_status_unknown(self) -> None:
        scheduler = TaskScheduler()
        assert scheduler.get_task_status("ghost") is None

    def test_scheduled_task_to_dict(self) -> None:
        task = ScheduledTask(
            id="t1",
            name="Test",
            priority=Priority.HIGH,
            resource_cost={"cpu": 1.0},
            tags=["urgent"],
        )
        data = task.to_dict()
        assert data["id"] == "t1"
        assert data["priority"] == str(Priority.HIGH)
        assert data["resource_cost"] == {"cpu": 1.0}
        assert data["tags"] == ["urgent"]


# ---------------------------------------------------------------------------
# Lifecycle Hooks
# ---------------------------------------------------------------------------

class TestLifecycleHookManager:
    """Tests for :class:`LifecycleHookManager`."""

    @pytest.fixture
    def manager(self) -> LifecycleHookManager:
        return LifecycleHookManager()

    @pytest.fixture
    def context(self) -> HookContext:
        return HookContext(
            hook_type=HookType.ON_RECEIVE,
            task_id="task-1",
        )

    def test_register_and_get_hooks(self, manager: LifecycleHookManager) -> None:
        hook = Hook(
            name="test-hook",
            hook_type=HookType.ON_RECEIVE,
            callback=lambda ctx: None,
        )
        manager.register(hook)
        hooks = manager.get_hooks(HookType.ON_RECEIVE)
        assert len(hooks) == 1
        assert hooks[0].name == "test-hook"

    def test_register_replaces_existing(self, manager: LifecycleHookManager) -> None:
        manager.register(
            Hook(name="h1", hook_type=HookType.ON_RECEIVE, callback=lambda ctx: None)
        )
        manager.register(
            Hook(name="h1", hook_type=HookType.ON_RECEIVE, callback=lambda ctx: None, priority=10)
        )
        hooks = manager.get_hooks(HookType.ON_RECEIVE)
        assert len(hooks) == 1
        assert hooks[0].priority == 10

    def test_unregister(self, manager: LifecycleHookManager) -> None:
        manager.register(
            Hook(name="h1", hook_type=HookType.ON_RECEIVE, callback=lambda ctx: None)
        )
        assert manager.unregister("h1") is True
        assert manager.get_hooks(HookType.ON_RECEIVE) == []
        assert manager.unregister("h1") is False

    @pytest.mark.asyncio
    async def test_run_hooks_in_priority_order(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        order: list[str] = []

        manager.register(
            Hook(
                name="low",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: order.append("low"),
                priority=1,
            )
        )
        manager.register(
            Hook(
                name="high",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: order.append("high"),
                priority=10,
            )
        )
        manager.register(
            Hook(
                name="medium",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: order.append("medium"),
                priority=5,
            )
        )
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert order == ["high", "medium", "low"]

    @pytest.mark.asyncio
    async def test_proceed_false_aborts_chain(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        fired: list[str] = []

        def stop_hook(ctx: HookContext) -> None:
            fired.append("stop")
            ctx.proceed = False

        def after_stop(ctx: HookContext) -> None:
            fired.append("after")

        manager.register(
            Hook(name="stop", hook_type=HookType.ON_RECEIVE, callback=stop_hook, priority=10)
        )
        manager.register(
            Hook(name="after", hook_type=HookType.ON_RECEIVE, callback=after_stop, priority=1)
        )
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert fired == ["stop"]
        assert context.proceed is False

    @pytest.mark.asyncio
    async def test_disabled_hook_skipped(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        fired: list[str] = []

        manager.register(
            Hook(
                name="enabled",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: fired.append("enabled"),
            )
        )
        manager.register(
            Hook(
                name="disabled",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: fired.append("disabled"),
            )
        )
        manager.disable("disabled")
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert "enabled" in fired
        assert "disabled" not in fired

    @pytest.mark.asyncio
    async def test_enable_disabled_hook(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        fired: list[str] = []
        manager.register(
            Hook(
                name="toggle",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: fired.append("toggle"),
            )
        )
        manager.disable("toggle")
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert fired == []
        manager.enable("toggle")
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert "toggle" in fired

    def test_enable_unknown_returns_false(self, manager: LifecycleHookManager) -> None:
        assert manager.enable("ghost") is False

    def test_disable_unknown_returns_false(self, manager: LifecycleHookManager) -> None:
        assert manager.disable("ghost") is False

    @pytest.mark.asyncio
    async def test_async_hook(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        fired: list[str] = []

        async def async_callback(ctx: HookContext) -> None:
            await asyncio.sleep(0)
            fired.append("async")

        manager.register(
            Hook(name="async-hook", hook_type=HookType.ON_RECEIVE, callback=async_callback)
        )
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert "async" in fired

    @pytest.mark.asyncio
    async def test_hook_exception_isolated(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        fired: list[str] = []

        def bad_hook(ctx: HookContext) -> None:
            raise RuntimeError("hook error")

        def good_hook(ctx: HookContext) -> None:
            fired.append("good")

        manager.register(
            Hook(name="bad", hook_type=HookType.ON_RECEIVE, callback=bad_hook, priority=10)
        )
        manager.register(
            Hook(name="good", hook_type=HookType.ON_RECEIVE, callback=good_hook, priority=1)
        )
        result = await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert "good" in fired
        assert result.error is not None
        assert isinstance(result.error, RuntimeError)

    @pytest.mark.asyncio
    async def test_hook_condition_skips(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        fired: list[str] = []

        manager.register(
            Hook(
                name="conditional",
                hook_type=HookType.ON_RECEIVE,
                callback=lambda ctx: fired.append("conditional"),
                condition=lambda ctx: False,
            )
        )
        await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert fired == []

    def test_get_hook_types(self, manager: LifecycleHookManager) -> None:
        manager.register(
            Hook(name="h1", hook_type=HookType.ON_RECEIVE, callback=lambda ctx: None)
        )
        manager.register(
            Hook(name="h2", hook_type=HookType.ON_ARCHIVE, callback=lambda ctx: None)
        )
        types = manager.get_hook_types()
        assert HookType.ON_RECEIVE in types
        assert HookType.ON_ARCHIVE in types

    def test_clear(self, manager: LifecycleHookManager) -> None:
        manager.register(
            Hook(name="h1", hook_type=HookType.ON_RECEIVE, callback=lambda ctx: None)
        )
        manager.register(
            Hook(name="h2", hook_type=HookType.ON_ARCHIVE, callback=lambda ctx: None)
        )
        manager.clear()
        assert manager.get_hooks() == []

    def test_get_hooks_all_types(self, manager: LifecycleHookManager) -> None:
        manager.register(
            Hook(name="h1", hook_type=HookType.ON_RECEIVE, callback=lambda ctx: None)
        )
        manager.register(
            Hook(name="h2", hook_type=HookType.ON_ARCHIVE, callback=lambda ctx: None)
        )
        all_hooks = manager.get_hooks()
        assert len(all_hooks) == 2

    @pytest.mark.asyncio
    async def test_hook_can_modify_metadata(
        self, manager: LifecycleHookManager, context: HookContext
    ) -> None:
        def modifier(ctx: HookContext) -> None:
            ctx.metadata["modified"] = True

        manager.register(
            Hook(name="modifier", hook_type=HookType.ON_RECEIVE, callback=modifier)
        )
        result = await manager.run_hooks(HookType.ON_RECEIVE, context)
        assert result.metadata["modified"] is True

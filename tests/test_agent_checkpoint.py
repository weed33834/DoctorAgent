# mypy: ignore-errors
"""Tests for agent checkpoint persistence (CheckpointStore + AgentCheckpoint)."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from doctoragent.agent import AgentCheckpoint, CheckpointStore


@pytest.fixture
def store(tmp_path: Path) -> CheckpointStore:
    """A CheckpointStore backed by a temp DB file."""
    return CheckpointStore(tmp_path / "checkpoints.db")


@pytest.fixture
def checkpoint() -> AgentCheckpoint:
    """A representative AgentCheckpoint for round-trip tests."""
    return AgentCheckpoint(
        task_id="task-001",
        iteration=3,
        messages=[
            {"role": "system", "content": "you are an assistant"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        plan={"steps": [{"step_id": "s1", "description": "search"}]},
        tool_calls_made=2,
        created_at="2026-07-28T00:00:00+00:00",
        status="paused",
    )


class TestAgentCheckpoint:
    """AgentCheckpoint serialisation / deserialisation."""

    def test_serialization_roundtrip(self, checkpoint: AgentCheckpoint):
        """model_dump_json -> model_validate_json preserves all fields."""
        json_str = checkpoint.model_dump_json()
        restored = AgentCheckpoint.model_validate_json(json_str)
        assert restored.task_id == checkpoint.task_id
        assert restored.iteration == checkpoint.iteration
        assert restored.messages == checkpoint.messages
        assert restored.plan == checkpoint.plan
        assert restored.tool_calls_made == checkpoint.tool_calls_made
        assert restored.created_at == checkpoint.created_at
        assert restored.status == checkpoint.status

    def test_defaults(self):
        """An empty checkpoint has sensible defaults."""
        cp = AgentCheckpoint(task_id="t")
        assert cp.task_id == "t"
        assert cp.iteration == 0
        assert cp.messages == []
        assert cp.plan is None
        assert cp.tool_calls_made == 0
        assert cp.status == "paused"

    def test_plan_none_allowed(self):
        """plan=None is a valid value (planning disabled)."""
        cp = AgentCheckpoint(task_id="t", plan=None)
        assert cp.plan is None


class TestCheckpointStore:
    """CheckpointStore save/load/list/delete."""

    def test_save_and_load(self, store: CheckpointStore, checkpoint: AgentCheckpoint):
        """save then load returns an equal checkpoint."""
        store.save(checkpoint.task_id, checkpoint)
        loaded = store.load(checkpoint.task_id)
        assert loaded is not None
        assert loaded.task_id == checkpoint.task_id
        assert loaded.iteration == checkpoint.iteration
        assert loaded.messages == checkpoint.messages
        assert loaded.plan == checkpoint.plan
        assert loaded.tool_calls_made == checkpoint.tool_calls_made
        assert loaded.status == checkpoint.status

    def test_load_missing_returns_none(self, store: CheckpointStore):
        """load on an unknown task_id returns None."""
        assert store.load("nonexistent-task") is None

    def test_save_overwrites(self, store: CheckpointStore, checkpoint: AgentCheckpoint):
        """saving twice for the same task_id upserts."""
        store.save(checkpoint.task_id, checkpoint)
        modified = checkpoint.model_copy(
            update={"iteration": 10, "status": "completed"}
        )
        store.save(checkpoint.task_id, modified)
        loaded = store.load(checkpoint.task_id)
        assert loaded is not None
        assert loaded.iteration == 10
        assert loaded.status == "completed"

    def test_list_checkpoints(
        self, store: CheckpointStore, checkpoint: AgentCheckpoint
    ):
        """list_checkpoints returns all stored task_ids."""
        assert store.list_checkpoints() == []
        store.save("task-a", checkpoint.model_copy(update={"task_id": "task-a"}))
        store.save("task-b", checkpoint.model_copy(update={"task_id": "task-b"}))
        ids = set(store.list_checkpoints())
        assert ids == {"task-a", "task-b"}

    def test_delete(self, store: CheckpointStore, checkpoint: AgentCheckpoint):
        """delete removes the checkpoint; subsequent load returns None."""
        store.save(checkpoint.task_id, checkpoint)
        assert store.load(checkpoint.task_id) is not None
        store.delete(checkpoint.task_id)
        assert store.load(checkpoint.task_id) is None

    def test_delete_missing_is_noop(self, store: CheckpointStore):
        """deleting a non-existent task_id does not raise."""
        store.delete("never-existed")

    def test_concurrent_saves_no_conflict(
        self, store: CheckpointStore, checkpoint: AgentCheckpoint
    ):
        """Concurrent saves for distinct task_ids don't corrupt the store."""
        results: list[str] = []
        errors: list[BaseException] = []

        def _save(tid: str) -> None:
            try:
                store.save(tid, checkpoint.model_copy(update={"task_id": tid}))
                results.append(tid)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_save, args=(f"task-{i}",)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"concurrent saves raised: {errors}"
        assert sorted(results) == [f"task-{i}" for i in range(10)]
        assert sorted(store.list_checkpoints()) == sorted(results)

    def test_persists_across_store_instances(
        self, store: CheckpointStore, checkpoint: AgentCheckpoint, tmp_path: Path
    ):
        """A checkpoint written by one store is readable by a new store."""
        store.save(checkpoint.task_id, checkpoint)
        reopened = CheckpointStore(store.db_path)
        loaded = reopened.load(checkpoint.task_id)
        assert loaded is not None
        assert loaded.task_id == checkpoint.task_id


class TestAgentShutdownCheckpoint:
    """Agent.aclose() persists the final trajectory as a checkpoint (Fix 5)."""

    async def test_aclose_persists_checkpoint(
        self, store: CheckpointStore
    ) -> None:
        """On shutdown the trajectory is snapshotted when a store is wired."""
        from doctoragent.model.agent import Agent, AgentStep, StepType
        from doctoragent.model.tools import ToolRegistry

        agent = Agent(
            llm_provider=MagicMock(),
            tool_registry=ToolRegistry(),
            checkpoint_store=store,
        )
        agent.trajectory.add_step(
            AgentStep(step_type=StepType.ACTION, content="did something")
        )

        await agent.aclose()

        cp = store.load("shutdown")
        assert cp is not None
        assert cp.task_id == "shutdown"
        assert cp.status == "paused"
        assert cp.iteration == 1  # one ACTION step

    async def test_aclose_noop_without_store(self) -> None:
        """Without a checkpoint_store or trajectory, aclose() is a safe no-op."""
        from doctoragent.model.agent import Agent
        from doctoragent.model.tools import ToolRegistry

        agent = Agent(llm_provider=MagicMock(), tool_registry=ToolRegistry())
        await agent.aclose()  # must not raise

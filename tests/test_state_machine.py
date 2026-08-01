"""Tests for task state machine."""

from uuid import uuid4

import pytest

from doctoragent.orchestration.state_machine import StateMachine, TaskState


def test_valid_transition() -> None:
    """Idle -> Classifying is allowed."""
    sm = StateMachine(uuid4())
    status = sm.transition(TaskState.CLASSIFYING)
    assert status.state == TaskState.CLASSIFYING.name


def test_invalid_transition_raises() -> None:
    """Idle -> Encrypting is not allowed."""
    sm = StateMachine(uuid4())
    with pytest.raises(ValueError):
        sm.transition(TaskState.ENCRYPTING)


def test_terminal_states_have_no_outbound() -> None:
    """Completed state has no allowed outbound transitions."""
    sm = StateMachine(uuid4(), TaskState.COMPLETED)
    assert not sm.can_transition_to(TaskState.IDLE)


# ---- Full happy path ----


def test_full_happy_path() -> None:
    """IDLE -> CLASSIFYING -> ENCRYPTING -> INDEXING -> COMPLETED."""
    sm = StateMachine(uuid4())
    assert sm.state == TaskState.IDLE

    status = sm.transition(TaskState.CLASSIFYING)
    assert status.state == TaskState.CLASSIFYING.name
    assert sm.state == TaskState.CLASSIFYING

    status = sm.transition(TaskState.ENCRYPTING)
    assert status.state == TaskState.ENCRYPTING.name
    assert sm.state == TaskState.ENCRYPTING

    status = sm.transition(TaskState.INDEXING)
    assert status.state == TaskState.INDEXING.name
    assert sm.state == TaskState.INDEXING

    status = sm.transition(TaskState.COMPLETED)
    assert status.state == TaskState.COMPLETED.name
    assert sm.state == TaskState.COMPLETED


# ---- Transitions to FAILED from each non-terminal state ----


@pytest.mark.parametrize(
    "start_state",
    [
        TaskState.IDLE,
        TaskState.CLASSIFYING,
        TaskState.ENCRYPTING,
        TaskState.INDEXING,
    ],
)
def test_transition_to_failed(start_state: TaskState) -> None:
    """Every non-terminal state can transition to FAILED."""
    sm = StateMachine(uuid4(), start_state)
    status = sm.transition(TaskState.FAILED)
    assert status.state == TaskState.FAILED.name
    assert sm.state == TaskState.FAILED


# ---- Transitions to QUARANTINED from each non-terminal state ----


@pytest.mark.parametrize(
    "start_state",
    [
        TaskState.IDLE,
        TaskState.CLASSIFYING,
        TaskState.ENCRYPTING,
        TaskState.INDEXING,
    ],
)
def test_transition_to_quarantined(start_state: TaskState) -> None:
    """Every non-terminal state can transition to QUARANTINED."""
    sm = StateMachine(uuid4(), start_state)
    status = sm.transition(TaskState.QUARANTINED)
    assert status.state == TaskState.QUARANTINED.name
    assert sm.state == TaskState.QUARANTINED


# ---- Terminal states have no outbound transitions ----


@pytest.mark.parametrize(
    "terminal_state",
    [TaskState.COMPLETED, TaskState.QUARANTINED],
)
def test_terminal_state_no_outbound(terminal_state: TaskState) -> None:
    """COMPLETED and QUARANTINED have no outbound transitions."""
    sm = StateMachine(uuid4(), terminal_state)
    for target in TaskState:
        assert not sm.can_transition_to(target), (
            f"{terminal_state.name} should not transition to {target.name}"
        )


@pytest.mark.parametrize(
    "terminal_state",
    [TaskState.COMPLETED, TaskState.QUARANTINED],
)
def test_terminal_state_transition_raises(terminal_state: TaskState) -> None:
    """Attempting any transition from a terminal state raises ValueError."""
    sm = StateMachine(uuid4(), terminal_state)
    for target in TaskState:
        with pytest.raises(ValueError, match="Invalid transition"):
            sm.transition(target)


def test_failed_can_transition_to_idle() -> None:
    """FAILED can transition to IDLE to support retry."""
    sm = StateMachine(uuid4(), TaskState.FAILED)
    assert sm.can_transition_to(TaskState.IDLE) is True
    status = sm.transition(TaskState.IDLE)
    assert status.state == TaskState.IDLE.name
    assert sm.state == TaskState.IDLE


# ---- can_transition_to returns True for valid transitions ----


@pytest.mark.parametrize(
    "start_state,target_state",
    [
        (TaskState.IDLE, TaskState.CLASSIFYING),
        (TaskState.IDLE, TaskState.FAILED),
        (TaskState.IDLE, TaskState.QUARANTINED),
        (TaskState.CLASSIFYING, TaskState.ENCRYPTING),
        (TaskState.CLASSIFYING, TaskState.QUARANTINED),
        (TaskState.CLASSIFYING, TaskState.FAILED),
        (TaskState.ENCRYPTING, TaskState.INDEXING),
        (TaskState.ENCRYPTING, TaskState.QUARANTINED),
        (TaskState.ENCRYPTING, TaskState.FAILED),
        (TaskState.INDEXING, TaskState.COMPLETED),
        (TaskState.INDEXING, TaskState.QUARANTINED),
        (TaskState.INDEXING, TaskState.FAILED),
    ],
)
def test_can_transition_to_valid(start_state: TaskState, target_state: TaskState) -> None:
    """can_transition_to returns True for all valid transitions."""
    sm = StateMachine(uuid4(), start_state)
    assert sm.can_transition_to(target_state) is True


# ---- can_transition_to returns False for invalid transitions ----


@pytest.mark.parametrize(
    "start_state,target_state",
    [
        (TaskState.IDLE, TaskState.ENCRYPTING),
        (TaskState.IDLE, TaskState.INDEXING),
        (TaskState.IDLE, TaskState.COMPLETED),
        (TaskState.CLASSIFYING, TaskState.IDLE),
        (TaskState.CLASSIFYING, TaskState.INDEXING),
        (TaskState.CLASSIFYING, TaskState.COMPLETED),
        (TaskState.ENCRYPTING, TaskState.IDLE),
        (TaskState.ENCRYPTING, TaskState.CLASSIFYING),
        (TaskState.ENCRYPTING, TaskState.COMPLETED),
        (TaskState.INDEXING, TaskState.IDLE),
        (TaskState.INDEXING, TaskState.CLASSIFYING),
        (TaskState.INDEXING, TaskState.ENCRYPTING),
    ],
)
def test_can_transition_to_invalid(start_state: TaskState, target_state: TaskState) -> None:
    """can_transition_to returns False for invalid transitions."""
    sm = StateMachine(uuid4(), start_state)
    assert sm.can_transition_to(target_state) is False


# ---- Specific invalid transitions raise ValueError ----


def test_classifying_to_idle_raises() -> None:
    """CLASSIFYING -> IDLE is not a valid transition."""
    sm = StateMachine(uuid4(), TaskState.CLASSIFYING)
    with pytest.raises(ValueError, match="Invalid transition"):
        sm.transition(TaskState.IDLE)


@pytest.mark.parametrize(
    "target_state",
    list(TaskState),
)
def test_completed_to_any_state_raises(target_state: TaskState) -> None:
    """COMPLETED -> any state raises ValueError."""
    sm = StateMachine(uuid4(), TaskState.COMPLETED)
    with pytest.raises(ValueError, match="Invalid transition"):
        sm.transition(target_state)

"""Task state machine for DoctorAgent."""

from enum import Enum, auto
from typing import ClassVar
from uuid import UUID

from doctoragent.api.schemas import TaskStatus


class TaskState(Enum):
    """States of a file processing task."""

    IDLE = auto()
    CLASSIFYING = auto()
    ENCRYPTING = auto()
    INDEXING = auto()
    COMPLETED = auto()
    FAILED = auto()
    QUARANTINED = auto()
    # Phase 8.3 多 Agent 协作子任务状态
    SPLITTING = auto()  # 父任务正在分解
    DISPATCHED = auto()  # 子任务已派发给 worker
    AGGREGATING = auto()  # 正在聚合子任务结果
    PENDING_CHILD = auto()  # 子任务等待处理


class StateMachine:
    """Simple finite state machine for tasks."""

    ALLOWED_TRANSITIONS: ClassVar[dict[TaskState, set[TaskState]]] = {
        TaskState.IDLE: {
            TaskState.CLASSIFYING,
            TaskState.FAILED,
            TaskState.QUARANTINED,
            # Phase 8.3：父任务分解、子任务创建入口
            TaskState.SPLITTING,
            TaskState.PENDING_CHILD,
        },
        TaskState.CLASSIFYING: {TaskState.ENCRYPTING, TaskState.QUARANTINED, TaskState.FAILED},
        TaskState.ENCRYPTING: {TaskState.INDEXING, TaskState.QUARANTINED, TaskState.FAILED},
        TaskState.INDEXING: {TaskState.COMPLETED, TaskState.QUARANTINED, TaskState.FAILED},
        TaskState.COMPLETED: set(),
        TaskState.FAILED: {TaskState.IDLE},
        TaskState.QUARANTINED: set(),
        # Phase 8.3 子任务协作转移
        TaskState.SPLITTING: {TaskState.DISPATCHED, TaskState.FAILED},
        TaskState.DISPATCHED: {TaskState.AGGREGATING, TaskState.FAILED},
        TaskState.AGGREGATING: {TaskState.COMPLETED, TaskState.FAILED},
        TaskState.PENDING_CHILD: {TaskState.CLASSIFYING, TaskState.FAILED},
    }

    def __init__(self, task_id: UUID, initial: TaskState = TaskState.IDLE) -> None:
        self.task_id = task_id
        self.state = initial

    def transition(self, new_state: TaskState) -> TaskStatus:
        """Transition to a new state if allowed."""
        if new_state not in self.ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"Invalid transition from {self.state.name} to {new_state.name}")
        self.state = new_state
        return TaskStatus(task_id=self.task_id, state=self.state.name)

    def can_transition_to(self, new_state: TaskState) -> bool:
        """Check if transition is allowed."""
        return new_state in self.ALLOWED_TRANSITIONS[self.state]

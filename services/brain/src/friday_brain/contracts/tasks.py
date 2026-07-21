import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, Field, ConfigDict
from friday_brain.contracts.errors import InvalidStateTransitionError


class TaskState(str, Enum):
    """Represents the lifecycle of a task."""

    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(BaseModel):
    """
    Represents a single task being processed by the brain.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    input: str
    state: TaskState = TaskState.PENDING
    client_request_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: dict[str, Any] | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def update_state(self, new_state: TaskState) -> Self:
        """
        Updates the task's state, ensuring valid transitions.
        """
        if not self.is_valid_transition(new_state):
            raise InvalidStateTransitionError(self.state, new_state)

        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)
        return self

    def is_valid_transition(self, new_state: TaskState) -> bool:
        """
        Checks if a state transition is valid.
        """
        if self.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}:
            return False  # Terminal states

        valid_transitions: dict[TaskState, set[TaskState]] = {
            TaskState.PENDING: {
                TaskState.PLANNING,
                TaskState.CANCELLATION_REQUESTED,
                TaskState.FAILED,
            },
            TaskState.PLANNING: {
                TaskState.EXECUTING,
                TaskState.CANCELLATION_REQUESTED,
                TaskState.FAILED,
            },
            TaskState.EXECUTING: {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLATION_REQUESTED,
            },
            TaskState.CANCELLATION_REQUESTED: {TaskState.CANCELLED},
        }

        return new_state in valid_transitions.get(self.state, set())

    model_config = ConfigDict(use_enum_values=True, from_attributes=True)

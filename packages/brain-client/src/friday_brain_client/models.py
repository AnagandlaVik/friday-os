from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TaskState(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_TASK_STATES = frozenset(
    {
        TaskState.CANCELLED,
        TaskState.COMPLETED,
        TaskState.FAILED,
    }
)


class CreateTaskRequest(BaseModel):
    input: str = Field(min_length=1)
    client_request_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, str] | None = None

    model_config = ConfigDict(extra="forbid")


class TaskResponse(BaseModel):
    id: UUID
    input: str
    state: TaskState
    client_request_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: dict[str, Any] | None = None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES

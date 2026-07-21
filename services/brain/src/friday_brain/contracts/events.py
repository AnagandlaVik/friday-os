import uuid
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, ConfigDict

from .tasks import TaskState

PayloadType = TypeVar("PayloadType", bound=BaseModel)


class Event(BaseModel, Generic[PayloadType]):
    """
    Canonical event envelope for all events in the system.
    """

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    schema_version: int = 1
    task_id: uuid.UUID
    correlation_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    source: str = "friday-brain"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: PayloadType
    metadata: dict[str, Any] = Field(default_factory=dict)


# Event Payloads
class TaskCreatedPayload(BaseModel):
    input: str
    state: TaskState
    client_request_id: str | None = None
    idempotency_key: str | None = None


class TaskPlanningStartedPayload(BaseModel):
    pass


class TaskPlanValidatedPayload(BaseModel):
    plan_id: uuid.UUID


class TaskExecutionStartedPayload(BaseModel):
    pass


class TaskCompletedPayload(BaseModel):
    result: Any


class TaskFailedPayload(BaseModel):
    error_code: str
    error_message: str
    error_details: dict[str, Any] | None = None


class TaskCancelledPayload(BaseModel):
    pass


class EmptyEventPayload(BaseModel):
    """Payload for lifecycle events that carry no additional data."""

    model_config = ConfigDict(extra="forbid")

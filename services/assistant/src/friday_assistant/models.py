from typing import Any
from uuid import UUID

from friday_brain_client import TaskState
from pydantic import BaseModel, ConfigDict, Field


class AssistRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=20_000,
    )
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
    )

    model_config = ConfigDict(extra="forbid")


class AssistResponse(BaseModel):
    request_id: str
    task_id: UUID
    state: TaskState
    response: str
    result: Any | None = None

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "friday-assistant"

    model_config = ConfigDict(extra="forbid")

from pydantic import BaseModel, Field, ConfigDict
from .tasks import Task


class CreateTaskRequest(BaseModel):
    """
    Request model for creating a new task.
    """

    input: str = Field(
        ..., max_length=10000, description="The main input or prompt for the task."
    )
    client_request_id: str | None = Field(
        None,
        max_length=256,
        description="A client-provided identifier for the request.",
    )
    idempotency_key: str | None = Field(
        None, max_length=256, description="A key to prevent duplicate task creation."
    )
    metadata: dict[str, str] | None = Field(
        None, description="Optional metadata for the task."
    )


class TaskResponse(Task):
    """
    Response model for a task, hiding certain internal fields if necessary.
    For now, it's a direct mapping of the Task model.
    """

    model_config = ConfigDict(use_enum_values=True, from_attributes=True)


class HealthResponse(BaseModel):
    """
    Response model for the health check endpoint.
    """

    status: str = "ok"
    service: str = "friday-brain"

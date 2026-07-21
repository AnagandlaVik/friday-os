from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from friday_brain.contracts.authorization import (
    AuthorizationEventType,
)
from friday_brain.contracts.tasks import Task
from friday_brain.contracts.tools import ToolPermission


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


class GrantTaskPermissionRequest(BaseModel):
    """Grant one capability to a specific task."""

    permission: ToolPermission
    expires_in_sec: int | None = Field(
        default=None,
        ge=1,
        le=86400,
        description=(
            "Optional grant lifetime. Omit for a grant "
            "that remains active until revoked."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class GrantToolConfirmationRequest(BaseModel):
    """Grant one exact-call confirmation."""

    expires_in_sec: int = Field(
        default=300,
        ge=1,
        le=3600,
    )

    model_config = ConfigDict(extra="forbid")


class TaskPermissionGrantResponse(BaseModel):
    grant_id: UUID
    task_id: UUID
    permission: ToolPermission
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
    )


class ToolConfirmationGrantResponse(BaseModel):
    grant_id: UUID
    task_id: UUID
    checkpoint_id: UUID
    tool_name: str
    arguments_digest: str
    granted_by: str
    granted_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuthorizationEventResponse(BaseModel):
    event_id: UUID
    task_id: UUID
    checkpoint_id: UUID | None
    event_type: AuthorizationEventType
    permission: ToolPermission | None
    tool_name: str | None
    arguments_digest: str | None
    actor_id: str | None
    reason: str | None
    details: dict[str, object]
    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
    )


class AuthorizationEventListResponse(BaseModel):
    events: list[AuthorizationEventResponse]


class ComponentHealthResponse(BaseModel):
    healthy: bool
    duration_ms: float = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class ReadinessResponse(HealthResponse):
    version: str
    build_sha: str
    environment: str
    components: dict[
        str,
        ComponentHealthResponse,
    ]


class VersionResponse(BaseModel):
    service: str = "friday-brain"
    version: str
    build_sha: str
    environment: str

    model_config = ConfigDict(extra="forbid")

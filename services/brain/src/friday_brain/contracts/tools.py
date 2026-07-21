from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class ToolRiskLevel(str, Enum):
    """Security impact of invoking a tool."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolPermission(str, Enum):
    """Capabilities a tool may require."""

    READ_DATA = "read_data"
    WRITE_DATA = "write_data"
    NETWORK_ACCESS = "network_access"
    FILESYSTEM_ACCESS = "filesystem_access"
    PROCESS_EXECUTION = "process_execution"
    USER_NOTIFICATION = "user_notification"


class ToolRetryPolicy(BaseModel):
    """Retry behavior declared by a tool."""

    max_attempts: int = Field(default=1, ge=1, le=10)
    base_delay_sec: float = Field(default=0.0, ge=0)
    max_delay_sec: float = Field(default=0.0, ge=0)
    retryable_error_codes: frozenset[str] = Field(default_factory=frozenset)

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_delays(self) -> Self:
        if self.max_delay_sec < self.base_delay_sec:
            raise ValueError(
                "Maximum retry delay cannot be shorter than the base delay."
            )

        return self


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Immutable metadata and schemas for a registered tool."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW
    permissions: frozenset[ToolPermission] = field(default_factory=frozenset)
    requires_confirmation: bool = False
    timeout_sec: float = 30.0
    retry_policy: ToolRetryPolicy = field(default_factory=ToolRetryPolicy)

    def __post_init__(self) -> None:
        if not _TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "Tool names must use lowercase letters, numbers, "
                "dots, underscores, or hyphens."
            )

        if not self.description.strip():
            raise ValueError("Tool description cannot be empty.")

        if self.timeout_sec <= 0:
            raise ValueError("Tool timeout must be greater than zero.")


class ToolInvocation(BaseModel):
    """Durable request to invoke one registered tool."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    task_id: UUID
    checkpoint_id: UUID

    model_config = ConfigDict(extra="forbid")


class ToolExecutionError(BaseModel):
    """Structured tool failure returned to execution code."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )


class ToolExecutionResult(BaseModel):
    """Structured success or failure from a tool invocation."""

    success: bool
    output: Any | None = None
    error: ToolExecutionError | None = None

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.success and self.error is not None:
            raise ValueError("A successful tool result cannot contain an error.")

        if not self.success and self.error is None:
            raise ValueError("A failed tool result must contain an error.")

        return self

    @classmethod
    def succeeded(
        cls,
        output: Any = None,
    ) -> Self:
        return cls(
            success=True,
            output=output,
        )

    @classmethod
    def failed(
        cls,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> Self:
        return cls(
            success=False,
            error=ToolExecutionError(
                code=code,
                message=message,
                retryable=retryable,
                details=details,
            ),
        )

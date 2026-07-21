import uuid
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# Public Error Model
class ErrorResponse(BaseModel):
    """
    Standardized error response for the API.
    """

    code: str = Field(description="A stable, machine-readable error code.")
    message: str = Field(description="A safe, human-readable error message.")
    correlation_id: str | None = Field(
        None, description="The correlation ID for the request."
    )
    retryable: bool = Field(
        False, description="Indicates if the operation can be retried."
    )
    details: dict[str, Any] | None = Field(
        None, description="Optional structured details about the error."
    )


# Domain-Specific Exceptions
class BrainError(Exception):
    """Base exception for all application-level errors."""

    def __init__(
        self,
        message: str,
        code: str = "internal_error",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.details = details


class TaskNotFoundError(BrainError):
    """Raised when a task is not found."""

    def __init__(self, task_id: UUID):
        super().__init__(f"Task '{task_id}' not found", code="task_not_found")


class InvalidStateTransitionError(BrainError):
    """Raised on an invalid task state transition."""

    def __init__(self, from_state: str, to_state: str):
        message = f"Invalid state transition from '{from_state}' to '{to_state}'"
        super().__init__(message, code="invalid_state_transition")


class IdempotencyConflictError(BrainError):
    """Raised on idempotency key conflict."""

    def __init__(self, idempotency_key: str):
        message = (
            f"Idempotency key '{idempotency_key}' conflicts with a previous request."
        )
        super().__init__(message, code="idempotency_conflict")


class InvalidPlanError(BrainError):
    """Raised when a plan is invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            f"Invalid plan: {message}", code="invalid_plan", details=details
        )


class ToolNotAllowedError(BrainError):
    """Raised when a tool is not allowed by policy."""

    def __init__(self, tool_name: str):
        super().__init__(f"Tool '{tool_name}' is not allowed", code="tool_not_allowed")


class ValidationError(BrainError):
    """Raised for general validation errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="validation_error", details=details)


class TaskConcurrencyConflictError(BrainError):
    """Raised when a task is updated using a stale version."""

    def __init__(
        self,
        task_id: uuid.UUID,
        expected_version: int,
    ) -> None:
        super().__init__(
            f"Task '{task_id}' was modified by another operation.",
            code="task_concurrency_conflict",
            details={
                "task_id": str(task_id),
                "expected_version": expected_version,
            },
        )

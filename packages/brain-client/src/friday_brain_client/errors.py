from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from friday_brain_client.models import TaskResponse


class BrainClientError(RuntimeError):
    """Base exception raised by the FRIDAY Brain client."""


class BrainConnectionError(BrainClientError):
    """Raised when the Brain API cannot be reached."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BrainProtocolError(BrainClientError):
    """Raised when the Brain API returns an invalid response."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BrainResponseError(BrainClientError):
    """Structured non-success response from the Brain API."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details
        self.correlation_id = correlation_id

        super().__init__(
            f"Brain API request failed with {status_code} ({code}): {message}"
        )


class BrainTaskTimeoutError(BrainClientError):
    """Raised when a task does not reach a terminal state in time."""

    def __init__(
        self,
        *,
        task_id: UUID,
        timeout_sec: float,
    ) -> None:
        self.task_id = task_id
        self.timeout_sec = timeout_sec

        super().__init__(
            f"Brain task {task_id} did not finish within {timeout_sec:g} seconds."
        )


class BrainTaskFailedError(BrainClientError):
    """Raised when run_task receives a failed terminal task."""

    def __init__(self, task: "TaskResponse") -> None:
        self.task = task
        error = task.error or {}
        self.code = str(error.get("code", "task_failed"))
        self.details = error.get("details")

        message = str(
            error.get(
                "message",
                f"Brain task {task.id} failed.",
            )
        )

        super().__init__(message)


class BrainTaskCancelledError(BrainClientError):
    """Raised when run_task receives a cancelled terminal task."""

    def __init__(self, task: "TaskResponse") -> None:
        self.task = task
        super().__init__(f"Brain task {task.id} was cancelled.")

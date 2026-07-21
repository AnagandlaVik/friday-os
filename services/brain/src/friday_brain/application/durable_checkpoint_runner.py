import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from friday_brain.application.secure_tool_runtime import (
    ToolExecutionFailedError,
)
from friday_brain.contracts.tasks import Task
from friday_brain.protocols.execution_lease_repository import (
    ExecutionLease,
)
from friday_brain.protocols.execution_plan_repository import (
    ExecutionPlanRepository,
    PersistedPlan,
    StepCheckpoint,
)
from friday_brain.protocols.step_executor import StepExecutor


RunStatus = Literal[
    "completed",
    "waiting_retry",
    "cancelled",
]

CancellationCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class CheckpointRunResult:
    status: RunStatus
    output: Any | None = None
    retry_available_at: datetime | None = None


class LeaseLostError(RuntimeError):
    """Raised when a checkpoint mutation is rejected by lease fencing."""


class DurableCheckpointError(RuntimeError):
    """Raised after a checkpoint exhausts its execution attempts."""

    def __init__(
        self,
        checkpoint_id: UUID,
        error: dict[str, Any] | None,
    ) -> None:
        self.checkpoint_id = checkpoint_id
        self.error = error

        message = (
            str(error.get("message", "Checkpoint execution failed."))
            if error
            else "Checkpoint execution failed."
        )

        super().__init__(message)


class InvalidCheckpointStateError(RuntimeError):
    """Raised when persisted plan and checkpoint state disagree."""


class DurableCheckpointRunner:
    """
    Executes a validated plan through durable, lease-fenced checkpoints.

    The runner performs no retry sleeping. A delayed retry is persisted and
    returned to the caller so another processing pass can resume it later.
    """

    def __init__(
        self,
        plan_repository: ExecutionPlanRepository,
        step_executor: StepExecutor,
        *,
        max_attempts: int = 3,
        retry_delay_sec: float = 1.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("Maximum attempts must be at least one.")

        if retry_delay_sec < 0:
            raise ValueError("Retry delay cannot be negative.")

        self._plan_repository = plan_repository
        self._step_executor = step_executor
        self._max_attempts = max_attempts
        self._retry_delay_sec = retry_delay_sec

    async def run(
        self,
        *,
        task: Task,
        persisted_plan: PersistedPlan,
        lease: ExecutionLease,
        cancellation_check: CancellationCheck | None = None,
    ) -> CheckpointRunResult:
        self._validate_identity(
            task=task,
            persisted_plan=persisted_plan,
            lease=lease,
        )

        while True:
            checkpoints = await self._plan_repository.list_checkpoints(
                persisted_plan.plan_id
            )

            self._validate_checkpoints(
                persisted_plan=persisted_plan,
                checkpoints=checkpoints,
            )

            terminal_result = self._terminal_result(checkpoints)

            if terminal_result is not None:
                return terminal_result

            if cancellation_check is not None and await cancellation_check():
                cancelled = await self._plan_repository.cancel_incomplete_checkpoints(
                    task_id=task.id,
                    lease_token=lease.lease_token,
                )

                cancellable_count = sum(
                    checkpoint.status
                    in {
                        "pending",
                        "executing",
                        "retry_wait",
                    }
                    for checkpoint in checkpoints
                )

                if cancellable_count > 0 and cancelled == 0:
                    raise LeaseLostError("Lease was lost while cancelling checkpoints.")

                return CheckpointRunResult(status="cancelled")

            candidate, retry_at = self._select_candidate(
                persisted_plan=persisted_plan,
                checkpoints=checkpoints,
            )

            if candidate is None:
                if retry_at is not None:
                    return CheckpointRunResult(
                        status="waiting_retry",
                        retry_available_at=retry_at,
                    )

                raise InvalidCheckpointStateError(
                    "No runnable checkpoint exists, but the plan is not complete."
                )

            step = persisted_plan.plan.steps[candidate.step_index]

            started = await self._claim_checkpoint(
                checkpoint=candidate,
                lease=lease,
            )

            try:
                output = await self._step_executor.execute_step(
                    step=step,
                    task=task,
                    checkpoint_id=started.checkpoint_id,
                    idempotency_key=started.idempotency_key,
                )
            except asyncio.CancelledError:
                # Leave the checkpoint in executing state. A worker with a
                # future valid lease can reclaim it using the same key.
                raise
            except Exception as exc:
                return await self._record_failure(
                    checkpoint=started,
                    lease=lease,
                    exception=exc,
                )

            completed = await self._plan_repository.complete_checkpoint(
                checkpoint_id=started.checkpoint_id,
                lease_token=lease.lease_token,
                output=output,
            )

            if completed is None:
                raise LeaseLostError(
                    "Lease was lost while completing checkpoint "
                    f"{started.checkpoint_id}."
                )

    async def _claim_checkpoint(
        self,
        *,
        checkpoint: StepCheckpoint,
        lease: ExecutionLease,
    ) -> StepCheckpoint:
        if checkpoint.status == "executing":
            started = await self._plan_repository.resume_checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                lease_token=lease.lease_token,
            )
        else:
            started = await self._plan_repository.start_checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                lease_token=lease.lease_token,
            )

        if started is None:
            raise LeaseLostError(
                "Lease was lost or checkpoint state changed while "
                f"claiming {checkpoint.checkpoint_id}."
            )

        return started

    async def _record_failure(
        self,
        *,
        checkpoint: StepCheckpoint,
        lease: ExecutionLease,
        exception: Exception,
    ) -> CheckpointRunResult:
        retryable = True
        max_attempts = self._max_attempts
        retry_delay_sec = self._retry_delay_sec
        error_code = type(exception).__name__
        error_details: dict[str, Any] | None = None

        if isinstance(
            exception,
            ToolExecutionFailedError,
        ):
            retryable = exception.retryable
            max_attempts = min(
                self._max_attempts,
                exception.max_attempts,
            )
            error_code = exception.code
            error_details = exception.details

            if exception.base_delay_sec > 0:
                retry_delay_sec = exception.base_delay_sec * (
                    2
                    ** max(
                        checkpoint.attempt_count - 1,
                        0,
                    )
                )

                if exception.max_delay_sec > 0:
                    retry_delay_sec = min(
                        retry_delay_sec,
                        exception.max_delay_sec,
                    )

        error = {
            "type": type(exception).__name__,
            "code": error_code,
            "message": str(exception),
            "retryable": retryable,
            "details": error_details,
        }

        if retryable and checkpoint.attempt_count < max_attempts:
            retrying = await self._plan_repository.schedule_checkpoint_retry(
                checkpoint_id=checkpoint.checkpoint_id,
                lease_token=lease.lease_token,
                error=error,
                retry_delay_sec=retry_delay_sec,
            )

            if retrying is None:
                raise LeaseLostError(
                    "Lease was lost while scheduling checkpoint "
                    f"{checkpoint.checkpoint_id} for retry."
                )

            return CheckpointRunResult(
                status="waiting_retry",
                retry_available_at=(retrying.retry_available_at),
            )

        failed = await self._plan_repository.fail_checkpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            lease_token=lease.lease_token,
            error=error,
        )

        if failed is None:
            raise LeaseLostError(
                f"Lease was lost while failing checkpoint {checkpoint.checkpoint_id}."
            )

        raise DurableCheckpointError(
            checkpoint_id=failed.checkpoint_id,
            error=failed.error,
        )

    @staticmethod
    def _select_candidate(
        *,
        persisted_plan: PersistedPlan,
        checkpoints: list[StepCheckpoint],
    ) -> tuple[StepCheckpoint | None, datetime | None]:
        completed_step_ids = {
            persisted_plan.plan.steps[checkpoint.step_index].id
            for checkpoint in checkpoints
            if checkpoint.status == "completed"
        }

        earliest_retry: datetime | None = None
        now = datetime.now(timezone.utc)

        for checkpoint in checkpoints:
            if checkpoint.status in {
                "completed",
                "failed",
                "cancelled",
            }:
                continue

            step = persisted_plan.plan.steps[checkpoint.step_index]

            if not all(
                dependency in completed_step_ids for dependency in step.dependencies
            ):
                continue

            if checkpoint.status == "retry_wait":
                retry_at = checkpoint.retry_available_at

                if retry_at is None:
                    raise InvalidCheckpointStateError(
                        "A retry-wait checkpoint has no retry timestamp."
                    )

                if retry_at > now:
                    if earliest_retry is None or retry_at < earliest_retry:
                        earliest_retry = retry_at
                    continue

            return checkpoint, earliest_retry

        return None, earliest_retry

    @staticmethod
    def _terminal_result(
        checkpoints: list[StepCheckpoint],
    ) -> CheckpointRunResult | None:
        failed = next(
            (checkpoint for checkpoint in checkpoints if checkpoint.status == "failed"),
            None,
        )

        if failed is not None:
            raise DurableCheckpointError(
                checkpoint_id=failed.checkpoint_id,
                error=failed.error,
            )

        if any(checkpoint.status == "cancelled" for checkpoint in checkpoints):
            return CheckpointRunResult(status="cancelled")

        if checkpoints and all(
            checkpoint.status == "completed" for checkpoint in checkpoints
        ):
            final_checkpoint = max(
                checkpoints,
                key=lambda checkpoint: checkpoint.step_index,
            )

            return CheckpointRunResult(
                status="completed",
                output=final_checkpoint.output,
            )

        return None

    @staticmethod
    def _validate_identity(
        *,
        task: Task,
        persisted_plan: PersistedPlan,
        lease: ExecutionLease,
    ) -> None:
        if persisted_plan.task_id != task.id:
            raise ValueError("Persisted plan does not belong to the task.")

        if lease.task_id != task.id:
            raise ValueError("Execution lease does not belong to the task.")

        if persisted_plan.execution_attempt > lease.execution_attempt:
            raise ValueError("Plan execution attempt is newer than the lease.")

    @staticmethod
    def _validate_checkpoints(
        *,
        persisted_plan: PersistedPlan,
        checkpoints: list[StepCheckpoint],
    ) -> None:
        steps = persisted_plan.plan.steps

        if len(checkpoints) != len(steps):
            raise InvalidCheckpointStateError(
                "Checkpoint count does not match plan step count."
            )

        for expected_index, checkpoint in enumerate(checkpoints):
            if checkpoint.plan_id != persisted_plan.plan_id:
                raise InvalidCheckpointStateError("Checkpoint belongs to another plan.")

            if checkpoint.task_id != persisted_plan.task_id:
                raise InvalidCheckpointStateError("Checkpoint belongs to another task.")

            if checkpoint.step_index != expected_index:
                raise InvalidCheckpointStateError(
                    "Checkpoints are not in contiguous step order."
                )

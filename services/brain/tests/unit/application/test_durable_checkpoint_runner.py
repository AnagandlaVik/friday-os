from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from friday_brain.application.durable_checkpoint_runner import (
    DurableCheckpointError,
    DurableCheckpointRunner,
    LeaseLostError,
)
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.protocols.execution_lease_repository import (
    ExecutionLease,
)
from friday_brain.protocols.execution_plan_repository import (
    PersistedPlan,
    StepCheckpoint,
)


class FakePlanRepository:
    def __init__(
        self,
        checkpoints: list[StepCheckpoint],
    ) -> None:
        self.checkpoints = checkpoints
        self.reject_mutations = False
        self.cancelled_count = 0

    async def list_checkpoints(
        self,
        plan_id: UUID,
    ) -> list[StepCheckpoint]:
        return [
            deepcopy(checkpoint)
            for checkpoint in self.checkpoints
            if checkpoint.plan_id == plan_id
        ]

    async def start_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
    ) -> StepCheckpoint | None:
        del lease_token

        if self.reject_mutations:
            return None

        checkpoint = self._get(checkpoint_id)

        if checkpoint.status not in {
            "pending",
            "retry_wait",
        }:
            return None

        replacement = self._replace(
            checkpoint,
            status="executing",
            attempt_count=checkpoint.attempt_count + 1,
            started_at=datetime.now(timezone.utc),
            retry_available_at=None,
            error=None,
        )
        return deepcopy(replacement)

    async def resume_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
    ) -> StepCheckpoint | None:
        del lease_token

        if self.reject_mutations:
            return None

        checkpoint = self._get(checkpoint_id)

        if checkpoint.status != "executing":
            return None

        replacement = self._replace(
            checkpoint,
            attempt_count=checkpoint.attempt_count + 1,
            started_at=datetime.now(timezone.utc),
        )
        return deepcopy(replacement)

    async def complete_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        output: Any,
    ) -> StepCheckpoint | None:
        del lease_token

        if self.reject_mutations:
            return None

        checkpoint = self._get(checkpoint_id)
        replacement = self._replace(
            checkpoint,
            status="completed",
            output=output,
            completed_at=datetime.now(timezone.utc),
        )
        return deepcopy(replacement)

    async def schedule_checkpoint_retry(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        error: dict[str, Any],
        retry_delay_sec: float,
    ) -> StepCheckpoint | None:
        del lease_token

        if self.reject_mutations:
            return None

        checkpoint = self._get(checkpoint_id)
        replacement = self._replace(
            checkpoint,
            status="retry_wait",
            error=error,
            retry_available_at=(
                datetime.now(timezone.utc) + timedelta(seconds=retry_delay_sec)
            ),
        )
        return deepcopy(replacement)

    async def fail_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        error: dict[str, Any],
    ) -> StepCheckpoint | None:
        del lease_token

        if self.reject_mutations:
            return None

        checkpoint = self._get(checkpoint_id)
        replacement = self._replace(
            checkpoint,
            status="failed",
            error=error,
            completed_at=datetime.now(timezone.utc),
        )
        return deepcopy(replacement)

    async def cancel_incomplete_checkpoints(
        self,
        task_id: UUID,
        lease_token: UUID,
    ) -> int:
        del lease_token

        if self.reject_mutations:
            return 0

        count = 0

        for checkpoint in list(self.checkpoints):
            if checkpoint.task_id == task_id and checkpoint.status in {
                "pending",
                "executing",
                "retry_wait",
            }:
                self._replace(
                    checkpoint,
                    status="cancelled",
                    completed_at=datetime.now(timezone.utc),
                )
                count += 1

        self.cancelled_count += count
        return count

    def _get(self, checkpoint_id: UUID) -> StepCheckpoint:
        return next(
            checkpoint
            for checkpoint in self.checkpoints
            if checkpoint.checkpoint_id == checkpoint_id
        )

    def _replace(
        self,
        checkpoint: StepCheckpoint,
        **changes: Any,
    ) -> StepCheckpoint:
        values = {
            field: getattr(checkpoint, field)
            for field in checkpoint.__dataclass_fields__
        }
        values.update(changes)
        values["updated_at"] = datetime.now(timezone.utc)

        replacement = StepCheckpoint(**values)
        index = self.checkpoints.index(checkpoint)
        self.checkpoints[index] = replacement
        return replacement


class RecordingStepExecutor:
    def __init__(
        self,
        *,
        failures: int = 0,
    ) -> None:
        self.failures = failures
        self.calls: list[tuple[UUID, str]] = []

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        checkpoint_id: UUID,
        idempotency_key: str,
        lease_token: UUID | None = None,
    ) -> Any:
        del task
        self.calls.append((step.id, idempotency_key))

        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("temporary tool failure")

        return {
            "operation": step.operation,
            "message": step.arguments.get("message"),
        }


def build_execution(
    statuses: list[str],
    *,
    attempt_counts: list[int] | None = None,
) -> tuple[
    Task,
    PersistedPlan,
    ExecutionLease,
    list[StepCheckpoint],
]:
    now = datetime.now(timezone.utc)
    task = Task(input="durable execution")

    first = PlanStep(
        operation="echo",
        arguments={"message": "first"},
    )
    second = PlanStep(
        operation="echo",
        arguments={"message": "second"},
        dependencies=[first.id],
    )
    plan = Plan(
        task_id=task.id,
        steps=[first, second],
    )

    persisted = PersistedPlan(
        plan_id=plan.id,
        task_id=task.id,
        task_version=task.version,
        execution_attempt=1,
        schema_version=1,
        status="validated",
        plan=plan,
        created_at=now,
        validated_at=now,
        invalidated_at=None,
    )

    lease = ExecutionLease(
        task_id=task.id,
        lease_token=uuid4(),
        worker_id="worker-one",
        acquired_at=now,
        heartbeat_at=now,
        expires_at=now + timedelta(seconds=30),
        execution_attempt=1,
    )

    counts = attempt_counts or [0, 0]
    checkpoints: list[StepCheckpoint] = []

    for index, status in enumerate(statuses):
        checkpoints.append(
            StepCheckpoint(
                checkpoint_id=uuid4(),
                task_id=task.id,
                plan_id=plan.id,
                step_index=index,
                operation=plan.steps[index].operation,
                arguments=plan.steps[index].arguments,
                status=status,  # type: ignore[arg-type]
                attempt_count=counts[index],
                idempotency_key=(f"friday:{task.id}:{plan.id}:{index}"),
                output=({"message": "already done"} if status == "completed" else None),
                error=None,
                started_at=(now if status in {"executing", "completed"} else None),
                completed_at=(now if status == "completed" else None),
                retry_available_at=None,
                created_at=now,
                updated_at=now,
            )
        )

    return task, persisted, lease, checkpoints


@pytest.mark.asyncio
async def test_runner_skips_completed_checkpoint() -> None:
    task, persisted, lease, checkpoints = build_execution(["completed", "pending"])
    repository = FakePlanRepository(checkpoints)
    executor = RecordingStepExecutor()
    runner = DurableCheckpointRunner(repository, executor)

    result = await runner.run(
        task=task,
        persisted_plan=persisted,
        lease=lease,
    )

    assert result.status == "completed"
    assert result.output == {
        "operation": "echo",
        "message": "second",
    }
    assert executor.calls == [
        (
            persisted.plan.steps[1].id,
            checkpoints[1].idempotency_key,
        )
    ]


@pytest.mark.asyncio
async def test_runner_recovers_executing_checkpoint() -> None:
    task, persisted, lease, checkpoints = build_execution(
        ["executing", "pending"],
        attempt_counts=[1, 0],
    )
    repository = FakePlanRepository(checkpoints)
    executor = RecordingStepExecutor()
    runner = DurableCheckpointRunner(repository, executor)

    result = await runner.run(
        task=task,
        persisted_plan=persisted,
        lease=lease,
    )

    assert result.status == "completed"
    assert repository.checkpoints[0].attempt_count == 2
    assert executor.calls[0][1] == checkpoints[0].idempotency_key


@pytest.mark.asyncio
async def test_runner_persists_retry_without_sleeping() -> None:
    task, persisted, lease, checkpoints = build_execution(["pending", "pending"])
    repository = FakePlanRepository(checkpoints)
    executor = RecordingStepExecutor(failures=1)
    runner = DurableCheckpointRunner(
        repository,
        executor,
        max_attempts=3,
        retry_delay_sec=10,
    )

    result = await runner.run(
        task=task,
        persisted_plan=persisted,
        lease=lease,
    )

    assert result.status == "waiting_retry"
    assert result.retry_available_at is not None
    assert repository.checkpoints[0].status == "retry_wait"
    assert repository.checkpoints[0].attempt_count == 1
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_runner_fails_after_attempt_limit() -> None:
    task, persisted, lease, checkpoints = build_execution(
        ["executing", "pending"],
        attempt_counts=[2, 0],
    )
    repository = FakePlanRepository(checkpoints)
    executor = RecordingStepExecutor(failures=1)
    runner = DurableCheckpointRunner(
        repository,
        executor,
        max_attempts=3,
    )

    with pytest.raises(
        DurableCheckpointError,
        match="temporary tool failure",
    ):
        await runner.run(
            task=task,
            persisted_plan=persisted,
            lease=lease,
        )

    assert repository.checkpoints[0].status == "failed"
    assert repository.checkpoints[0].attempt_count == 3


@pytest.mark.asyncio
async def test_runner_stops_when_lease_is_rejected() -> None:
    task, persisted, lease, checkpoints = build_execution(["pending", "pending"])
    repository = FakePlanRepository(checkpoints)
    repository.reject_mutations = True
    executor = RecordingStepExecutor()
    runner = DurableCheckpointRunner(repository, executor)

    with pytest.raises(LeaseLostError):
        await runner.run(
            task=task,
            persisted_plan=persisted,
            lease=lease,
        )

    assert executor.calls == []


@pytest.mark.asyncio
async def test_runner_cancels_before_next_step() -> None:
    task, persisted, lease, checkpoints = build_execution(["pending", "pending"])
    repository = FakePlanRepository(checkpoints)
    executor = RecordingStepExecutor()
    runner = DurableCheckpointRunner(repository, executor)

    async def cancellation_check() -> bool:
        return True

    result = await runner.run(
        task=task,
        persisted_plan=persisted,
        lease=lease,
        cancellation_check=cancellation_check,
    )

    assert result.status == "cancelled"
    assert repository.cancelled_count == 2
    assert executor.calls == []


@pytest.mark.asyncio
async def test_nonretryable_tool_failure_fails_immediately() -> None:
    from friday_brain.application.secure_tool_runtime import (
        ToolExecutionFailedError,
    )
    from friday_brain.contracts.tools import ToolExecutionError

    task, persisted, lease, checkpoints = build_execution(["pending", "pending"])
    repository = FakePlanRepository(checkpoints)

    class NonretryableExecutor:
        async def execute_step(
            self,
            step: PlanStep,
            task: Task,
            checkpoint_id: UUID,
            idempotency_key: str,
            lease_token: UUID | None = None,
        ) -> Any:
            del step
            del task
            del checkpoint_id
            del idempotency_key

            raise ToolExecutionFailedError(
                error=ToolExecutionError(
                    code="permission_denied",
                    message="Permission denied.",
                    retryable=False,
                ),
                max_attempts=5,
                base_delay_sec=1.0,
                max_delay_sec=10.0,
            )

    runner = DurableCheckpointRunner(
        repository,
        NonretryableExecutor(),
        max_attempts=5,
    )

    with pytest.raises(
        DurableCheckpointError,
        match="Permission denied",
    ):
        await runner.run(
            task=task,
            persisted_plan=persisted,
            lease=lease,
        )

    assert repository.checkpoints[0].status == "failed"
    assert repository.checkpoints[0].attempt_count == 1
    assert repository.checkpoints[0].error is not None
    assert repository.checkpoints[0].error["retryable"] is False

import asyncio
import logging
from contextlib import suppress
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from friday_brain.application.durable_checkpoint_runner import (
    CheckpointRunResult,
    DurableCheckpointError,
    DurableCheckpointRunner,
    LeaseLostError,
)
from friday_brain.contracts.errors import BrainError
from friday_brain.contracts.events import (
    Event,
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskExecutionStartedPayload,
    TaskFailedPayload,
    TaskPlanningStartedPayload,
    TaskPlanValidatedPayload,
)
from friday_brain.contracts.tasks import Task, TaskState
from friday_brain.protocols.execution_lease_repository import (
    ExecutionLease,
    ExecutionLeaseRepository,
)
from friday_brain.protocols.execution_plan_repository import (
    ExecutionPlanRepository,
    PersistedPlan,
)
from friday_brain.protocols.planner import Planner
from friday_brain.protocols.plan_validator import PlanValidator
from friday_brain.protocols.task_repository import TaskRepository


logger = logging.getLogger(__name__)


class DurableTaskProcessor:
    """Runs tasks through durable plans and lease-fenced checkpoints."""

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        lease_repository: ExecutionLeaseRepository,
        plan_repository: ExecutionPlanRepository,
        planner: Planner,
        plan_validator: PlanValidator,
        checkpoint_runner: DurableCheckpointRunner,
        worker_id: str,
        lease_duration_sec: float = 30.0,
        heartbeat_interval_sec: float = 10.0,
    ) -> None:
        if not worker_id:
            raise ValueError("Worker ID cannot be empty.")

        if lease_duration_sec <= 0:
            raise ValueError("Lease duration must be greater than zero.")

        if heartbeat_interval_sec <= 0:
            raise ValueError("Heartbeat interval must be greater than zero.")

        if heartbeat_interval_sec >= lease_duration_sec:
            raise ValueError(
                "Heartbeat interval must be shorter than the lease duration."
            )

        self._task_repository = task_repository
        self._lease_repository = lease_repository
        self._plan_repository = plan_repository
        self._planner = planner
        self._plan_validator = plan_validator
        self._checkpoint_runner = checkpoint_runner
        self._worker_id = worker_id
        self._lease_duration_sec = lease_duration_sec
        self._heartbeat_interval_sec = heartbeat_interval_sec

    async def process_task(self, task_id: UUID) -> Task:
        task = await self._get_task_or_fail(task_id)

        if self._is_terminal(task):
            return task

        lease = await self._lease_repository.acquire(
            task_id=task_id,
            worker_id=self._worker_id,
            lease_duration_sec=self._lease_duration_sec,
        )

        if lease is None:
            # Another worker currently owns this task.
            return await self._get_task_or_fail(task_id)

        try:
            return await self._run_with_heartbeat(
                task_id=task_id,
                lease=lease,
            )
        except LeaseLostError:
            logger.warning(
                "Worker %s lost the execution lease for task %s.",
                self._worker_id,
                task_id,
            )
            return await self._get_task_or_fail(task_id)
        except DurableCheckpointError as error:
            logger.error(
                "Task %s exhausted checkpoint %s.",
                task_id,
                error.checkpoint_id,
                exc_info=True,
            )
            return await self._fail_task(
                task_id=task_id,
                error_code="checkpoint_failed",
                error_message=str(error),
                error_details={
                    "checkpoint_id": str(error.checkpoint_id),
                    "checkpoint_error": error.error,
                },
                lease_token=lease.lease_token,
            )
        except BrainError as error:
            logger.error(
                "Task %s failed with domain error %s.",
                task_id,
                error.code,
                exc_info=True,
            )
            return await self._fail_task(
                task_id=task_id,
                error_code=error.code,
                error_message=error.message,
                error_details=error.details,
                lease_token=lease.lease_token,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Task %s failed during durable processing.",
                task_id,
            )
            return await self._fail_task(
                task_id=task_id,
                error_code="internal_error",
                error_message=("An unexpected internal error occurred."),
                error_details=None,
                lease_token=lease.lease_token,
            )
        finally:
            await self._lease_repository.release(
                task_id=task_id,
                lease_token=lease.lease_token,
                worker_id=self._worker_id,
            )

    async def _run_with_heartbeat(
        self,
        *,
        task_id: UUID,
        lease: ExecutionLease,
    ) -> Task:
        processing = asyncio.create_task(
            self._process_owned_task(
                task_id=task_id,
                lease=lease,
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop(lease))

        done, _ = await asyncio.wait(
            {processing, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if processing in done:
            heartbeat.cancel()

            with suppress(asyncio.CancelledError):
                await heartbeat

            return await processing

        processing.cancel()

        with suppress(asyncio.CancelledError):
            await processing

        # Propagate a heartbeat exception when one occurred.
        await heartbeat

        raise LeaseLostError(f"Execution lease for task {task_id} was lost.")

    async def _heartbeat_loop(
        self,
        lease: ExecutionLease,
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_sec)

            renewed = await self._lease_repository.renew(
                task_id=lease.task_id,
                lease_token=lease.lease_token,
                worker_id=self._worker_id,
                lease_duration_sec=self._lease_duration_sec,
            )

            if renewed is None:
                raise LeaseLostError(
                    f"Execution lease for task {lease.task_id} could not be renewed."
                )

    async def _process_owned_task(
        self,
        *,
        task_id: UUID,
        lease: ExecutionLease,
    ) -> Task:
        task = await self._get_task_or_fail(task_id)

        if self._is_terminal(task):
            return task

        if task.state == TaskState.CANCELLATION_REQUESTED:
            return await self._cancel_task(
                task=task,
                lease_token=lease.lease_token,
            )

        if task.state == TaskState.PENDING:
            task = await self._transition_with_event(
                task=task,
                new_state=TaskState.PLANNING,
                event_type="task.planning_started",
                payload=TaskPlanningStartedPayload(),
            )

        persisted_plan = await self._plan_repository.get_validated_plan(task_id)

        if persisted_plan is None:
            if task.state != TaskState.PLANNING:
                raise RuntimeError("An executing task has no validated plan.")

            persisted_plan = await self._create_plan(
                task=task,
                lease=lease,
            )

        task = await self._get_task_or_fail(task_id)

        if task.state == TaskState.CANCELLATION_REQUESTED:
            return await self._cancel_task(
                task=task,
                lease_token=lease.lease_token,
            )

        if task.state == TaskState.PLANNING:
            task = await self._transition_with_event(
                task=task,
                new_state=TaskState.EXECUTING,
                event_type="task.execution_started",
                payload=TaskExecutionStartedPayload(),
            )
        elif task.state != TaskState.EXECUTING:
            if self._is_terminal(task):
                return task

            raise RuntimeError(
                f"Task {task.id} cannot execute from state {task.state}."
            )

        run_result = await self._checkpoint_runner.run(
            task=task,
            persisted_plan=persisted_plan,
            lease=lease,
            cancellation_check=lambda: self._is_cancellation_requested(task_id),
        )

        return await self._finish_run(
            task_id=task_id,
            lease=lease,
            result=run_result,
        )

    async def _create_plan(
        self,
        *,
        task: Task,
        lease: ExecutionLease,
    ) -> PersistedPlan:
        plan = await self._planner.create_plan(task)

        current = await self._get_task_or_fail(task.id)

        if current.state == TaskState.CANCELLATION_REQUESTED:
            raise LeaseLostError("Planning stopped because cancellation was requested.")

        await self._plan_validator.validate_plan(
            plan,
            current,
        )

        persisted = await self._plan_repository.save_validated_plan(
            task_id=current.id,
            task_version=current.version,
            execution_attempt=lease.execution_attempt,
            lease_token=lease.lease_token,
            plan=plan,
        )

        if persisted is None:
            raise LeaseLostError("Lease was lost while persisting the plan.")

        await self._append_event(
            task=current,
            event_type="task.plan_validated",
            payload=TaskPlanValidatedPayload(plan_id=persisted.plan_id),
        )

        return persisted

    async def _finish_run(
        self,
        *,
        task_id: UUID,
        lease: ExecutionLease,
        result: CheckpointRunResult,
    ) -> Task:
        task = await self._get_task_or_fail(task_id)

        if result.status == "waiting_retry":
            # The task stays executing. A later recovery pass will
            # reacquire it after retry_available_at.
            return task

        if (
            result.status == "cancelled"
            or task.state == TaskState.CANCELLATION_REQUESTED
        ):
            return await self._cancel_task(
                task=task,
                lease_token=lease.lease_token,
            )

        if result.status != "completed":
            raise RuntimeError(f"Unknown checkpoint result: {result.status}")

        if task.state != TaskState.EXECUTING:
            if self._is_terminal(task):
                return task

            raise RuntimeError(
                "Completed checkpoints belong to a task that is not executing."
            )

        task.result = result.output

        return await self._transition_with_event(
            task=task,
            new_state=TaskState.COMPLETED,
            event_type="task.completed",
            payload=TaskCompletedPayload(result=result.output),
        )

    async def _cancel_task(
        self,
        *,
        task: Task,
        lease_token: UUID,
    ) -> Task:
        if task.state == TaskState.CANCELLED:
            return task

        if task.state != TaskState.CANCELLATION_REQUESTED:
            return task

        await self._plan_repository.cancel_incomplete_checkpoints(
            task_id=task.id,
            lease_token=lease_token,
        )

        return await self._transition_with_event(
            task=task,
            new_state=TaskState.CANCELLED,
            event_type="task.cancelled",
            payload=TaskCancelledPayload(),
        )

    async def _fail_task(
        self,
        *,
        task_id: UUID,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any] | None,
        lease_token: UUID,
    ) -> Task:
        task = await self._get_task_or_fail(task_id)

        if task.state == TaskState.CANCELLATION_REQUESTED:
            return await self._cancel_task(
                task=task,
                lease_token=lease_token,
            )

        if self._is_terminal(task):
            return task

        task.error = {
            "code": error_code,
            "message": error_message,
            "details": error_details,
        }

        return await self._transition_with_event(
            task=task,
            new_state=TaskState.FAILED,
            event_type="task.failed",
            payload=TaskFailedPayload(
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
            ),
        )

    async def _is_cancellation_requested(
        self,
        task_id: UUID,
    ) -> bool:
        task = await self._task_repository.get(task_id)

        return task is not None and task.state == TaskState.CANCELLATION_REQUESTED

    async def _get_task_or_fail(
        self,
        task_id: UUID,
    ) -> Task:
        task = await self._task_repository.get(task_id)

        if task is None:
            raise RuntimeError(f"Task {task_id} was not found.")

        return task

    async def _transition_with_event(
        self,
        *,
        task: Task,
        new_state: TaskState,
        event_type: str,
        payload: BaseModel,
    ) -> Task:
        expected_version = task.version
        task.update_state(new_state)

        event = self._make_event(
            event_type=event_type,
            task=task,
            payload=payload,
        )

        return await self._task_repository.update_with_event(
            task=task,
            expected_version=expected_version,
            event=event,
        )

    async def _append_event(
        self,
        *,
        task: Task,
        event_type: str,
        payload: BaseModel,
    ) -> None:
        event = self._make_event(
            event_type=event_type,
            task=task,
            payload=payload,
        )

        await self._task_repository.append_event(
            task_id=task.id,
            expected_version=task.version,
            event=event,
        )

    @staticmethod
    def _make_event(
        *,
        event_type: str,
        task: Task,
        payload: BaseModel,
    ) -> Event[Any]:
        return Event(
            event_type=event_type,
            task_id=task.id,
            correlation_id=task.id,
            payload=payload,
        )

    @staticmethod
    def _is_terminal(task: Task) -> bool:
        return task.state in {
            TaskState.CANCELLED,
            TaskState.COMPLETED,
            TaskState.FAILED,
        }

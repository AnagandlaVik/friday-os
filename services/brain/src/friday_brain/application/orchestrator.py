import asyncio
import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.errors import (
    BrainError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    TaskConcurrencyConflictError,
    TaskNotFoundError,
)
from friday_brain.contracts.events import (
    EmptyEventPayload,
    Event,
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskExecutionStartedPayload,
    TaskFailedPayload,
    TaskPlanningStartedPayload,
    TaskPlanValidatedPayload,
)
from friday_brain.application.durable_task_processor import DurableTaskProcessor
from friday_brain.contracts.tasks import Task, TaskState
from friday_brain.protocols.planner import Planner
from friday_brain.protocols.plan_validator import PlanValidator
from friday_brain.protocols.task_repository import TaskRepository
from friday_brain.protocols.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)


class Orchestrator:
    """Orchestrates the complete lifecycle of a task."""

    def __init__(
        self,
        task_repository: TaskRepository,
        planner: Planner,
        plan_validator: PlanValidator,
        tool_executor: ToolExecutor,
        durable_processor: DurableTaskProcessor | None = None,
    ) -> None:
        self._task_repository = task_repository
        self._planner = planner
        self._plan_validator = plan_validator
        self._tool_executor = tool_executor
        self._durable_processor = durable_processor

    async def create_task(
        self,
        request: CreateTaskRequest,
        correlation_id: UUID,
    ) -> tuple[Task, bool]:
        if request.idempotency_key:
            existing_task = await self._task_repository.find_by_idempotency_key(
                request.idempotency_key
            )
            if existing_task:
                if self._is_equivalent_request(existing_task, request):
                    return existing_task, False
                raise IdempotencyConflictError(request.idempotency_key)

        task = Task(
            input=request.input,
            client_request_id=request.client_request_id,
            idempotency_key=request.idempotency_key,
            metadata=request.metadata or {},
        )

        event = self._make_event(
            event_type="task.created",
            task=task,
            correlation_id=correlation_id,
            payload=TaskCreatedPayload(
                input=task.input,
                state=task.state,
                client_request_id=task.client_request_id,
                idempotency_key=task.idempotency_key,
            ),
        )

        persisted = await self._task_repository.create_with_event(
            task,
            event,
        )

        return persisted, persisted.id == task.id

    async def start_task_processing(self, task: Task) -> None:
        """Start task processing without blocking the request."""
        asyncio.create_task(self.process_task(task.id))

    async def process_task(self, task_id: UUID) -> Task:
        if self._durable_processor is not None:
            return await self._durable_processor.process_task(task_id)

        try:
            task = await self._get_task_or_fail(task_id)

            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            task = await self._transition_with_event(
                task=task,
                new_state=TaskState.PLANNING,
                event_type="task.planning_started",
                correlation_id=task.id,
                payload=TaskPlanningStartedPayload(),
            )

            plan = await self._planner.create_plan(task)

            task = await self._get_task_or_fail(task_id)
            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            await self._plan_validator.validate_plan(plan, task)

            await self._append_event(
                task=task,
                event_type="task.plan_validated",
                correlation_id=task.id,
                payload=TaskPlanValidatedPayload(plan_id=plan.id),
            )

            task = await self._get_task_or_fail(task_id)

            task = await self._transition_with_event(
                task=task,
                new_state=TaskState.EXECUTING,
                event_type="task.execution_started",
                correlation_id=task.id,
                payload=TaskExecutionStartedPayload(),
            )

            task = await self._get_task_or_fail(task_id)
            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            result = await self._tool_executor.execute_plan(plan, task)

            task = await self._get_task_or_fail(task_id)
            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            task.result = result

            await self._transition_with_event(
                task=task,
                new_state=TaskState.COMPLETED,
                event_type="task.completed",
                correlation_id=task.id,
                payload=TaskCompletedPayload(result=result),
            )

        except BrainError as error:
            logger.error(
                "Task %s failed with domain error %s.",
                task_id,
                error.code,
                exc_info=True,
            )
            await self._fail_task(
                task_id,
                error.code,
                error.message,
                error.details,
            )
        except Exception:
            logger.exception(
                "Task %s failed with an unexpected error.",
                task_id,
            )
            await self._fail_task(
                task_id,
                "internal_error",
                "An unexpected internal error occurred.",
            )

        return await self._get_task_or_fail(task_id)

    async def request_cancellation(self, task_id: UUID) -> Task:
        task = await self._get_task_or_fail(task_id)

        if task.state in {
            TaskState.CANCELLED,
            TaskState.CANCELLATION_REQUESTED,
            TaskState.COMPLETED,
            TaskState.FAILED,
        }:
            return task

        try:
            return await self._transition_with_event(
                task=task,
                new_state=TaskState.CANCELLATION_REQUESTED,
                event_type="task.cancellation_requested",
                correlation_id=task.id,
                payload=EmptyEventPayload(),
            )
        except (
            InvalidStateTransitionError,
            TaskConcurrencyConflictError,
        ):
            return await self._get_task_or_fail(task_id)

    async def get_task(self, task_id: UUID) -> Task:
        """Retrieve a task by ID."""
        return await self._get_task_or_fail(task_id)

    async def _check_and_handle_cancellation(
        self,
        task: Task,
    ) -> bool:
        if task.state != TaskState.CANCELLATION_REQUESTED:
            return False

        await self._transition_with_event(
            task=task,
            new_state=TaskState.CANCELLED,
            event_type="task.cancelled",
            correlation_id=task.id,
            payload=TaskCancelledPayload(),
        )
        return True

    async def _fail_task(
        self,
        task_id: UUID,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            task = await self._get_task_or_fail(task_id)

            if task.state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.CANCELLATION_REQUESTED,
            }:
                return

            task.error = {
                "code": code,
                "message": message,
                "details": details,
            }

            await self._transition_with_event(
                task=task,
                new_state=TaskState.FAILED,
                event_type="task.failed",
                correlation_id=task.id,
                payload=TaskFailedPayload(
                    error_code=code,
                    error_message=message,
                    error_details=details,
                ),
            )
        except Exception:
            logger.exception(
                "Failed to transition task %s to FAILED.",
                task_id,
            )

    async def _get_task_or_fail(self, task_id: UUID) -> Task:
        task = await self._task_repository.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    async def _transition_with_event(
        self,
        task: Task,
        new_state: TaskState,
        event_type: str,
        correlation_id: UUID,
        payload: BaseModel,
    ) -> Task:
        expected_version = task.version
        task.update_state(new_state)

        event = self._make_event(
            event_type=event_type,
            task=task,
            correlation_id=correlation_id,
            payload=payload,
        )

        return await self._task_repository.update_with_event(
            task=task,
            expected_version=expected_version,
            event=event,
        )

    async def _append_event(
        self,
        task: Task,
        event_type: str,
        correlation_id: UUID,
        payload: BaseModel,
    ) -> None:
        event = self._make_event(
            event_type=event_type,
            task=task,
            correlation_id=correlation_id,
            payload=payload,
        )

        await self._task_repository.append_event(
            task_id=task.id,
            expected_version=task.version,
            event=event,
        )

    @staticmethod
    def _make_event(
        event_type: str,
        task: Task,
        correlation_id: UUID,
        payload: BaseModel,
    ) -> Event[Any]:
        return Event(
            event_type=event_type,
            task_id=task.id,
            correlation_id=correlation_id,
            payload=payload,
        )

    @staticmethod
    def _is_equivalent_request(
        task: Task,
        request: CreateTaskRequest,
    ) -> bool:
        return task.input == request.input

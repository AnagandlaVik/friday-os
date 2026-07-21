
import asyncio
import logging
from uuid import UUID
from pydantic import BaseModel
from typing import Any

from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.errors import (
    BrainError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    TaskNotFoundError,
)
from friday_brain.contracts.events import (
    Event,
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskExecutionStartedPayload,
    TaskFailedPayload,
    TaskPlanningStartedPayload,
    TaskPlanValidatedPayload,
)
from friday_brain.contracts.tasks import Task, TaskState
from friday_brain.protocols.event_bus import EventBus
from friday_brain.protocols.planner import Planner
from friday_brain.protocols.plan_validator import PlanValidator
from friday_brain.protocols.state_store import StateStore
from friday_brain.protocols.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Orchestrates the entire lifecycle of a task.
    """

    def __init__(
        self,
        state_store: StateStore,
        event_bus: EventBus,
        planner: Planner,
        plan_validator: PlanValidator,
        tool_executor: ToolExecutor,
    ) -> None:
        self._state_store = state_store
        self._event_bus = event_bus
        self._planner = planner
        self._plan_validator = plan_validator
        self._tool_executor = tool_executor

    async def create_task(self, request: CreateTaskRequest, correlation_id: UUID) -> tuple[Task, bool]:
        if request.idempotency_key:
            existing_task = await self._state_store.find_by_idempotency_key(request.idempotency_key)
            if existing_task:
                if self._is_equivalent_request(existing_task, request):
                    return existing_task, False
                else:
                    raise IdempotencyConflictError(request.idempotency_key)

        task = Task(
            input=request.input,
            client_request_id=request.client_request_id,
            idempotency_key=request.idempotency_key,
            metadata=request.metadata or {},
        )

        await self._state_store.save(task)
        await self._publish_event(
            "task.created",
            task,
            correlation_id,
            TaskCreatedPayload(
                input=task.input,
                state=task.state,
                client_request_id=task.client_request_id,
                idempotency_key=task.idempotency_key,
            ),
        )
        return task, True

    async def start_task_processing(self, task: Task) -> None:
        """Asynchronously starts the processing of a task."""
        asyncio.create_task(self.process_task(task.id))

    async def process_task(self, task_id: UUID) -> Task:
        try:
            task = await self._get_task_or_fail(task_id)

            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            # Planning
            task = await self._transition_and_save(task, TaskState.PLANNING)
            await self._publish_event("task.planning_started", task, task.id, TaskPlanningStartedPayload())
            
            plan = await self._planner.create_plan(task)

            task = await self._get_task_or_fail(task_id)
            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            await self._plan_validator.validate_plan(plan, task)
            await self._publish_event("task.plan_validated", task, task.id, TaskPlanValidatedPayload(plan_id=plan.id))

            # Execution
            task = await self._transition_and_save(task, TaskState.EXECUTING)
            await self._publish_event("task.execution_started", task, task.id, TaskExecutionStartedPayload())

            task = await self._get_task_or_fail(task_id)
            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            result = await self._tool_executor.execute_plan(plan, task)

            task = await self._get_task_or_fail(task_id)
            if await self._check_and_handle_cancellation(task):
                return await self._get_task_or_fail(task_id)

            task.result = result
            
            # Completion
            task = await self._transition_and_save(task, TaskState.COMPLETED)
            await self._publish_event("task.completed", task, task.id, TaskCompletedPayload(result=result))

        except BrainError as e:
            logger.error(f"Task {task_id} failed with a domain error: {e.code}", exc_info=True)
            await self._fail_task(task_id, e.code, e.message, e.details)
        except Exception:
            logger.exception(f"Task {task_id} failed with an unexpected error.")
            await self._fail_task(task_id, "internal_error", "An unexpected internal error occurred.")
        
        return await self._get_task_or_fail(task_id)

    async def request_cancellation(self, task_id: UUID) -> Task:
        task = await self._get_task_or_fail(task_id)
        print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "{task.state}", "event": "before_request_cancellation"}}')

        if task.state in {TaskState.CANCELLED, TaskState.CANCELLATION_REQUESTED}:
            print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "{task.state}", "event": "after_request_cancellation"}}')
            return task
        
        if task.state in {TaskState.COMPLETED, TaskState.FAILED}:
            # Cannot cancel a terminal task
            print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "{task.state}", "event": "after_request_cancellation"}}')
            return task

        try:
            task = await self._transition_and_save(task, TaskState.CANCELLATION_REQUESTED)
            # The orchestrator will notice this state at its next safe boundary.
            print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "{task.state}", "event": "after_request_cancellation"}}')
            return task
        except InvalidStateTransitionError:
            # Race condition, e.g., task completed just as we tried to cancel.
            task = await self._get_task_or_fail(task_id)
            print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "{task.state}", "event": "after_request_cancellation"}}')
            return task

    async def _check_and_handle_cancellation(self, task: Task) -> bool:
        if task.state == TaskState.CANCELLATION_REQUESTED:
            await self._transition_and_save(task, TaskState.CANCELLED)
            await self._publish_event("task.cancelled", task, task.id, TaskCancelledPayload())
            return True
        return False


    async def get_task(self, task_id: UUID) -> Task:
        """Retrieves a task by its ID."""
        return await self._get_task_or_fail(task_id)

    async def _fail_task(self, task_id: UUID, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        try:
            task = await self._get_task_or_fail(task_id)
            if task.state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.CANCELLATION_REQUESTED,
            }:
                return  # Already in a terminal or pending cancellation state

            task.error = {"code": code, "message": message, "details": details}
            task = await self._transition_and_save(task, TaskState.FAILED)
            await self._publish_event(
                "task.failed",
                task,
                task.id,
                TaskFailedPayload(error_code=code, error_message=message, error_details=details),
            )
        except Exception:
            logger.exception(f"Failed to transition task {task_id} to the FAILED state.")

    async def _get_task_or_fail(self, task_id: UUID) -> Task:
        task = await self._state_store.get(task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        return task

    async def _transition_and_save(self, task: Task, new_state: TaskState) -> Task:
        try:
            task.update_state(new_state)
        except InvalidStateTransitionError as e:
            raise e
        await self._state_store.save(task)
        return await self._get_task_or_fail(task.id)

    async def _publish_event(self, event_type: str, task: Task, correlation_id: UUID, payload: BaseModel) -> None:
        event = Event(
            event_type=event_type,
            task_id=task.id,
            correlation_id=correlation_id,
            payload=payload,
        )
        await self._event_bus.publish(event)

    def _is_equivalent_request(self, task: Task, request: CreateTaskRequest) -> bool:
        # For this milestone, we only check the input.
        # A more robust check might involve other fields from the request.
        return task.input == request.input

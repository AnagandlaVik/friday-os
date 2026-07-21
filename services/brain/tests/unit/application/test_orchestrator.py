import uuid

import pytest

from friday_brain.adapters.in_memory_task_repository import (
    InMemoryTaskRepository,
)
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.errors import (
    IdempotencyConflictError,
    InvalidPlanError,
)
from friday_brain.contracts.tasks import TaskState

from .fakes import (
    FakePlanner,
    FakePlanValidator,
    FakeToolExecutor,
)


@pytest.fixture
def orchestrator_components():
    repository = InMemoryTaskRepository()
    planner = FakePlanner()
    plan_validator = FakePlanValidator()
    tool_executor = FakeToolExecutor()

    orchestrator = Orchestrator(
        task_repository=repository,
        planner=planner,
        plan_validator=plan_validator,
        tool_executor=tool_executor,
    )

    return orchestrator, repository, planner, plan_validator, tool_executor


@pytest.mark.asyncio
async def test_create_task(orchestrator_components):
    orchestrator, repository, _, _, _ = orchestrator_components

    task, created = await orchestrator.create_task(
        CreateTaskRequest(input="test"),
        uuid.uuid4(),
    )

    assert created is True
    assert task.input == "test"
    assert task.state == TaskState.PENDING
    assert await repository.get(task.id) is not None
    assert [event.event_type for event in repository.events] == ["task.created"]


@pytest.mark.asyncio
async def test_process_task_success_flow(orchestrator_components):
    orchestrator, repository, _, _, _ = orchestrator_components

    task, _ = await orchestrator.create_task(
        CreateTaskRequest(input="test"),
        uuid.uuid4(),
    )

    await orchestrator.process_task(task.id)

    updated_task = await repository.get(task.id)

    assert updated_task is not None
    assert updated_task.state == TaskState.COMPLETED
    assert updated_task.result == "Echo: hello"

    assert [event.event_type for event in repository.events] == [
        "task.created",
        "task.planning_started",
        "task.plan_validated",
        "task.execution_started",
        "task.completed",
    ]


@pytest.mark.asyncio
async def test_idempotency_success(orchestrator_components):
    orchestrator, _, _, _, _ = orchestrator_components
    request = CreateTaskRequest(
        input="test",
        idempotency_key="key-1",
    )

    task1, created1 = await orchestrator.create_task(
        request,
        uuid.uuid4(),
    )
    task2, created2 = await orchestrator.create_task(
        request,
        uuid.uuid4(),
    )

    assert created1 is True
    assert created2 is False
    assert task1.id == task2.id


@pytest.mark.asyncio
async def test_idempotency_conflict(orchestrator_components):
    orchestrator, _, _, _, _ = orchestrator_components

    await orchestrator.create_task(
        CreateTaskRequest(
            input="test1",
            idempotency_key="key-1",
        ),
        uuid.uuid4(),
    )

    with pytest.raises(IdempotencyConflictError):
        await orchestrator.create_task(
            CreateTaskRequest(
                input="test2",
                idempotency_key="key-1",
            ),
            uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_task_fails_on_invalid_plan(orchestrator_components):
    orchestrator, repository, _, _, _ = orchestrator_components

    orchestrator._plan_validator = FakePlanValidator(
        validation_error=InvalidPlanError("bad plan")
    )

    task, _ = await orchestrator.create_task(
        CreateTaskRequest(input="test"),
        uuid.uuid4(),
    )

    await orchestrator.process_task(task.id)

    updated_task = await repository.get(task.id)

    assert updated_task is not None
    assert updated_task.state == TaskState.FAILED
    assert updated_task.error is not None
    assert updated_task.error["code"] == "invalid_plan"
    assert "task.failed" in [event.event_type for event in repository.events]


@pytest.mark.asyncio
async def test_request_cancellation_while_pending(
    orchestrator_components,
):
    orchestrator, repository, _, _, _ = orchestrator_components

    task, _ = await orchestrator.create_task(
        CreateTaskRequest(input="cancellable"),
        uuid.uuid4(),
    )

    updated_task = await orchestrator.request_cancellation(task.id)

    assert updated_task.state == TaskState.CANCELLATION_REQUESTED

    await orchestrator.process_task(task.id)

    final_task = await repository.get(task.id)

    assert final_task is not None
    assert final_task.state == TaskState.CANCELLED

    event_types = [event.event_type for event in repository.events]
    assert "task.cancelled" in event_types
    assert "task.planning_started" not in event_types

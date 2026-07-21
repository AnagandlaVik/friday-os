import uuid
import pytest
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.errors import IdempotencyConflictError, InvalidPlanError
from friday_brain.contracts.tasks import Task, TaskState

from .fakes import (
    FakeEventBus,
    FakePlanner,
    FakePlanValidator,
    FakeStateStore,
    FakeToolExecutor,
)


@pytest.fixture
def orchestrator_components():
    state_store = FakeStateStore()
    event_bus = FakeEventBus()
    planner = FakePlanner()
    plan_validator = FakePlanValidator()
    tool_executor = FakeToolExecutor()
    orchestrator = Orchestrator(
        state_store, event_bus, planner, plan_validator, tool_executor
    )
    return orchestrator, state_store, event_bus, planner, plan_validator, tool_executor


@pytest.mark.asyncio
async def test_create_task(orchestrator_components):
    orchestrator, store, bus, _, _, _ = orchestrator_components
    request = CreateTaskRequest(input="test")
    task, created = await orchestrator.create_task(request, uuid.uuid4())

    assert created is True

    assert task.input == "test"
    assert task.state == TaskState.PENDING
    assert await store.get(task.id) is not None
    assert len(bus.published_events) == 1
    assert bus.published_events[0].event_type == "task.created"


@pytest.mark.asyncio
async def test_process_task_success_flow(orchestrator_components):
    orchestrator, store, bus, _, _, _ = orchestrator_components
    task = Task(input="test")
    await store.save(task)

    await orchestrator.process_task(task.id)

    updated_task = await store.get(task.id)
    assert updated_task.state == TaskState.COMPLETED
    assert updated_task.result == "Echo: hello"

    event_types = [e.event_type for e in bus.published_events]
    assert "task.planning_started" in event_types
    assert "task.plan_validated" in event_types
    assert "task.execution_started" in event_types
    assert "task.completed" in event_types


@pytest.mark.asyncio
async def test_idempotency_success(orchestrator_components):
    orchestrator, _, _, _, _, _ = orchestrator_components
    request = CreateTaskRequest(input="test", idempotency_key="key-1")
    task1, created1 = await orchestrator.create_task(request, uuid.uuid4())
    assert created1 is True
    task2, created2 = await orchestrator.create_task(request, uuid.uuid4())
    assert created2 is False
    assert task1.id == task2.id


@pytest.mark.asyncio
async def test_idempotency_conflict(orchestrator_components):
    orchestrator, _, _, _, _, _ = orchestrator_components
    request1 = CreateTaskRequest(input="test1", idempotency_key="key-1")
    request2 = CreateTaskRequest(input="test2", idempotency_key="key-1")
    await orchestrator.create_task(request1, uuid.uuid4())
    with pytest.raises(IdempotencyConflictError):
        await orchestrator.create_task(request2, uuid.uuid4())


@pytest.mark.asyncio
async def test_task_fails_on_invalid_plan(orchestrator_components):
    orchestrator, store, bus, _, _, _ = orchestrator_components
    plan_validator = FakePlanValidator(validation_error=InvalidPlanError("bad plan"))
    orchestrator._plan_validator = plan_validator

    task = Task(input="test")
    await store.save(task)

    await orchestrator.process_task(task.id)

    updated_task = await store.get(task.id)
    assert updated_task.state == TaskState.FAILED
    assert updated_task.error["code"] == "invalid_plan"
    assert "task.failed" in [e.event_type for e in bus.published_events]


@pytest.mark.asyncio
async def test_request_cancellation_while_pending(orchestrator_components):
    orchestrator, store, bus, _, _, _ = orchestrator_components
    task = Task(input="cancellable")
    await store.save(task)

    # Request cancellation while the task is still pending
    updated_task = await orchestrator.request_cancellation(task.id)
    assert updated_task.state == TaskState.CANCELLATION_REQUESTED

    # Now, when the processor picks up the task, it should immediately cancel
    await orchestrator.process_task(task.id)

    final_task = await store.get(task.id)
    assert final_task.state == TaskState.CANCELLED

    event_types = [e.event_type for e in bus.published_events]
    # The orchestrator should see the cancellation before it even starts planning
    assert "task.cancelled" in event_types
    assert "task.planning_started" not in event_types

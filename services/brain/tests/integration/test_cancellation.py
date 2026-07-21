import asyncio
import uuid
from typing import AsyncGenerator

import pytest
from httpx import AsyncClient, ASGITransport

from friday_brain.main import create_app
from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from friday_brain.contracts.tasks import Task, TaskState


@pytest.fixture
def composition_root() -> CompositionRoot:
    return CompositionRoot(app_settings=settings)


@pytest.fixture
async def client(
    composition_root: CompositionRoot,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Test client fixture that creates a new application instance
    for each test function.
    """
    app = create_app(composition_root)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_cancel_non_existent_task(client: AsyncClient):
    """
    Ensures that requesting cancellation for a task that does not exist
    returns a 404 Not Found error.
    """
    task_id = uuid.uuid4()
    response = await client.delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


@pytest.mark.asyncio
async def test_cancel_completed_task(
    client: AsyncClient, composition_root: CompositionRoot
):
    """
    Ensures that requesting cancellation for a task that has already completed
    is a no-op and returns the task's final state.
    """
    create_response = await client.post("/api/v1/tasks", json={"input": "test"})
    assert create_response.status_code == 201
    task_id = create_response.json()["id"]

    for _ in range(50):
        get_response = await client.get(f"/api/v1/tasks/{task_id}")
        if get_response.json()["state"] == "completed":
            break
        await asyncio.sleep(0.01)

    delete_response = await client.delete(f"/api/v1/tasks/{task_id}")
    assert delete_response.status_code == 200

    final_task_data = delete_response.json()
    assert final_task_data["state"] == "completed"


@pytest.mark.asyncio
async def test_cancel_already_cancelled_task(
    client: AsyncClient, composition_root: CompositionRoot
):
    """
    Ensures that requesting cancellation for a task that is already CANCELLED
    returns the cancelled state.
    """
    store = composition_root.get_state_store()
    task = Task(input="test already cancelled")
    task.state = TaskState.CANCELLED
    await store.save(task)

    delete_response = await client.delete(f"/api/v1/tasks/{task.id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_repeated_delete_requests_idempotent(
    client: AsyncClient, composition_root: CompositionRoot
):
    """
    Ensures that multiple DELETE requests to cancel a task are idempotent.
    """
    store = composition_root.get_state_store()
    task = Task(input="test repeated delete")
    task.state = TaskState.PENDING
    await store.save(task)

    delete_response1 = await client.delete(f"/api/v1/tasks/{task.id}")
    assert delete_response1.status_code == 200
    state1 = delete_response1.json()["state"]

    delete_response2 = await client.delete(f"/api/v1/tasks/{task.id}")
    assert delete_response2.status_code == 200
    state2 = delete_response2.json()["state"]

    assert state1 == state2


@pytest.mark.asyncio
async def test_orchestrator_in_progress_cancellation():
    """
    Performs a direct application-level integration test of Orchestrator cancellation
    using asyncio.Event coordination to prevent deadlocks and guarantee determinism.
    """
    from friday_brain.adapters.in_memory_state_store import InMemoryStateStore
    from friday_brain.adapters.in_memory_event_bus import InMemoryEventBus
    from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
    from friday_brain.adapters.deterministic_plan_validator import (
        DeterministicPlanValidator,
    )
    from friday_brain.security.tool_policy import ToolPolicy
    from friday_brain.application.orchestrator import Orchestrator
    from friday_brain.contracts.api import CreateTaskRequest
    from .fakes import ControllableToolExecutor

    # Fresh instances
    state_store = InMemoryStateStore()
    event_bus = InMemoryEventBus()
    planner = PlaceholderPlanner()
    tool_policy = ToolPolicy(allowed_operations={"echo"})
    plan_validator = DeterministicPlanValidator(
        allowed_operations={"echo"},
        max_steps=10,
    )
    tool_executor = ControllableToolExecutor(tool_policy=tool_policy)

    orchestrator = Orchestrator(
        state_store=state_store,
        event_bus=event_bus,
        planner=planner,
        plan_validator=plan_validator,
        tool_executor=tool_executor,
    )

    # Record published events
    published_events = []
    original_publish = event_bus.publish

    async def recording_publish(event):
        published_events.append(event)
        await original_publish(event)

    event_bus.publish = recording_publish

    # 1. Start orchestration with asyncio.create_task
    request = CreateTaskRequest(input="test cancellation flow")
    task, created = await orchestrator.create_task(request, uuid.uuid4())
    task_id = task.id

    # Start task processing
    processing_task = asyncio.create_task(orchestrator.process_task(task_id))

    # 3. Wait until the executor signals that execution has started
    await tool_executor.execution_started.wait()

    # 4. Confirm the task state is executing
    executing_task = await state_store.get(task_id)
    assert executing_task is not None
    assert executing_task.state == TaskState.EXECUTING

    # 5. Request cancellation through the Orchestrator
    cancel_task = await orchestrator.request_cancellation(task_id)

    # 6. Confirm the task becomes cancellation_requested
    assert cancel_task.state == TaskState.CANCELLATION_REQUESTED

    # 7. Request cancellation again and verify it is idempotent
    cancel_task_repeat = await orchestrator.request_cancellation(task_id)
    assert cancel_task_repeat.state == TaskState.CANCELLATION_REQUESTED

    # 8. Release the executor at the safe cancellation boundary
    tool_executor.resume_execution.set()

    # 9. Await orchestration completion
    await processing_task

    # 10. Confirm the final state is cancelled
    final_task = await state_store.get(task_id)
    assert final_task is not None
    assert final_task.state == TaskState.CANCELLED

    # 11. Confirm exactly one task.cancellation_requested event
    cancellation_requested_events = [
        e for e in published_events if e.event_type == "task.cancellation_requested"
    ]
    assert len(cancellation_requested_events) == 1

    # 12. Confirm exactly one task.cancelled event
    cancelled_events = [e for e in published_events if e.event_type == "task.cancelled"]
    assert len(cancelled_events) == 1

    # 13. Confirm the task never transitions to completed
    completed_events = [e for e in published_events if e.event_type == "task.completed"]
    assert len(completed_events) == 0

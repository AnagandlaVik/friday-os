import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.in_memory_task_repository import (
    InMemoryTaskRepository,
)
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.events import Event, TaskCreatedPayload
from friday_brain.contracts.tasks import Task, TaskState
from friday_brain.main import create_app
from friday_brain.security.tool_policy import ToolPolicy

from .fakes import ControllableToolExecutor


def make_created_event(task: Task) -> Event[TaskCreatedPayload]:
    return Event(
        event_type="task.created",
        task_id=task.id,
        correlation_id=uuid.uuid4(),
        payload=TaskCreatedPayload(
            input=task.input,
            state=task.state,
            client_request_id=task.client_request_id,
            idempotency_key=task.idempotency_key,
        ),
    )


@pytest.fixture
def composition_root() -> CompositionRoot:
    return CompositionRoot(app_settings=settings)


@pytest.fixture
async def client(
    composition_root: CompositionRoot,
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app(composition_root)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_cancel_non_existent_task(
    client: AsyncClient,
) -> None:
    task_id = uuid.uuid4()

    response = await client.delete(f"/api/v1/tasks/{task_id}")

    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


@pytest.mark.asyncio
async def test_cancel_completed_task(
    client: AsyncClient,
    composition_root: CompositionRoot,
) -> None:
    create_response = await client.post(
        "/api/v1/tasks",
        json={"input": "test"},
    )
    assert create_response.status_code == 201

    task_id = create_response.json()["id"]

    for _ in range(50):
        get_response = await client.get(f"/api/v1/tasks/{task_id}")

        if get_response.json()["state"] == "completed":
            break

        await asyncio.sleep(0.01)

    delete_response = await client.delete(f"/api/v1/tasks/{task_id}")

    assert delete_response.status_code == 200
    assert delete_response.json()["state"] == "completed"


@pytest.mark.asyncio
async def test_cancel_already_cancelled_task(
    client: AsyncClient,
    composition_root: CompositionRoot,
) -> None:
    repository = composition_root.get_task_repository()

    task = Task(input="test already cancelled")
    task.state = TaskState.CANCELLED

    await repository.create_with_event(
        task,
        make_created_event(task),
    )

    delete_response = await client.delete(f"/api/v1/tasks/{task.id}")

    assert delete_response.status_code == 200
    assert delete_response.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_repeated_delete_requests_idempotent(
    client: AsyncClient,
    composition_root: CompositionRoot,
) -> None:
    repository = composition_root.get_task_repository()

    task = Task(input="test repeated delete")

    await repository.create_with_event(
        task,
        make_created_event(task),
    )

    first_response = await client.delete(f"/api/v1/tasks/{task.id}")
    assert first_response.status_code == 200

    second_response = await client.delete(f"/api/v1/tasks/{task.id}")
    assert second_response.status_code == 200

    assert first_response.json()["state"] == second_response.json()["state"]


@pytest.mark.asyncio
async def test_orchestrator_in_progress_cancellation() -> None:
    repository = InMemoryTaskRepository()
    planner = PlaceholderPlanner()
    tool_policy = ToolPolicy(allowed_operations={"echo"})
    plan_validator = DeterministicPlanValidator(
        allowed_operations={"echo"},
        max_steps=10,
    )
    tool_executor = ControllableToolExecutor(tool_policy=tool_policy)

    orchestrator = Orchestrator(
        task_repository=repository,
        planner=planner,
        plan_validator=plan_validator,
        tool_executor=tool_executor,
    )

    request = CreateTaskRequest(input="test cancellation flow")
    task, created = await orchestrator.create_task(
        request,
        uuid.uuid4(),
    )

    assert created is True

    processing_task = asyncio.create_task(orchestrator.process_task(task.id))

    await tool_executor.execution_started.wait()

    executing_task = await repository.get(task.id)

    assert executing_task is not None
    assert executing_task.state == TaskState.EXECUTING

    cancellation_requested = await orchestrator.request_cancellation(task.id)

    assert cancellation_requested.state == TaskState.CANCELLATION_REQUESTED

    repeated_request = await orchestrator.request_cancellation(task.id)

    assert repeated_request.state == TaskState.CANCELLATION_REQUESTED

    tool_executor.resume_execution.set()
    await processing_task

    final_task = await repository.get(task.id)

    assert final_task is not None
    assert final_task.state == TaskState.CANCELLED

    event_types = [event.event_type for event in repository.events]

    assert event_types.count("task.cancellation_requested") == 1
    assert event_types.count("task.cancelled") == 1
    assert "task.completed" not in event_types

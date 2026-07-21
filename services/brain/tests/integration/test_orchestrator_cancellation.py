import asyncio
import uuid

import pytest

from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.in_memory_task_repository import (
    InMemoryTaskRepository,
)
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.config import settings
from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.tasks import TaskState
from friday_brain.security.tool_policy import ToolPolicy

from .fakes import ControllableToolExecutor


@pytest.fixture
def controllable_orchestrator():
    repository = InMemoryTaskRepository()
    planner = PlaceholderPlanner()
    plan_validator = DeterministicPlanValidator(
        allowed_operations=settings.allowed_operations,
        max_steps=settings.max_plan_steps,
    )
    tool_executor = ControllableToolExecutor(
        tool_policy=ToolPolicy(allowed_operations=["echo"])
    )

    orchestrator = Orchestrator(
        task_repository=repository,
        planner=planner,
        plan_validator=plan_validator,
        tool_executor=tool_executor,
    )

    return orchestrator, repository, tool_executor


@pytest.mark.asyncio
async def test_cancellation_during_execution_app_level(
    controllable_orchestrator,
) -> None:
    orchestrator, repository, executor = controllable_orchestrator

    task, _ = await orchestrator.create_task(
        CreateTaskRequest(input="long running task"),
        uuid.uuid4(),
    )

    processing_task = asyncio.create_task(orchestrator.process_task(task.id))

    await executor.execution_started.wait()

    await orchestrator.request_cancellation(task.id)

    cancellation_requested = await repository.get(task.id)

    assert cancellation_requested is not None
    assert cancellation_requested.state == TaskState.CANCELLATION_REQUESTED

    executor.resume_execution.set()
    await processing_task

    final_task = await repository.get(task.id)

    assert final_task is not None
    assert final_task.state == TaskState.CANCELLED

    event_types = [event.event_type for event in repository.events]

    assert "task.cancelled" in event_types
    assert "task.completed" not in event_types
    assert "task.failed" not in event_types

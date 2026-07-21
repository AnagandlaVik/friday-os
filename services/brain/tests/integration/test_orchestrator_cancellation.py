
import asyncio
import uuid
import pytest

from friday_brain.application.orchestrator import Orchestrator
from friday_brain.contracts.api import CreateTaskRequest
from friday_brain.contracts.tasks import TaskState
from friday_brain.adapters.in_memory_state_store import InMemoryStateStore
from friday_brain.adapters.in_memory_event_bus import InMemoryEventBus
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
from friday_brain.adapters.deterministic_plan_validator import DeterministicPlanValidator
from friday_brain.config import settings

from friday_brain.security.tool_policy import ToolPolicy
from .fakes import ControllableToolExecutor


@pytest.fixture
def controllable_orchestrator():
    """
    Sets up an Orchestrator with a ControllableToolExecutor for fine-grained
    cancellation tests.
    """
    state_store = InMemoryStateStore()
    event_bus = InMemoryEventBus()
    planner = PlaceholderPlanner()
    plan_validator = DeterministicPlanValidator(
        allowed_operations=settings.allowed_operations,
        max_steps=settings.max_plan_steps,
    )
    tool_executor = ControllableToolExecutor(tool_policy=ToolPolicy(allowed_operations=["echo"]))
    
    orchestrator = Orchestrator(
        state_store=state_store,
        event_bus=event_bus,
        planner=planner,
        plan_validator=plan_validator,
        tool_executor=tool_executor,
    )
    
    return orchestrator, state_store, event_bus, tool_executor


@pytest.mark.asyncio
async def test_cancellation_during_execution_app_level(controllable_orchestrator):
    """
    Tests cancellation requested while a task's plan is actively being
    executed.
    """
    orchestrator, state_store, event_bus, executor = controllable_orchestrator

    # 1. Start orchestration in an asyncio task
    request = CreateTaskRequest(input="long running task")
    task, _ = await orchestrator.create_task(request, uuid.uuid4())
    
    processing_task = asyncio.create_task(orchestrator.process_task(task.id))

    # 2. Wait until the executor signals that execution has started
    await executor.execution_started.wait()

    # 3. Request cancellation through the Orchestrator application interface
    await orchestrator.request_cancellation(task.id)

    # 4. Confirm cancellation_requested
    cancelled_task = await state_store.get(task.id)
    assert cancelled_task.state == TaskState.CANCELLATION_REQUESTED

    # 5. Release the waiting executor
    executor.resume_execution.set()

    # 6. Await orchestration completion
    await processing_task

    # 7. Confirm the single terminal state is cancelled
    final_task = await state_store.get(task.id)
    assert final_task.state == TaskState.CANCELLED

    # 8. Confirm the cancellation lifecycle events were emitted
    event_types = [e.event_type for e in event_bus.published_events]
    assert "task.cancelled" in event_types
    assert "task.completed" not in event_types
    assert "task.failed" not in event_types

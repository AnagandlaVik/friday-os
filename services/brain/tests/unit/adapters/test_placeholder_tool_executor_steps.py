from uuid import uuid4

import pytest

from friday_brain.adapters.placeholder_tool_executor import (
    PlaceholderToolExecutor,
)
from friday_brain.contracts.errors import ToolNotAllowedError
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.security.tool_policy import ToolPolicy


@pytest.fixture
def executor() -> PlaceholderToolExecutor:
    return PlaceholderToolExecutor(tool_policy=ToolPolicy(allowed_operations={"echo"}))


@pytest.mark.asyncio
async def test_execute_single_step(
    executor: PlaceholderToolExecutor,
) -> None:
    task = Task(input="hello")
    step = PlanStep(
        operation="echo",
        arguments={"message": "hello"},
    )

    result = await executor.execute_step(
        step=step,
        task=task,
        checkpoint_id=uuid4(),
        idempotency_key="stable-key",
    )

    assert result == "Echo: hello"


@pytest.mark.asyncio
async def test_execute_step_rejects_disallowed_operation() -> None:
    executor = PlaceholderToolExecutor(
        tool_policy=ToolPolicy(allowed_operations={"echo"})
    )
    task = Task(input="hello")
    step = PlanStep(operation="forbidden")

    with pytest.raises(ToolNotAllowedError):
        await executor.execute_step(
            step=step,
            task=task,
            checkpoint_id=uuid4(),
            idempotency_key="stable-key",
        )


@pytest.mark.asyncio
async def test_legacy_execute_plan_still_works(
    executor: PlaceholderToolExecutor,
) -> None:
    task = Task(input="hello")

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
        steps=[second, first],
    )

    result = await executor.execute_plan(
        plan=plan,
        task=task,
    )

    assert result == "Echo: second"


@pytest.mark.asyncio
async def test_execute_plan_rejects_unknown_dependency(
    executor: PlaceholderToolExecutor,
) -> None:
    task = Task(input="hello")
    step = PlanStep(
        operation="echo",
        dependencies=[task.id],
    )
    plan = Plan(
        task_id=task.id,
        steps=[step],
    )

    with pytest.raises(
        ValueError,
        match="depends on unknown step",
    ):
        await executor.execute_plan(
            plan=plan,
            task=task,
        )

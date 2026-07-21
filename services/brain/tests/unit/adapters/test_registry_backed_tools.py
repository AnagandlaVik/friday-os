from uuid import uuid4

import pytest

from friday_brain.adapters.builtin_tools import (
    EchoInput,
    EchoOutput,
    create_builtin_tool_registry,
)
from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.placeholder_tool_executor import (
    PlaceholderToolExecutor,
)
from friday_brain.application.tool_registry import (
    ToolRegistry,
)
from friday_brain.contracts.errors import InvalidPlanError
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.security.tool_policy import ToolPolicy


def test_builtin_registry_contains_echo() -> None:
    registry = create_builtin_tool_registry()
    definition = registry.get("echo")

    assert definition.input_model is EchoInput
    assert definition.output_model is EchoOutput
    assert definition.requires_confirmation is False


@pytest.mark.asyncio
async def test_validator_accepts_registered_echo() -> None:
    registry = create_builtin_tool_registry()
    validator = DeterministicPlanValidator(
        allowed_operations={"echo"},
        max_steps=10,
        tool_registry=registry,
    )
    task = Task(input="hello")
    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation="echo",
                arguments={"message": "hello"},
            )
        ],
    )

    await validator.validate_plan(plan, task)


@pytest.mark.asyncio
async def test_validator_rejects_unregistered_operation() -> None:
    validator = DeterministicPlanValidator(
        allowed_operations={"missing"},
        max_steps=10,
        tool_registry=ToolRegistry(),
    )
    task = Task(input="hello")
    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation="missing",
            )
        ],
    )

    with pytest.raises(
        InvalidPlanError,
        match="not registered",
    ):
        await validator.validate_plan(plan, task)


@pytest.mark.asyncio
async def test_validator_rejects_invalid_arguments() -> None:
    validator = DeterministicPlanValidator(
        allowed_operations={"echo"},
        max_steps=10,
        tool_registry=create_builtin_tool_registry(),
    )
    task = Task(input="hello")
    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation="echo",
                arguments={"message": ""},
            )
        ],
    )

    with pytest.raises(
        InvalidPlanError,
        match="Arguments",
    ):
        await validator.validate_plan(plan, task)


@pytest.mark.asyncio
async def test_executor_uses_registered_schemas() -> None:
    executor = PlaceholderToolExecutor(
        tool_policy=ToolPolicy(allowed_operations={"echo"}),
        tool_registry=create_builtin_tool_registry(),
    )
    task = Task(input="hello")
    step = PlanStep(
        operation="echo",
        arguments={"message": "hello"},
    )

    result = await executor.execute_step(
        step=step,
        task=task,
        checkpoint_id=uuid4(),
        idempotency_key="registry-test",
    )

    assert result == "Echo: hello"

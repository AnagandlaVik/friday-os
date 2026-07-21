import uuid

import pytest

from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.contracts.errors import InvalidPlanError
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task


@pytest.fixture
def validator():
    return DeterministicPlanValidator(
        allowed_operations={"echo", "another_op"}, max_steps=5
    )


@pytest.fixture
def task():
    return Task(input="test")


async def test_valid_plan(validator: DeterministicPlanValidator, task: Task):
    plan = Plan(
        task_id=task.id,
        steps=[PlanStep(operation="echo", arguments={"message": "hello"})],
    )
    await validator.validate_plan(plan, task)  # Should not raise


async def test_plan_task_id_mismatch(validator: DeterministicPlanValidator, task: Task):
    plan = Plan(task_id=uuid.uuid4(), steps=[PlanStep(operation="echo")])
    with pytest.raises(InvalidPlanError) as exc_info:
        await validator.validate_plan(plan, task)
    assert exc_info.value.code == "invalid_plan"
    assert "does not match" in exc_info.value.message


async def test_plan_no_steps(validator: DeterministicPlanValidator, task: Task):
    plan = Plan(task_id=task.id, steps=[])
    with pytest.raises(InvalidPlanError, match="must contain at least one step"):
        await validator.validate_plan(plan, task)


async def test_plan_too_many_steps(validator: DeterministicPlanValidator, task: Task):
    steps = [PlanStep(operation="echo") for _ in range(6)]
    plan = Plan(task_id=task.id, steps=steps)
    with pytest.raises(InvalidPlanError, match="exceeds maximum"):
        await validator.validate_plan(plan, task)


async def test_plan_disallowed_operation(
    validator: DeterministicPlanValidator, task: Task
):
    plan = Plan(task_id=task.id, steps=[PlanStep(operation="forbidden_op")])
    with pytest.raises(InvalidPlanError, match="not in the list of allowed operations"):
        await validator.validate_plan(plan, task)


async def test_plan_duplicate_step_ids(
    validator: DeterministicPlanValidator, task: Task
):
    step_id = uuid.uuid4()
    steps = [
        PlanStep(id=step_id, operation="echo"),
        PlanStep(id=step_id, operation="echo"),
    ]
    plan = Plan(task_id=task.id, steps=steps)
    with pytest.raises(InvalidPlanError, match="Step IDs must be unique"):
        await validator.validate_plan(plan, task)


async def test_plan_unknown_dependency(
    validator: DeterministicPlanValidator, task: Task
):
    steps = [PlanStep(operation="echo", dependencies=[uuid.uuid4()])]
    plan = Plan(task_id=task.id, steps=steps)
    with pytest.raises(InvalidPlanError, match="unknown dependency"):
        await validator.validate_plan(plan, task)


async def test_plan_circular_dependency(
    validator: DeterministicPlanValidator, task: Task
):
    step1_id = uuid.uuid4()
    step2_id = uuid.uuid4()
    steps = [
        PlanStep(id=step1_id, operation="echo", dependencies=[step2_id]),
        PlanStep(id=step2_id, operation="echo", dependencies=[step1_id]),
    ]
    plan = Plan(task_id=task.id, steps=steps)
    with pytest.raises(InvalidPlanError, match="Circular dependency detected"):
        await validator.validate_plan(plan, task)

import uuid

from pydantic import ValidationError

from friday_brain.application.tool_registry import (
    ToolNotRegisteredError,
    ToolRegistry,
)
from friday_brain.contracts.errors import InvalidPlanError
from friday_brain.contracts.plans import Plan
from friday_brain.contracts.tasks import Task


class DeterministicPlanValidator:
    """Deterministically validates execution plans."""

    def __init__(
        self,
        allowed_operations: set[str],
        max_steps: int,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("Maximum plan steps must be at least one.")

        self._allowed_operations = allowed_operations
        self._max_steps = max_steps
        self._tool_registry = tool_registry

    async def validate_plan(
        self,
        plan: Plan,
        task: Task,
    ) -> None:
        if plan.task_id != task.id:
            raise InvalidPlanError(
                f"Plan task_id {plan.task_id} does not match task id {task.id}"
            )

        if not plan.steps:
            raise InvalidPlanError("Plan must contain at least one step.")

        if len(plan.steps) > self._max_steps:
            raise InvalidPlanError(
                f"Plan exceeds maximum number of steps ({self._max_steps})."
            )

        step_ids = {step.id for step in plan.steps}

        if len(step_ids) != len(plan.steps):
            raise InvalidPlanError("Step IDs must be unique.")

        for step in plan.steps:
            self._validate_operation(
                operation=step.operation,
                arguments=step.arguments,
            )

            for dependency_id in step.dependencies:
                if dependency_id not in step_ids:
                    raise InvalidPlanError(
                        f"Step {step.id} has an unknown dependency: {dependency_id}"
                    )

        self._check_for_circular_dependencies(plan)

    def _validate_operation(
        self,
        *,
        operation: str,
        arguments: dict[str, object],
    ) -> None:
        if operation not in self._allowed_operations:
            raise InvalidPlanError(
                f"Operation '{operation}' is not in the list of allowed operations."
            )

        if self._tool_registry is None:
            return

        try:
            self._tool_registry.validate_arguments(
                operation,
                arguments,
            )
        except ToolNotRegisteredError as exc:
            raise InvalidPlanError(
                f"Operation '{operation}' is not registered."
            ) from exc
        except ValidationError as exc:
            raise InvalidPlanError(
                f"Arguments for operation '{operation}' are invalid: {exc}"
            ) from exc

    def _check_for_circular_dependencies(
        self,
        plan: Plan,
    ) -> None:
        graph = {step.id: step.dependencies for step in plan.steps}
        visiting: set[uuid.UUID] = set()
        visited: set[uuid.UUID] = set()

        def visit(node: uuid.UUID) -> None:
            if node in visited:
                return

            if node in visiting:
                raise InvalidPlanError(
                    f"Circular dependency detected in plan involving step {node}"
                )

            visiting.add(node)

            for dependency_id in graph.get(node, []):
                visit(dependency_id)

            visiting.remove(node)
            visited.add(node)

        for step_id in graph:
            visit(step_id)

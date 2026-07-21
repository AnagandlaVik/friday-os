import uuid

from friday_brain.contracts.errors import InvalidPlanError
from friday_brain.contracts.plans import Plan
from friday_brain.contracts.tasks import Task


class DeterministicPlanValidator:
    """
    Validates a plan against a set of deterministic rules.
    """

    def __init__(self, allowed_operations: set[str], max_steps: int = 10):
        self._allowed_operations = allowed_operations
        self._max_steps = max_steps

    async def validate_plan(self, plan: Plan, task: Task) -> None:
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
            if step.operation not in self._allowed_operations:
                raise InvalidPlanError(
                    f"Operation '{step.operation}' is not in the list of allowed operations."
                )
            for dep_id in step.dependencies:
                if dep_id not in step_ids:
                    raise InvalidPlanError(
                        f"Step {step.id} has an unknown dependency: {dep_id}"
                    )

        self._check_for_circular_dependencies(plan)

    def _check_for_circular_dependencies(self, plan: Plan) -> None:
        graph = {step.id: step.dependencies for step in plan.steps}
        visiting = set()  # Nodes currently in the recursion stack
        visited = set()  # All nodes that have been visited

        def has_cycle(node: uuid.UUID) -> None:
            visiting.add(node)
            visited.add(node)
            for neighbor in graph.get(node, []):
                if neighbor in visiting:
                    # Found a back edge, which means there is a cycle.
                    raise InvalidPlanError(
                        f"Circular dependency detected in plan involving step {neighbor}"
                    )
                if neighbor not in visited:
                    has_cycle(neighbor)
            visiting.remove(node)

        for step_id in list(graph.keys()):
            if step_id not in visited:
                has_cycle(step_id)

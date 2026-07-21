from typing import Any
from uuid import UUID

from friday_brain.contracts.errors import ToolNotAllowedError
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.security.tool_policy import ToolPolicy


class PlaceholderToolExecutor:
    """
    Harmless deterministic tool executor.

    The legacy execute_plan method remains available while durable execution
    moves to the single-step execute_step boundary.
    """

    def __init__(self, tool_policy: ToolPolicy) -> None:
        self._tool_policy = tool_policy

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        idempotency_key: str,
    ) -> Any:
        """
        Execute one plan step.

        The placeholder operation does not create external side effects, but
        the idempotency key is accepted so real adapters can deduplicate tool
        calls later.
        """
        del task
        del idempotency_key

        if not self._tool_policy.is_allowed(step.operation):
            raise ToolNotAllowedError(step.operation)

        if step.operation == "echo":
            message = step.arguments.get("message", "")
            return f"Echo: {message}"

        raise NotImplementedError(
            f"Operation '{step.operation}' is not implemented "
            "by the placeholder executor."
        )

    async def execute_plan(
        self,
        plan: Plan,
        task: Task,
    ) -> Any:
        """
        Execute a complete plan for compatibility with the legacy orchestrator.
        """
        sorted_steps = self._topological_sort(plan.steps)

        final_result: Any = None

        for step_index, step in enumerate(sorted_steps):
            final_result = await self.execute_step(
                step=step,
                task=task,
                idempotency_key=(f"legacy:{task.id}:{plan.id}:{step_index}"),
            )

        return final_result

    def _topological_sort(
        self,
        steps: list[PlanStep],
    ) -> list[PlanStep]:
        """Return steps in dependency order."""
        step_map = {step.id: step for step in steps}
        graph = {step.id: set(step.dependencies) for step in steps}
        reverse_graph: dict[UUID, list[UUID]] = {step_id: [] for step_id in graph}
        in_degree = {step_id: 0 for step_id in graph}

        for step_id, dependencies in graph.items():
            in_degree[step_id] = len(dependencies)

            for dependency_id in dependencies:
                if dependency_id not in step_map:
                    raise ValueError(
                        f"Step {step_id} depends on unknown step {dependency_id}."
                    )

                reverse_graph[dependency_id].append(step_id)

        ready = [step_id for step_id, degree in in_degree.items() if degree == 0]
        ordered: list[PlanStep] = []

        while ready:
            step_id = ready.pop(0)
            ordered.append(step_map[step_id])

            for dependent_id in reverse_graph[step_id]:
                in_degree[dependent_id] -= 1

                if in_degree[dependent_id] == 0:
                    ready.append(dependent_id)

        if len(ordered) != len(steps):
            raise ValueError("Plan contains a circular dependency.")

        return ordered

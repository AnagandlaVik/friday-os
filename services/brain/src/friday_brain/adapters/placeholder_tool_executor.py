from typing import Any, Dict, List
from uuid import UUID

from friday_brain.contracts.errors import ToolNotAllowedError
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.security.tool_policy import ToolPolicy


class PlaceholderToolExecutor:
    """
    A placeholder tool executor that is harmless and deterministic.
    It executes a plan's steps in an order determined by a topological sort.
    """

    def __init__(self, tool_policy: ToolPolicy):
        self._tool_policy = tool_policy

    async def execute_plan(self, plan: Plan, task: Task) -> Any:
        """
        Executes a plan by processing its steps in order.
        For Milestone 1, it only handles the 'echo' operation.
        """
        sorted_steps = self._topological_sort(plan.steps)

        step_results: Dict[UUID, Any] = {}
        final_result = None

        for step in sorted_steps:
            if not self._tool_policy.is_allowed(step.operation):
                raise ToolNotAllowedError(step.operation)

            if step.operation == "echo":
                # In a real executor, we might substitute arguments from previous steps.
                # e.g. message = step.arguments.get("message", "").format(**step_results)
                message = step.arguments.get("message", "")

                result = f"Echo: {message}"
                step_results[step.id] = result
                final_result = (
                    result  # For a single-step plan, this is the final result
                )
            else:
                # In a real executor, we'd have a dispatcher to different tool functions.
                raise NotImplementedError(
                    f"Operation '{step.operation}' is not implemented by the placeholder executor."
                )

        return final_result

    def _topological_sort(self, steps: List[PlanStep]) -> List[PlanStep]:
        """
        Performs a topological sort on the plan steps to determine execution order.
        """
        step_map = {step.id: step for step in steps}
        graph = {step.id: set(step.dependencies) for step in steps}

        # This is a reverse dependency graph for easier traversal
        reverse_graph: Dict[UUID, List[UUID]] = {step_id: [] for step_id in graph}
        in_degree = {step_id: 0 for step_id in graph}

        for step_id, dependencies in graph.items():
            in_degree[step_id] = len(dependencies)
            for dep_id in dependencies:
                reverse_graph[dep_id].append(step_id)

        queue = [step_id for step_id, degree in in_degree.items() if degree == 0]
        sorted_order = []

        while queue:
            step_id = queue.pop(0)
            sorted_order.append(step_map[step_id])

            for dependent_step_id in reverse_graph[step_id]:
                in_degree[dependent_step_id] -= 1
                if in_degree[dependent_step_id] == 0:
                    queue.append(dependent_step_id)

        if len(sorted_order) != len(steps):
            # This should have been caught by the plan validator, but as a safeguard:
            raise ValueError("Plan contains a cycle, could not sort topologically.")

        return sorted_order

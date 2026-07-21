from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from friday_brain.adapters.builtin_tool_handlers import (
    create_builtin_tool_handlers,
)
from friday_brain.adapters.builtin_tools import (
    create_builtin_tool_registry,
)
from friday_brain.application.secure_tool_runtime import (
    SecureToolRuntime,
    ToolExecutionFailedError,
)
from friday_brain.application.tool_registry import ToolRegistry
from friday_brain.contracts.errors import ToolNotAllowedError
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.contracts.tools import ToolInvocation
from friday_brain.security.tool_policy import ToolPolicy


class PlaceholderToolExecutor:
    """
    Registry-backed compatibility adapter for secure tool execution.

    Durable execution calls execute_step. The complete-plan method remains
    available for the legacy in-memory orchestrator.
    """

    def __init__(
        self,
        tool_policy: ToolPolicy,
        tool_registry: ToolRegistry | None = None,
        secure_runtime: SecureToolRuntime | None = None,
    ) -> None:
        self._tool_policy = tool_policy
        self._tool_registry = (
            tool_registry
            if tool_registry is not None
            else create_builtin_tool_registry()
        )
        self._secure_runtime = (
            secure_runtime
            if secure_runtime is not None
            else SecureToolRuntime(
                registry=self._tool_registry,
                handlers=create_builtin_tool_handlers(),
            )
        )

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        checkpoint_id: UUID,
        idempotency_key: str,
        lease_token: UUID | None = None,
    ) -> Any:
        if not self._tool_policy.is_allowed(step.operation):
            raise ToolNotAllowedError(step.operation)

        definition = self._tool_registry.get(step.operation)

        result = await self._secure_runtime.execute(
            ToolInvocation(
                tool_name=step.operation,
                arguments=step.arguments,
                idempotency_key=idempotency_key,
                task_id=task.id,
                checkpoint_id=checkpoint_id,
            ),
            lease_token=lease_token,
        )

        if not result.success:
            if result.error is None:
                raise RuntimeError(
                    "Secure runtime returned a failed result without an error."
                )

            raise ToolExecutionFailedError(
                error=result.error,
                max_attempts=(definition.retry_policy.max_attempts),
                base_delay_sec=(definition.retry_policy.base_delay_sec),
                max_delay_sec=(definition.retry_policy.max_delay_sec),
            )

        # Preserve the original echo result shape while the legacy API
        # still expects a string rather than a structured result object.
        if step.operation == "echo" and isinstance(result.output, dict):
            return result.output.get("result")

        return result.output

    async def execute_plan(
        self,
        plan: Plan,
        task: Task,
    ) -> Any:
        sorted_steps = self._topological_sort(plan.steps)
        final_result: Any = None

        for step_index, step in enumerate(sorted_steps):
            idempotency_key = f"legacy:{task.id}:{plan.id}:{step_index}"

            final_result = await self.execute_step(
                step=step,
                task=task,
                checkpoint_id=uuid5(
                    NAMESPACE_URL,
                    idempotency_key,
                ),
                idempotency_key=idempotency_key,
            )

        return final_result

    def _topological_sort(
        self,
        steps: list[PlanStep],
    ) -> list[PlanStep]:
        step_map = {step.id: step for step in steps}
        graph = {step.id: set(step.dependencies) for step in steps}
        reverse_graph: dict[
            UUID,
            list[UUID],
        ] = {step_id: [] for step_id in graph}
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

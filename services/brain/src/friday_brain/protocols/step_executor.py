from typing import Any, Protocol

from friday_brain.contracts.plans import PlanStep
from friday_brain.contracts.tasks import Task


class StepExecutor(Protocol):
    """Executes one durable plan step at a time."""

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        idempotency_key: str,
    ) -> Any:
        """Execute one step using a stable idempotency key."""
        ...

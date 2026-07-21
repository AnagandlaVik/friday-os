from typing import Any, Protocol
from uuid import UUID

from friday_brain.contracts.plans import PlanStep
from friday_brain.contracts.tasks import Task


class StepExecutor(Protocol):
    """Executes one durable plan step at a time."""

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        checkpoint_id: UUID,
        idempotency_key: str,
        lease_token: UUID | None = None,
    ) -> Any:
        """Execute one durable step inside its tool boundary."""
        ...

from typing import Protocol

from friday_brain.contracts.plans import Plan
from friday_brain.contracts.tasks import Task


class PlanValidator(Protocol):
    """
    Protocol for validating a plan.
    """

    async def validate_plan(self, plan: Plan, task: Task) -> None:
        """
        Validates the plan. Raises InvalidPlanError if the plan is invalid.
        """
        ...

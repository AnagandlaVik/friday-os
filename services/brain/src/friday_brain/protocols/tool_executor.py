from typing import Any, Protocol

from friday_brain.contracts.plans import Plan
from friday_brain.contracts.tasks import Task


class ToolExecutor(Protocol):
    """
    Protocol for executing a plan.
    """

    async def execute_plan(self, plan: Plan, task: Task) -> Any:
        """
        Executes a plan and returns the result.
        """
        ...

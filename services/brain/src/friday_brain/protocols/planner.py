from typing import Protocol

from friday_brain.contracts.plans import Plan
from friday_brain.contracts.tasks import Task


class Planner(Protocol):
    """
    Protocol for a planner that creates an execution plan for a task.
    """

    async def create_plan(self, task: Task) -> Plan:
        """
        Creates a plan for the given task.
        """
        ...

from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task


class PlaceholderPlanner:
    """
    A placeholder planner that returns a deterministic, single-step plan.
    """

    async def create_plan(self, task: Task) -> Plan:
        """
        Creates a simple, single-step plan to echo the task's input.
        """
        step = PlanStep(
            operation="echo",
            arguments={"message": task.input},
        )
        plan = Plan(
            task_id=task.id,
            steps=[step],
        )
        return plan

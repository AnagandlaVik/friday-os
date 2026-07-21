import asyncio
from typing import Any

from friday_brain.adapters.placeholder_tool_executor import PlaceholderToolExecutor
from friday_brain.contracts.plans import Plan
from friday_brain.contracts.tasks import Task


class ControllableToolExecutor(PlaceholderToolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_started = asyncio.Event()
        self.resume_execution = asyncio.Event()

    async def execute_plan(self, plan: Plan, task: Task) -> Any:
        """
        An executor that signals when it has started and waits for a signal
        to continue.
        """
        self.execution_started.set()
        await self.resume_execution.wait()
        return await super().execute_plan(plan, task)

from typing import Any, List
import uuid

from friday_brain.contracts.events import Event
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task


class FakeStateStore:
    def __init__(self):
        self.tasks = {}
        self.idempotency_keys = {}

    async def save(self, task: Task):
        self.tasks[task.id] = task.model_copy(deep=True)
        if task.idempotency_key:
            self.idempotency_keys[task.idempotency_key] = task.id

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id, None)

    async def find_by_idempotency_key(self, idempotency_key: str) -> Task | None:
        task_id = self.idempotency_keys.get(idempotency_key)
        return await self.get(task_id) if task_id else None


class FakeEventBus:
    def __init__(self):
        self.published_events: List[Event] = []

    async def publish(self, event: Event):
        self.published_events.append(event)


class FakePlanner:
    def __init__(self, plan_to_return: Plan = None):
        self._plan = plan_to_return

    async def create_plan(self, task: Task) -> Plan:
        if self._plan:
            return self._plan
        return Plan(
            task_id=task.id,
            steps=[PlanStep(operation="echo", arguments={"message": "hello"})],
        )


class FakePlanValidator:
    def __init__(self, validation_error: Exception = None):
        self._validation_error = validation_error

    async def validate_plan(self, plan: Plan, task: Task):
        if self._validation_error:
            raise self._validation_error


class FakeToolExecutor:
    def __init__(self, result: Any = "Echo: hello"):
        self._result = result

    async def execute_plan(self, plan: Plan, task: Task) -> Any:
        return self._result

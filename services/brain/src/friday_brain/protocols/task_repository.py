from typing import Any, Protocol
from uuid import UUID

from friday_brain.contracts.events import Event
from friday_brain.contracts.tasks import Task


class TaskRepository(Protocol):
    """Authoritative durable persistence boundary for tasks and events."""

    async def start(self) -> None:
        """Initialize database resources."""
        ...

    async def stop(self) -> None:
        """Release database resources."""
        ...

    async def is_healthy(self) -> bool:
        """Return whether PostgreSQL is reachable."""
        ...

    async def get(self, task_id: UUID) -> Task | None:
        """Retrieve a task by ID."""
        ...

    async def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Task | None:
        """Retrieve a task by its idempotency key."""
        ...

    async def create_with_event(
        self,
        task: Task,
        event: Event[Any],
    ) -> Task:
        """Atomically create a task, history event, and outbox event."""
        ...

    async def update_with_event(
        self,
        task: Task,
        expected_version: int,
        event: Event[Any],
    ) -> Task:
        """Atomically update a task and append its event and outbox row."""
        ...

    async def append_event(
        self,
        task_id: UUID,
        expected_version: int,
        event: Event[Any],
    ) -> None:
        """Atomically append an event without mutating task state."""
        ...

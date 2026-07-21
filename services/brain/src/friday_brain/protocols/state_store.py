import uuid
from typing import Protocol

from friday_brain.contracts.tasks import Task


class StateStore(Protocol):
    """
    Protocol for a durable or in-memory store for task state.
    """

    async def start(self) -> None:
        """
        Initialize connection pools or local state caches.
        """
        ...

    async def stop(self) -> None:
        """
        Gracefully release connection pools and file handles.
        """
        ...

    async def save(self, task: Task) -> None:
        """
        Saves the complete task state.
        """
        ...

    async def get(self, task_id: uuid.UUID) -> Task | None:
        """
        Retrieves a task by its ID.
        """
        ...

    async def find_by_idempotency_key(self, idempotency_key: str) -> Task | None:
        """
        Finds a task by its idempotency key.
        """
        ...

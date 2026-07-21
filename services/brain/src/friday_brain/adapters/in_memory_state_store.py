import asyncio
import uuid
from typing import Dict

from friday_brain.contracts.tasks import Task


class InMemoryStateStore:
    """
    In-memory implementation of the StateStore protocol.
    This is not suitable for production use as all state is lost on restart.
    """

    def __init__(self) -> None:
        self._tasks: Dict[uuid.UUID, Task] = {}
        self._idempotency_keys: Dict[str, uuid.UUID] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """No-op for in-memory state store."""
        pass

    async def stop(self) -> None:
        """No-op for in-memory state store."""
        pass

    async def is_healthy(self) -> bool:
        """In-memory state store is always healthy."""
        return True

    def reset(self) -> None:
        """Clears the store for testing purposes."""
        self._tasks.clear()
        self._idempotency_keys.clear()

    async def save(self, task: Task) -> None:
        print(
            f'{{"timestamp": {__import__("time").time()}, "task_id": "{task.id}", "state": "{task.state}", "event": "before_state_store_save"}}'
        )
        async with self._lock:
            existing = self._tasks.get(task.id)
            if existing and existing.version != task.version:
                from friday_brain.contracts.errors import InvalidStateTransitionError

                raise InvalidStateTransitionError(
                    from_state=f"version {existing.version}",
                    to_state=f"version {task.version}",
                )
            task.version += 1
            # Store a copy to prevent mutation outside the store
            task_copy = task.model_copy(deep=True)
            self._tasks[task_copy.id] = task_copy
            if task_copy.idempotency_key:
                self._idempotency_keys[task_copy.idempotency_key] = task_copy.id
        print(
            f'{{"timestamp": {__import__("time").time()}, "task_id": "{task.id}", "state": "{task.state}", "event": "after_state_store_save"}}'
        )

    async def get(self, task_id: uuid.UUID) -> Task | None:
        async with self._lock:
            return self._get_unlocked(task_id)

    def _get_unlocked(self, task_id: uuid.UUID) -> Task | None:
        task = self._tasks.get(task_id)
        # Return a copy to prevent mutation
        return task.model_copy(deep=True) if task else None

    async def find_by_idempotency_key(self, idempotency_key: str) -> Task | None:
        async with self._lock:
            task_id = self._idempotency_keys.get(idempotency_key)
            if task_id:
                # Call the unlocked version since we already hold the lock
                return self._get_unlocked(task_id)
            return None

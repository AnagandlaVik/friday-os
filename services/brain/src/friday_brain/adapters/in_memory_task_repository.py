import asyncio
from typing import Any
from uuid import UUID

from friday_brain.contracts.errors import (
    IdempotencyConflictError,
    TaskConcurrencyConflictError,
    TaskNotFoundError,
)
from friday_brain.contracts.events import Event
from friday_brain.contracts.tasks import Task


class InMemoryTaskRepository:
    """In-memory task repository for tests and local development."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._idempotency_index: dict[str, UUID] = {}
        self._events: list[Event[Any]] = []
        self._outbox_events: list[Event[Any]] = []
        self._started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def is_healthy(self) -> bool:
        return self._started

    async def get(self, task_id: UUID) -> Task | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    async def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Task | None:
        task_id = self._idempotency_index.get(idempotency_key)
        if task_id is None:
            return None
        return await self.get(task_id)

    async def create_with_event(
        self,
        task: Task,
        event: Event[Any],
    ) -> Task:
        self._validate_event_task_id(task.id, event)

        async with self._lock:
            if task.idempotency_key is not None:
                existing_id = self._idempotency_index.get(task.idempotency_key)
                if existing_id is not None:
                    existing = self._tasks[existing_id]
                    if existing.input != task.input:
                        raise IdempotencyConflictError(task.idempotency_key)
                    return existing.model_copy(deep=True)

            if task.id in self._tasks:
                raise ValueError(f"Task '{task.id}' already exists.")

            persisted = task.model_copy(deep=True)
            persisted.version = 1

            self._tasks[persisted.id] = persisted

            if persisted.idempotency_key is not None:
                self._idempotency_index[persisted.idempotency_key] = persisted.id

            self._events.append(event.model_copy(deep=True))
            self._outbox_events.append(event.model_copy(deep=True))

            return persisted.model_copy(deep=True)

    async def update_with_event(
        self,
        task: Task,
        expected_version: int,
        event: Event[Any],
    ) -> Task:
        self._validate_event_task_id(task.id, event)

        async with self._lock:
            existing = self._tasks.get(task.id)
            if existing is None:
                raise TaskNotFoundError(task.id)

            if existing.version != expected_version:
                raise TaskConcurrencyConflictError(
                    task.id,
                    expected_version,
                )

            persisted = task.model_copy(deep=True)
            persisted.version = expected_version + 1

            self._tasks[persisted.id] = persisted
            self._events.append(event.model_copy(deep=True))
            self._outbox_events.append(event.model_copy(deep=True))

            return persisted.model_copy(deep=True)

    async def append_event(
        self,
        task_id: UUID,
        expected_version: int,
        event: Event[Any],
    ) -> None:
        self._validate_event_task_id(task_id, event)

        async with self._lock:
            existing = self._tasks.get(task_id)
            if existing is None:
                raise TaskNotFoundError(task_id)

            if existing.version != expected_version:
                raise TaskConcurrencyConflictError(
                    task_id,
                    expected_version,
                )

            self._events.append(event.model_copy(deep=True))
            self._outbox_events.append(event.model_copy(deep=True))

    @property
    def events(self) -> list[Event[Any]]:
        return [event.model_copy(deep=True) for event in self._events]

    @property
    def outbox_events(self) -> list[Event[Any]]:
        return [event.model_copy(deep=True) for event in self._outbox_events]

    @staticmethod
    def _validate_event_task_id(
        task_id: UUID,
        event: Event[Any],
    ) -> None:
        if event.task_id != task_id:
            raise ValueError("Event task_id must match the persisted task ID.")

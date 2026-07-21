from uuid import uuid4

import pytest

from friday_brain.adapters.in_memory_task_repository import (
    InMemoryTaskRepository,
)
from friday_brain.contracts.errors import (
    IdempotencyConflictError,
    TaskConcurrencyConflictError,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
    TaskPlanningStartedPayload,
)
from friday_brain.contracts.tasks import Task, TaskState


def make_created_event(
    task: Task,
) -> Event[TaskCreatedPayload]:
    return Event(
        event_type="task.created",
        task_id=task.id,
        correlation_id=uuid4(),
        payload=TaskCreatedPayload(
            input=task.input,
            state=task.state,
            client_request_id=task.client_request_id,
            idempotency_key=task.idempotency_key,
        ),
    )


@pytest.mark.asyncio
async def test_lifecycle_health() -> None:
    repository = InMemoryTaskRepository()

    assert await repository.is_healthy() is False

    await repository.start()
    assert await repository.is_healthy() is True

    await repository.stop()
    assert await repository.is_healthy() is False


@pytest.mark.asyncio
async def test_create_records_task_event_and_outbox() -> None:
    repository = InMemoryTaskRepository()
    task = Task(input="Create task")
    event = make_created_event(task)

    created = await repository.create_with_event(task, event)

    assert created.id == task.id
    assert created.version == 1
    assert await repository.get(task.id) == created
    assert repository.events == [event]
    assert repository.outbox_events == [event]


@pytest.mark.asyncio
async def test_equivalent_idempotent_creation_returns_existing() -> None:
    repository = InMemoryTaskRepository()

    first = Task(
        input="Same request",
        idempotency_key="same-key",
    )
    second = Task(
        input="Same request",
        idempotency_key="same-key",
    )

    created = await repository.create_with_event(
        first,
        make_created_event(first),
    )
    duplicate = await repository.create_with_event(
        second,
        make_created_event(second),
    )

    assert duplicate == created
    assert len(repository.events) == 1
    assert len(repository.outbox_events) == 1


@pytest.mark.asyncio
async def test_conflicting_idempotent_creation_is_rejected() -> None:
    repository = InMemoryTaskRepository()

    first = Task(
        input="Original",
        idempotency_key="conflict-key",
    )
    conflicting = Task(
        input="Different",
        idempotency_key="conflict-key",
    )

    await repository.create_with_event(
        first,
        make_created_event(first),
    )

    with pytest.raises(IdempotencyConflictError):
        await repository.create_with_event(
            conflicting,
            make_created_event(conflicting),
        )


@pytest.mark.asyncio
async def test_update_increments_version_and_records_event() -> None:
    repository = InMemoryTaskRepository()

    task = Task(input="Versioned task")
    created = await repository.create_with_event(
        task,
        make_created_event(task),
    )

    planning = created.model_copy(deep=True)
    planning.update_state(TaskState.PLANNING)

    event = Event(
        event_type="task.planning_started",
        task_id=created.id,
        correlation_id=uuid4(),
        payload=TaskPlanningStartedPayload(),
    )

    updated = await repository.update_with_event(
        planning,
        expected_version=created.version,
        event=event,
    )

    assert updated.version == 2
    assert updated.state == TaskState.PLANNING
    assert len(repository.events) == 2
    assert len(repository.outbox_events) == 2


@pytest.mark.asyncio
async def test_stale_update_is_rejected_without_new_event() -> None:
    repository = InMemoryTaskRepository()

    task = Task(input="Concurrency task")
    created = await repository.create_with_event(
        task,
        make_created_event(task),
    )

    planning = created.model_copy(deep=True)
    planning.update_state(TaskState.PLANNING)

    event = Event(
        event_type="task.planning_started",
        task_id=created.id,
        correlation_id=uuid4(),
        payload=TaskPlanningStartedPayload(),
    )

    await repository.update_with_event(
        planning,
        expected_version=1,
        event=event,
    )

    with pytest.raises(TaskConcurrencyConflictError):
        await repository.update_with_event(
            planning,
            expected_version=1,
            event=Event(
                event_type="task.planning_started",
                task_id=created.id,
                correlation_id=uuid4(),
                payload=TaskPlanningStartedPayload(),
            ),
        )

    assert len(repository.events) == 2
    assert len(repository.outbox_events) == 2


@pytest.mark.asyncio
async def test_append_event_does_not_increment_version() -> None:
    repository = InMemoryTaskRepository()

    task = Task(input="Event-only task")
    created = await repository.create_with_event(
        task,
        make_created_event(task),
    )

    event = Event(
        event_type="task.plan_validated",
        task_id=created.id,
        correlation_id=uuid4(),
        payload=TaskPlanningStartedPayload(),
    )

    await repository.append_event(
        created.id,
        expected_version=created.version,
        event=event,
    )

    reloaded = await repository.get(created.id)

    assert reloaded is not None
    assert reloaded.version == 1
    assert len(repository.events) == 2
    assert len(repository.outbox_events) == 2

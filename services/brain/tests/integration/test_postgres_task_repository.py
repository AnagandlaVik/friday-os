import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.contracts.errors import IdempotencyConflictError
from friday_brain.contracts.events import Event, TaskCreatedPayload
from friday_brain.contracts.tasks import Task


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://friday:friday@localhost:5432/friday",
)


def make_created_event(task: Task) -> Event[TaskCreatedPayload]:
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


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL integration service is unavailable.")

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE
                    outbox_events,
                    task_events,
                    tasks
                RESTART IDENTITY CASCADE
                """
            )
        )

    yield engine

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE
                    outbox_events,
                    task_events,
                    tasks
                RESTART IDENTITY CASCADE
                """
            )
        )

    await engine.dispose()


@pytest.fixture
async def repository(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[PostgresTaskRepository]:
    repository = PostgresTaskRepository(POSTGRES_URL)
    await repository.start()
    yield repository
    await repository.stop()


@pytest.mark.asyncio
async def test_repository_health(
    repository: PostgresTaskRepository,
) -> None:
    assert await repository.is_healthy() is True


@pytest.mark.asyncio
async def test_create_with_event_is_atomic(
    repository: PostgresTaskRepository,
    postgres_engine: AsyncEngine,
) -> None:
    task = Task(
        input="Test PostgreSQL task",
        idempotency_key="create-atomic-1",
    )
    event = make_created_event(task)

    created = await repository.create_with_event(task, event)

    assert created.id == task.id
    assert created.version == 1
    assert await repository.get(task.id) == created
    assert await repository.find_by_idempotency_key("create-atomic-1") == created

    async with postgres_engine.connect() as connection:
        task_count = await connection.scalar(text("SELECT COUNT(*) FROM tasks"))
        event_count = await connection.scalar(text("SELECT COUNT(*) FROM task_events"))
        outbox_count = await connection.scalar(
            text("SELECT COUNT(*) FROM outbox_events")
        )
        outbox = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT
                        event_id,
                        task_id,
                        subject,
                        status,
                        attempt_count
                    FROM outbox_events
                    WHERE event_id = :event_id
                    """
                    ),
                    {"event_id": event.event_id},
                )
            )
            .mappings()
            .one()
        )

    assert task_count == 1
    assert event_count == 1
    assert outbox_count == 1
    assert outbox["event_id"] == event.event_id
    assert outbox["task_id"] == task.id
    assert outbox["subject"] == "friday.events.brain.task.created"
    assert outbox["status"] == "pending"
    assert outbox["attempt_count"] == 0


@pytest.mark.asyncio
async def test_equivalent_idempotent_creation_returns_existing_task(
    repository: PostgresTaskRepository,
    postgres_engine: AsyncEngine,
) -> None:
    first = Task(
        input="Same request",
        idempotency_key="same-request",
    )
    second = Task(
        input="Same request",
        idempotency_key="same-request",
    )

    first_created = await repository.create_with_event(
        first,
        make_created_event(first),
    )
    second_created = await repository.create_with_event(
        second,
        make_created_event(second),
    )

    assert second_created == first_created

    async with postgres_engine.connect() as connection:
        task_count = await connection.scalar(text("SELECT COUNT(*) FROM tasks"))
        event_count = await connection.scalar(text("SELECT COUNT(*) FROM task_events"))
        outbox_count = await connection.scalar(
            text("SELECT COUNT(*) FROM outbox_events")
        )

    assert task_count == 1
    assert event_count == 1
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_conflicting_idempotent_creation_is_rejected(
    repository: PostgresTaskRepository,
) -> None:
    first = Task(
        input="Original request",
        idempotency_key="conflicting-request",
    )
    conflicting = Task(
        input="Different request",
        idempotency_key="conflicting-request",
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
async def test_event_failure_rolls_back_task_creation(
    repository: PostgresTaskRepository,
    postgres_engine: AsyncEngine,
) -> None:
    first = Task(input="First task")
    duplicate_event = make_created_event(first)

    await repository.create_with_event(first, duplicate_event)

    second = Task(input="Second task")
    invalid_duplicate_event = Event(
        event_id=duplicate_event.event_id,
        event_type="task.created",
        task_id=second.id,
        correlation_id=uuid4(),
        payload=TaskCreatedPayload(
            input=second.input,
            state=second.state,
        ),
    )

    with pytest.raises(Exception):
        await repository.create_with_event(
            second,
            invalid_duplicate_event,
        )

    assert await repository.get(second.id) is None

    async with postgres_engine.connect() as connection:
        task_count = await connection.scalar(text("SELECT COUNT(*) FROM tasks"))
        event_count = await connection.scalar(text("SELECT COUNT(*) FROM task_events"))
        outbox_count = await connection.scalar(
            text("SELECT COUNT(*) FROM outbox_events")
        )

    assert task_count == 1
    assert event_count == 1
    assert outbox_count == 1

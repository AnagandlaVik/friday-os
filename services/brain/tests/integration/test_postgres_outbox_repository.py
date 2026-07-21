import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from friday_brain.adapters.postgres_outbox_repository import (
    PostgresOutboxRepository,
)
from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.contracts.tasks import Task


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://friday:friday@localhost:5432/friday",
)


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


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        POSTGRES_URL,
        pool_pre_ping=True,
    )

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


@pytest.mark.asyncio
async def test_competing_workers_claim_message_once(
    postgres_engine: AsyncEngine,
) -> None:
    task_repository = PostgresTaskRepository(POSTGRES_URL)
    first_repository = PostgresOutboxRepository(POSTGRES_URL)
    second_repository = PostgresOutboxRepository(POSTGRES_URL)

    await task_repository.start()
    await first_repository.start()
    await second_repository.start()

    try:
        task = Task(input="Competing workers")
        event = make_created_event(task)

        await task_repository.create_with_event(task, event)

        first_claim, second_claim = await asyncio.gather(
            first_repository.claim_batch(
                worker_id="worker-one",
                batch_size=1,
                lock_timeout_sec=30.0,
            ),
            second_repository.claim_batch(
                worker_id="worker-two",
                batch_size=1,
                lock_timeout_sec=30.0,
            ),
        )

        claimed = first_claim + second_claim

        assert len(claimed) == 1
        assert claimed[0].event_id == event.event_id
        assert claimed[0].attempt_count == 1

        claiming_worker = "worker-one" if first_claim else "worker-two"
        claiming_repository = first_repository if first_claim else second_repository

        await claiming_repository.mark_published(
            event_id=event.event_id,
            worker_id=claiming_worker,
        )

        async with postgres_engine.connect() as connection:
            status = await connection.scalar(
                text(
                    """
                    SELECT status
                    FROM outbox_events
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event.event_id},
            )

        assert status == "published"
    finally:
        await second_repository.stop()
        await first_repository.stop()
        await task_repository.stop()


@pytest.mark.asyncio
async def test_failed_message_retries_then_dead_letters(
    postgres_engine: AsyncEngine,
) -> None:
    task_repository = PostgresTaskRepository(POSTGRES_URL)
    outbox_repository = PostgresOutboxRepository(POSTGRES_URL)

    await task_repository.start()
    await outbox_repository.start()

    try:
        task = Task(input="Retry task")
        event = make_created_event(task)

        await task_repository.create_with_event(task, event)

        first_claim = await outbox_repository.claim_batch(
            worker_id="retry-worker",
            batch_size=1,
            lock_timeout_sec=30.0,
        )

        assert first_claim[0].attempt_count == 1

        await outbox_repository.mark_failed(
            event_id=event.event_id,
            worker_id="retry-worker",
            error="first failure",
            retry_delay_sec=0.0,
            max_attempts=2,
        )

        second_claim = await outbox_repository.claim_batch(
            worker_id="retry-worker",
            batch_size=1,
            lock_timeout_sec=30.0,
        )

        assert second_claim[0].attempt_count == 2

        await outbox_repository.mark_failed(
            event_id=event.event_id,
            worker_id="retry-worker",
            error="second failure",
            retry_delay_sec=0.0,
            max_attempts=2,
        )

        async with postgres_engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT
                            status,
                            attempt_count,
                            last_error
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

        assert row["status"] == "dead_letter"
        assert row["attempt_count"] == 2
        assert row["last_error"] == "second failure"
    finally:
        await outbox_repository.stop()
        await task_repository.stop()

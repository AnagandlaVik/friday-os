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

from friday_brain.adapters.postgres_execution_lease_repository import (
    PostgresExecutionLeaseRepository,
)
from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.contracts.tasks import Task, TaskState


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
                    task_step_checkpoints,
                    task_plans,
                    task_execution_leases,
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
                    task_step_checkpoints,
                    task_plans,
                    task_execution_leases,
                    outbox_events,
                    task_events,
                    tasks
                RESTART IDENTITY CASCADE
                """
            )
        )

    await engine.dispose()


@pytest.fixture
async def task_repository(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[PostgresTaskRepository]:
    repository = PostgresTaskRepository(POSTGRES_URL)
    await repository.start()
    yield repository
    await repository.stop()


@pytest.fixture
async def lease_repository(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[PostgresExecutionLeaseRepository]:
    repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    await repository.start()
    yield repository
    await repository.stop()


async def create_task(
    repository: PostgresTaskRepository,
    *,
    state: TaskState = TaskState.PENDING,
    input_text: str = "Lease test task",
) -> Task:
    task = Task(
        input=input_text,
        state=state,
    )

    return await repository.create_with_event(
        task,
        make_created_event(task),
    )


@pytest.mark.asyncio
async def test_acquire_and_renew_lease(
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
) -> None:
    task = await create_task(task_repository)

    acquired = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-one",
        lease_duration_sec=30.0,
    )

    assert acquired is not None
    assert acquired.task_id == task.id
    assert acquired.worker_id == "worker-one"
    assert acquired.execution_attempt == 1
    assert acquired.expires_at > acquired.acquired_at

    blocked = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-two",
        lease_duration_sec=30.0,
    )

    assert blocked is None

    renewed = await lease_repository.renew(
        task_id=task.id,
        lease_token=acquired.lease_token,
        worker_id="worker-one",
        lease_duration_sec=60.0,
    )

    assert renewed is not None
    assert renewed.lease_token == acquired.lease_token
    assert renewed.execution_attempt == 1
    assert renewed.expires_at > acquired.expires_at


@pytest.mark.asyncio
async def test_competing_workers_acquire_only_one_lease(
    task_repository: PostgresTaskRepository,
) -> None:
    task = await create_task(task_repository)

    first = PostgresExecutionLeaseRepository(POSTGRES_URL)
    second = PostgresExecutionLeaseRepository(POSTGRES_URL)

    await first.start()
    await second.start()

    try:
        first_result, second_result = await asyncio.gather(
            first.acquire(
                task_id=task.id,
                worker_id="worker-one",
                lease_duration_sec=30.0,
            ),
            second.acquire(
                task_id=task.id,
                worker_id="worker-two",
                lease_duration_sec=30.0,
            ),
        )

        acquired = [
            lease for lease in (first_result, second_result) if lease is not None
        ]

        assert len(acquired) == 1
        assert acquired[0].task_id == task.id
    finally:
        await second.stop()
        await first.stop()


@pytest.mark.asyncio
async def test_expired_lease_can_be_taken_over(
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
) -> None:
    task = await create_task(task_repository)

    first = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-one",
        lease_duration_sec=0.1,
    )

    assert first is not None

    await asyncio.sleep(0.15)

    second = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-two",
        lease_duration_sec=30.0,
    )

    assert second is not None
    assert second.worker_id == "worker-two"
    assert second.lease_token != first.lease_token
    assert second.execution_attempt == 2

    stale_renewal = await lease_repository.renew(
        task_id=task.id,
        lease_token=first.lease_token,
        worker_id="worker-one",
        lease_duration_sec=30.0,
    )

    stale_release = await lease_repository.release(
        task_id=task.id,
        lease_token=first.lease_token,
        worker_id="worker-one",
    )

    assert stale_renewal is None
    assert stale_release is False

    assert (
        await lease_repository.release(
            task_id=task.id,
            lease_token=second.lease_token,
            worker_id="worker-two",
        )
        is True
    )


@pytest.mark.asyncio
async def test_release_requires_exact_token_and_worker(
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
) -> None:
    task = await create_task(task_repository)

    lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id="lease-owner",
        lease_duration_sec=30.0,
    )

    assert lease is not None

    assert (
        await lease_repository.release(
            task_id=task.id,
            lease_token=uuid4(),
            worker_id="lease-owner",
        )
        is False
    )

    assert (
        await lease_repository.release(
            task_id=task.id,
            lease_token=lease.lease_token,
            worker_id="wrong-worker",
        )
        is False
    )

    assert (
        await lease_repository.release(
            task_id=task.id,
            lease_token=lease.lease_token,
            worker_id="lease-owner",
        )
        is True
    )


@pytest.mark.asyncio
async def test_discover_recoverable_tasks(
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
) -> None:
    active = await create_task(
        task_repository,
        input_text="Actively leased",
    )
    recoverable = await create_task(
        task_repository,
        state=TaskState.PLANNING,
        input_text="Recoverable planning task",
    )
    terminal = await create_task(
        task_repository,
        state=TaskState.COMPLETED,
        input_text="Terminal task",
    )

    lease = await lease_repository.acquire(
        task_id=active.id,
        worker_id="active-worker",
        lease_duration_sec=30.0,
    )

    assert lease is not None

    discovered = await lease_repository.discover_recoverable(limit=10)

    assert recoverable.id in discovered
    assert active.id not in discovered
    assert terminal.id not in discovered


@pytest.mark.asyncio
async def test_nonpositive_lease_duration_is_rejected(
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
) -> None:
    task = await create_task(task_repository)

    with pytest.raises(ValueError):
        await lease_repository.acquire(
            task_id=task.id,
            worker_id="worker",
            lease_duration_sec=0.0,
        )

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
from friday_brain.adapters.postgres_execution_plan_repository import (
    PostgresExecutionPlanRepository,
)
from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.contracts.plans import Plan, PlanStep
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
        pytest.skip("PostgreSQL is unavailable.")

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
async def repositories(
    postgres_engine: AsyncEngine,
):
    task_repository = PostgresTaskRepository(POSTGRES_URL)
    lease_repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    plan_repository = PostgresExecutionPlanRepository(POSTGRES_URL)

    await task_repository.start()
    await lease_repository.start()
    await plan_repository.start()

    yield (
        task_repository,
        lease_repository,
        plan_repository,
    )

    await plan_repository.stop()
    await lease_repository.stop()
    await task_repository.stop()


async def create_leased_task(repositories):
    task_repository, lease_repository, _ = repositories

    task = Task(input="Durable execution task")
    task = await task_repository.create_with_event(
        task,
        make_created_event(task),
    )

    lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-one",
        lease_duration_sec=30.0,
    )

    assert lease is not None
    return task, lease


def make_plan(task: Task) -> Plan:
    first = PlanStep(
        operation="echo",
        arguments={"message": "first"},
    )
    second = PlanStep(
        operation="echo",
        arguments={"message": "second"},
        dependencies=[first.id],
    )

    return Plan(
        task_id=task.id,
        steps=[first, second],
    )


@pytest.mark.asyncio
async def test_save_plan_creates_ordered_checkpoints(
    repositories,
) -> None:
    _, _, plan_repository = repositories
    task, lease = await create_leased_task(repositories)
    plan = make_plan(task)

    persisted = await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=lease.execution_attempt,
        lease_token=lease.lease_token,
        plan=plan,
    )

    assert persisted is not None
    assert persisted.plan == plan
    assert persisted.status == "validated"

    checkpoints = await plan_repository.list_checkpoints(plan.id)

    assert len(checkpoints) == 2
    assert [item.step_index for item in checkpoints] == [0, 1]
    assert [item.status for item in checkpoints] == [
        "pending",
        "pending",
    ]
    assert checkpoints[0].operation == "echo"
    assert checkpoints[0].attempt_count == 0
    assert checkpoints[0].idempotency_key.endswith(":0")
    assert checkpoints[1].idempotency_key.endswith(":1")

    duplicate = await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=lease.execution_attempt,
        lease_token=lease.lease_token,
        plan=plan,
    )

    assert duplicate == persisted
    assert len(await plan_repository.list_checkpoints(plan.id)) == 2


@pytest.mark.asyncio
async def test_stale_lease_cannot_persist_plan(
    repositories,
) -> None:
    _, lease_repository, plan_repository = repositories
    task, first_lease = await create_leased_task(repositories)

    await asyncio.sleep(0.01)

    async with lease_repository._require_engine().begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE task_execution_leases
                SET
                    acquired_at = now() - INTERVAL '2 seconds',
                    heartbeat_at = now() - INTERVAL '2 seconds',
                    expires_at = now() - INTERVAL '1 second',
                    updated_at = now()
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task.id},
        )

    second_lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-two",
        lease_duration_sec=30.0,
    )

    assert second_lease is not None

    stale_result = await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=first_lease.execution_attempt,
        lease_token=first_lease.lease_token,
        plan=make_plan(task),
    )

    assert stale_result is None


@pytest.mark.asyncio
async def test_checkpoint_completion_is_persisted(
    repositories,
) -> None:
    _, _, plan_repository = repositories
    task, lease = await create_leased_task(repositories)
    plan = make_plan(task)

    await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=lease.execution_attempt,
        lease_token=lease.lease_token,
        plan=plan,
    )

    checkpoint = (await plan_repository.list_checkpoints(plan.id))[0]

    started = await plan_repository.start_checkpoint(
        checkpoint.checkpoint_id,
        lease.lease_token,
    )

    assert started is not None
    assert started.status == "executing"
    assert started.attempt_count == 1

    completed = await plan_repository.complete_checkpoint(
        checkpoint.checkpoint_id,
        lease.lease_token,
        {"message": "finished"},
    )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.output == {"message": "finished"}
    assert completed.completed_at is not None


@pytest.mark.asyncio
async def test_retry_state_survives_and_increments_attempt(
    repositories,
) -> None:
    _, _, plan_repository = repositories
    task, lease = await create_leased_task(repositories)
    plan = make_plan(task)

    await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=lease.execution_attempt,
        lease_token=lease.lease_token,
        plan=plan,
    )

    checkpoint = (await plan_repository.list_checkpoints(plan.id))[0]

    started = await plan_repository.start_checkpoint(
        checkpoint.checkpoint_id,
        lease.lease_token,
    )
    assert started is not None
    assert started.attempt_count == 1

    retrying = await plan_repository.schedule_checkpoint_retry(
        checkpoint.checkpoint_id,
        lease.lease_token,
        {"code": "temporary_failure"},
        retry_delay_sec=0.05,
    )

    assert retrying is not None
    assert retrying.status == "retry_wait"
    assert retrying.retry_available_at is not None

    blocked = await plan_repository.start_checkpoint(
        checkpoint.checkpoint_id,
        lease.lease_token,
    )
    assert blocked is None

    await asyncio.sleep(0.06)

    restarted = await plan_repository.start_checkpoint(
        checkpoint.checkpoint_id,
        lease.lease_token,
    )

    assert restarted is not None
    assert restarted.status == "executing"
    assert restarted.attempt_count == 2


@pytest.mark.asyncio
async def test_stale_token_cannot_mutate_checkpoint(
    repositories,
) -> None:
    _, lease_repository, plan_repository = repositories
    task, first_lease = await create_leased_task(repositories)
    plan = make_plan(task)

    await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=first_lease.execution_attempt,
        lease_token=first_lease.lease_token,
        plan=plan,
    )

    checkpoint = (await plan_repository.list_checkpoints(plan.id))[0]

    async with lease_repository._require_engine().begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE task_execution_leases
                SET
                    acquired_at = now() - INTERVAL '2 seconds',
                    heartbeat_at = now() - INTERVAL '2 seconds',
                    expires_at = now() - INTERVAL '1 second',
                    updated_at = now()
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task.id},
        )

    second_lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id="worker-two",
        lease_duration_sec=30.0,
    )

    assert second_lease is not None

    stale_start = await plan_repository.start_checkpoint(
        checkpoint.checkpoint_id,
        first_lease.lease_token,
    )

    assert stale_start is None

    current_start = await plan_repository.start_checkpoint(
        checkpoint.checkpoint_id,
        second_lease.lease_token,
    )

    assert current_start is not None


@pytest.mark.asyncio
async def test_cancel_incomplete_checkpoints(
    repositories,
) -> None:
    _, _, plan_repository = repositories
    task, lease = await create_leased_task(repositories)
    plan = make_plan(task)

    await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=lease.execution_attempt,
        lease_token=lease.lease_token,
        plan=plan,
    )

    checkpoints = await plan_repository.list_checkpoints(plan.id)

    await plan_repository.start_checkpoint(
        checkpoints[0].checkpoint_id,
        lease.lease_token,
    )

    cancelled_count = await plan_repository.cancel_incomplete_checkpoints(
        task.id,
        lease.lease_token,
    )

    assert cancelled_count == 2

    cancelled = await plan_repository.list_checkpoints(plan.id)

    assert [item.status for item in cancelled] == [
        "cancelled",
        "cancelled",
    ]

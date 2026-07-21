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
from friday_brain.adapters.postgres_tool_invocation_repository import (
    PostgresToolInvocationRepository,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.contracts.tools import ToolInvocation
from friday_brain.protocols.execution_lease_repository import (
    ExecutionLease,
)
from friday_brain.protocols.execution_plan_repository import (
    StepCheckpoint,
)
from friday_brain.protocols.tool_invocation_repository import (
    ToolInvocationConflictError,
)


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
                    tool_invocations,
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
                    tool_invocations,
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
) -> AsyncIterator[
    tuple[
        PostgresTaskRepository,
        PostgresExecutionLeaseRepository,
        PostgresExecutionPlanRepository,
        PostgresToolInvocationRepository,
    ]
]:
    del postgres_engine

    task_repository = PostgresTaskRepository(POSTGRES_URL)
    lease_repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    plan_repository = PostgresExecutionPlanRepository(POSTGRES_URL)
    invocation_repository = PostgresToolInvocationRepository(POSTGRES_URL)

    await task_repository.start()
    await lease_repository.start()
    await plan_repository.start()
    await invocation_repository.start()

    yield (
        task_repository,
        lease_repository,
        plan_repository,
        invocation_repository,
    )

    await invocation_repository.stop()
    await plan_repository.stop()
    await lease_repository.stop()
    await task_repository.stop()


async def create_leased_checkpoint(
    repositories,
) -> tuple[
    Task,
    ExecutionLease,
    StepCheckpoint,
]:
    (
        task_repository,
        lease_repository,
        plan_repository,
        _,
    ) = repositories

    task = Task(input="ledger test")
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

    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation="echo",
                arguments={"message": "hello"},
            )
        ],
    )

    persisted = await plan_repository.save_validated_plan(
        task_id=task.id,
        task_version=task.version,
        execution_attempt=lease.execution_attempt,
        lease_token=lease.lease_token,
        plan=plan,
    )
    assert persisted is not None

    checkpoint = (await plan_repository.list_checkpoints(plan.id))[0]

    return task, lease, checkpoint


def make_invocation(
    *,
    task: Task,
    checkpoint: StepCheckpoint,
    arguments: dict[str, object] | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name="echo",
        arguments=(arguments if arguments is not None else {"message": "hello"}),
        idempotency_key=checkpoint.idempotency_key,
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )


@pytest.mark.asyncio
async def test_success_is_cached(
    repositories,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    repository = repositories[3]
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    claim = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert claim.outcome == "acquired"

    executing = await repository.mark_executing(
        invocation_id=claim.record.invocation_id,
        claim_token=claim.record.claim_token,
        lease_token=lease.lease_token,
        reservation_duration_sec=30.0,
    )
    assert executing is not None

    completed = await repository.complete_success(
        invocation_id=claim.record.invocation_id,
        claim_token=claim.record.claim_token,
        lease_token=lease.lease_token,
        output={"result": "Echo: hello"},
    )
    assert completed is not None
    assert completed.status == "succeeded"

    duplicate = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert duplicate.outcome == "cached_success"
    assert duplicate.record.output == {"result": "Echo: hello"}
    assert duplicate.record.execution_attempt == 1


@pytest.mark.asyncio
async def test_concurrent_claims_execute_only_once(
    repositories,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = PostgresToolInvocationRepository(POSTGRES_URL)
    second = PostgresToolInvocationRepository(POSTGRES_URL)

    await first.start()
    await second.start()

    try:
        first_claim, second_claim = await asyncio.gather(
            first.claim(
                invocation=invocation,
                lease_token=lease.lease_token,
                worker_id="worker-one",
                reservation_duration_sec=30.0,
            ),
            second.claim(
                invocation=invocation,
                lease_token=lease.lease_token,
                worker_id="worker-one",
                reservation_duration_sec=30.0,
            ),
        )
    finally:
        await second.stop()
        await first.stop()

    outcomes = {
        first_claim.outcome,
        second_claim.outcome,
    }

    assert outcomes == {"acquired", "busy"}
    assert first_claim.record.invocation_id == second_claim.record.invocation_id


@pytest.mark.asyncio
async def test_stale_reservation_can_be_taken_over(
    repositories,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    repository = repositories[3]
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=0.05,
    )
    assert first.outcome == "acquired"

    await asyncio.sleep(0.08)

    second = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert second.outcome == "acquired"
    assert second.record.claim_token != (first.record.claim_token)
    assert second.record.execution_attempt == 2

    stale_start = await repository.mark_executing(
        invocation_id=first.record.invocation_id,
        claim_token=first.record.claim_token,
        lease_token=lease.lease_token,
        reservation_duration_sec=30.0,
    )

    assert stale_start is None


@pytest.mark.asyncio
async def test_retryable_failure_can_be_reclaimed(
    repositories,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    repository = repositories[3]
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )
    assert first.outcome == "acquired"

    assert (
        await repository.mark_executing(
            invocation_id=first.record.invocation_id,
            claim_token=first.record.claim_token,
            lease_token=lease.lease_token,
            reservation_duration_sec=30.0,
        )
        is not None
    )

    failed = await repository.complete_failure(
        invocation_id=first.record.invocation_id,
        claim_token=first.record.claim_token,
        lease_token=lease.lease_token,
        error={"code": "temporary_failure"},
        retryable=True,
    )
    assert failed is not None

    second = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert second.outcome == "acquired"
    assert second.record.execution_attempt == 2
    assert second.record.error is None


@pytest.mark.asyncio
async def test_nonretryable_failure_is_cached(
    repositories,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    repository = repositories[3]
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert (
        await repository.mark_executing(
            invocation_id=first.record.invocation_id,
            claim_token=first.record.claim_token,
            lease_token=lease.lease_token,
            reservation_duration_sec=30.0,
        )
        is not None
    )

    assert (
        await repository.complete_failure(
            invocation_id=first.record.invocation_id,
            claim_token=first.record.claim_token,
            lease_token=lease.lease_token,
            error={"code": "permission_denied"},
            retryable=False,
        )
        is not None
    )

    duplicate = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert duplicate.outcome == "cached_failure"
    assert duplicate.record.error == {"code": "permission_denied"}


@pytest.mark.asyncio
async def test_idempotency_key_conflict_is_rejected(
    repositories,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    repository = repositories[3]

    original = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    await repository.claim(
        invocation=original,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    conflicting = make_invocation(
        task=task,
        checkpoint=checkpoint,
        arguments={"message": "different"},
    )

    with pytest.raises(ToolInvocationConflictError):
        await repository.claim(
            invocation=conflicting,
            lease_token=lease.lease_token,
            worker_id="worker-one",
            reservation_duration_sec=30.0,
        )


@pytest.mark.asyncio
async def test_stale_execution_lease_cannot_complete(
    repositories,
    postgres_engine: AsyncEngine,
) -> None:
    task, lease, checkpoint = await create_leased_checkpoint(repositories)
    repository = repositories[3]
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    claim = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="worker-one",
        reservation_duration_sec=30.0,
    )

    assert (
        await repository.mark_executing(
            invocation_id=claim.record.invocation_id,
            claim_token=claim.record.claim_token,
            lease_token=lease.lease_token,
            reservation_duration_sec=30.0,
        )
        is not None
    )

    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE task_execution_leases
                SET
                    acquired_at =
                        now() - INTERVAL '2 seconds',
                    heartbeat_at =
                        now() - INTERVAL '2 seconds',
                    expires_at =
                        now() - INTERVAL '1 second',
                    updated_at = now()
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task.id},
        )

    stale_completion = await repository.complete_success(
        invocation_id=claim.record.invocation_id,
        claim_token=claim.record.claim_token,
        lease_token=lease.lease_token,
        output={"result": "should not persist"},
    )

    assert stale_completion is None

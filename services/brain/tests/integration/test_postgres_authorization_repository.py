import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from friday_brain.adapters.postgres_authorization_repository import (
    PostgresAuthorizationRepository,
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
from friday_brain.contracts.tools import ToolPermission
from friday_brain.security.authorization import (
    digest_tool_arguments,
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
                    authorization_events,
                    tool_confirmation_grants,
                    task_permission_grants,
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
                    authorization_events,
                    tool_confirmation_grants,
                    task_permission_grants,
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
async def authorization_repository(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[PostgresAuthorizationRepository]:
    del postgres_engine

    repository = PostgresAuthorizationRepository(POSTGRES_URL)
    await repository.start()

    yield repository

    await repository.stop()


async def create_task_and_checkpoint() -> tuple[
    Task,
    object,
]:
    task_repository = PostgresTaskRepository(POSTGRES_URL)
    lease_repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    plan_repository = PostgresExecutionPlanRepository(POSTGRES_URL)

    await task_repository.start()
    await lease_repository.start()
    await plan_repository.start()

    try:
        task = Task(input="authorization test")
        task = await task_repository.create_with_event(
            task,
            make_created_event(task),
        )

        lease = await lease_repository.acquire(
            task_id=task.id,
            worker_id="authorization-worker",
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

        return task, checkpoint
    finally:
        await plan_repository.stop()
        await lease_repository.stop()
        await task_repository.stop()


@pytest.mark.asyncio
async def test_permission_grant_and_revocation(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, _ = await create_task_and_checkpoint()
    now = datetime.now(UTC)

    grant = await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.NETWORK_ACCESS,
        granted_by="user-one",
        expires_at=now + timedelta(minutes=10),
    )

    assert grant.permission == ToolPermission.NETWORK_ACCESS

    permissions = await authorization_repository.get_active_permissions(
        task_id=task.id,
        at=now,
    )
    assert permissions == frozenset({ToolPermission.NETWORK_ACCESS})

    revoked = await authorization_repository.revoke_permission(
        task_id=task.id,
        permission=ToolPermission.NETWORK_ACCESS,
        actor_id="user-one",
        reason="No longer needed.",
    )

    assert revoked is not None
    assert revoked.revoked_at is not None

    permissions = await authorization_repository.get_active_permissions(
        task_id=task.id,
        at=datetime.now(UTC),
    )
    assert permissions == frozenset()


@pytest.mark.asyncio
async def test_expired_permission_is_not_active(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, _ = await create_task_and_checkpoint()
    now = datetime.now(UTC)

    await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.NETWORK_ACCESS,
        granted_by="user-one",
        expires_at=now + timedelta(milliseconds=50),
    )

    await asyncio.sleep(0.08)

    permissions = await authorization_repository.get_active_permissions(
        task_id=task.id,
        at=datetime.now(UTC),
    )

    assert permissions == frozenset()


@pytest.mark.asyncio
async def test_confirmation_can_only_be_consumed_once(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, checkpoint = await create_task_and_checkpoint()
    digest = digest_tool_arguments({"message": "hello"})
    now = datetime.now(UTC)

    grant = await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        granted_by="user-one",
        expires_at=now + timedelta(minutes=5),
    )

    assert grant.consumed_at is None

    first = await authorization_repository.consume_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        at=datetime.now(UTC),
    )
    second = await authorization_repository.consume_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        at=datetime.now(UTC),
    )

    assert first is not None
    assert first.consumed_at is not None
    assert second is None


@pytest.mark.asyncio
async def test_confirmation_is_bound_to_exact_arguments(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, checkpoint = await create_task_and_checkpoint()
    approved_digest = digest_tool_arguments({"path": "/approved.txt"})
    changed_digest = digest_tool_arguments({"path": "/changed.txt"})

    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="filesystem.write",
        arguments_digest=approved_digest,
        granted_by="user-one",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await authorization_repository.consume_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="filesystem.write",
        arguments_digest=changed_digest,
        at=datetime.now(UTC),
    )

    assert result is None


@pytest.mark.asyncio
async def test_revoked_confirmation_cannot_be_consumed(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, checkpoint = await create_task_and_checkpoint()
    digest = digest_tool_arguments({"message": "hello"})

    grant = await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        granted_by="user-one",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    revoked = await authorization_repository.revoke_confirmation(
        task_id=task.id,
        grant_id=grant.grant_id,
        actor_id="user-one",
        reason="Cancelled.",
    )
    assert revoked is not None

    consumed = await authorization_repository.consume_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        at=datetime.now(UTC),
    )

    assert consumed is None


@pytest.mark.asyncio
async def test_authorization_actions_create_audit_events(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, checkpoint = await create_task_and_checkpoint()
    digest = digest_tool_arguments({"message": "hello"})

    await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.NETWORK_ACCESS,
        granted_by="user-one",
        expires_at=None,
    )

    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        granted_by="user-one",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    await authorization_repository.consume_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="echo",
        arguments_digest=digest,
        at=datetime.now(UTC),
    )

    events = await authorization_repository.list_events(task_id=task.id)

    event_types = {event.event_type for event in events}

    assert "permission_granted" in event_types
    assert "confirmation_granted" in event_types
    assert "confirmation_consumed" in event_types


@pytest.mark.asyncio
async def test_confirmation_is_consumed_once_under_concurrency(
    authorization_repository: PostgresAuthorizationRepository,
) -> None:
    task, checkpoint = await create_task_and_checkpoint()
    digest = digest_tool_arguments(
        {
            "path": "approved.txt",
            "content": "approved",
        }
    )

    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="filesystem.write_text",
        arguments_digest=digest,
        granted_by="user-one",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)),
    )

    results = await asyncio.gather(
        *[
            authorization_repository.consume_confirmation(
                task_id=task.id,
                checkpoint_id=(checkpoint.checkpoint_id),
                tool_name=("filesystem.write_text"),
                arguments_digest=digest,
                at=datetime.now(UTC),
            )
            for _ in range(4)
        ]
    )

    consumed = [result for result in results if result is not None]

    assert len(consumed) == 1

    assert (
        await authorization_repository.has_consumed_confirmation(
            task_id=task.id,
            checkpoint_id=(checkpoint.checkpoint_id),
            tool_name="filesystem.write_text",
            arguments_digest=digest,
        )
        is True
    )

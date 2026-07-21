import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
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
from friday_brain.adapters.postgres_tool_invocation_repository import (
    PostgresToolInvocationRepository,
)
from friday_brain.application.secure_tool_runtime import (
    SecureToolRuntime,
    ToolHandlerError,
)
from friday_brain.application.tool_registry import ToolRegistry
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolInvocation,
    ToolPermission,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
)
from friday_brain.security.authorization import (
    digest_tool_arguments,
)


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://friday:friday@localhost:5432/friday",
)


class AuthorizedInput(BaseModel):
    message: str

    model_config = ConfigDict(extra="forbid")


class AuthorizedOutput(BaseModel):
    result: str

    model_config = ConfigDict(extra="forbid")


class CountingHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        self.calls += 1
        assert isinstance(arguments, AuthorizedInput)
        assert ToolPermission.NETWORK_ACCESS in context.granted_permissions
        assert context.confirmation_granted is True

        return {"result": arguments.message}


class FailOnceHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context
        self.calls += 1
        assert isinstance(arguments, AuthorizedInput)

        if self.calls == 1:
            raise ToolHandlerError(
                code="temporary_failure",
                message="Try again.",
                retryable=True,
            )

        return {"result": arguments.message}


def make_event(
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
async def authorization_runtime_fixture() -> AsyncIterator[tuple[Any, ...]]:
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

    task_repository = PostgresTaskRepository(POSTGRES_URL)
    lease_repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    plan_repository = PostgresExecutionPlanRepository(POSTGRES_URL)
    invocation_repository = PostgresToolInvocationRepository(POSTGRES_URL)
    authorization_repository = PostgresAuthorizationRepository(POSTGRES_URL)

    await task_repository.start()
    await lease_repository.start()
    await plan_repository.start()
    await invocation_repository.start()
    await authorization_repository.start()

    task = Task(input="authorized runtime")
    task = await task_repository.create_with_event(
        task,
        make_event(task),
    )

    lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id="authorization-runtime-worker",
        lease_duration_sec=30.0,
    )
    assert lease is not None

    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation="authorized.test",
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

    yield (
        task,
        lease,
        checkpoint,
        invocation_repository,
        authorization_repository,
    )

    await authorization_repository.stop()
    await invocation_repository.stop()
    await plan_repository.stop()
    await lease_repository.stop()
    await task_repository.stop()
    await engine.dispose()


def make_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name="authorized.test",
                description="Authorization integration test.",
                input_model=AuthorizedInput,
                output_model=AuthorizedOutput,
                permissions=frozenset(
                    {
                        ToolPermission.NETWORK_ACCESS,
                    }
                ),
                requires_confirmation=True,
                timeout_sec=2.0,
            )
        ]
    )


def make_invocation(
    *,
    task,
    checkpoint,
    message: str = "hello",
) -> ToolInvocation:
    return ToolInvocation(
        tool_name="authorized.test",
        arguments={"message": message},
        idempotency_key=checkpoint.idempotency_key,
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )


@pytest.mark.asyncio
async def test_missing_permission_is_denied_and_audited(
    authorization_runtime_fixture,
) -> None:
    (
        task,
        lease,
        checkpoint,
        invocation_repository,
        authorization_repository,
    ) = authorization_runtime_fixture
    handler = CountingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"authorized.test": handler},
        invocation_repository=invocation_repository,
        authorization_repository=authorization_repository,
        worker_id="authorization-runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )

    result = await runtime.execute(
        make_invocation(
            task=task,
            checkpoint=checkpoint,
        ),
        lease_token=lease.lease_token,
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_permission_denied"
    assert handler.calls == 0

    events = await authorization_repository.list_events(task_id=task.id)
    assert any(event.event_type == "permission_denied" for event in events)


@pytest.mark.asyncio
async def test_confirmation_is_bound_to_exact_arguments(
    authorization_runtime_fixture,
) -> None:
    (
        task,
        lease,
        checkpoint,
        invocation_repository,
        authorization_repository,
    ) = authorization_runtime_fixture
    handler = CountingHandler()

    await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.NETWORK_ACCESS,
        granted_by="user",
        expires_at=None,
    )
    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="authorized.test",
        arguments_digest=digest_tool_arguments({"message": "hello"}),
        granted_by="user",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"authorized.test": handler},
        invocation_repository=invocation_repository,
        authorization_repository=authorization_repository,
        worker_id="authorization-runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )

    changed = await runtime.execute(
        make_invocation(
            task=task,
            checkpoint=checkpoint,
            message="changed",
        ),
        lease_token=lease.lease_token,
    )

    assert changed.success is False
    assert changed.error is not None
    assert changed.error.code == "tool_confirmation_required"
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_consumed_confirmation_survives_exact_retry(
    authorization_runtime_fixture,
) -> None:
    (
        task,
        lease,
        checkpoint,
        invocation_repository,
        authorization_repository,
    ) = authorization_runtime_fixture
    handler = FailOnceHandler()
    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.NETWORK_ACCESS,
        granted_by="user",
        expires_at=None,
    )
    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="authorized.test",
        arguments_digest=digest_tool_arguments(invocation.arguments),
        granted_by="user",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"authorized.test": handler},
        invocation_repository=invocation_repository,
        authorization_repository=authorization_repository,
        worker_id="authorization-runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )

    first = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )
    second = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert first.success is False
    assert first.error is not None
    assert first.error.code == "temporary_failure"

    assert second.success is True
    assert second.output == {"result": "hello"}
    assert handler.calls == 2

    assert (
        await authorization_repository.has_consumed_confirmation(
            task_id=task.id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_name="authorized.test",
            arguments_digest=digest_tool_arguments(invocation.arguments),
        )
        is True
    )

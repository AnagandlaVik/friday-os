import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from friday_brain.adapters.builtin_tool_handlers import (
    create_builtin_tool_handlers,
)
from friday_brain.adapters.builtin_tools import (
    create_builtin_tool_registry,
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
from friday_brain.contracts.plans import (
    Plan,
    PlanStep,
)
from friday_brain.contracts.tasks import Task
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolInvocation,
    ToolPermission,
    ToolRetryPolicy,
)
from friday_brain.protocols.execution_lease_repository import (
    ExecutionLease,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
)
from friday_brain.protocols.execution_plan_repository import (
    StepCheckpoint,
)
from friday_brain.security.authorization import (
    digest_tool_arguments,
)
from friday_brain.security.filesystem_sandbox import (
    FilesystemSandbox,
)


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    ("postgresql+asyncpg://friday:friday@localhost:5432/friday"),
)


class CountingFilesystemSandbox(FilesystemSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.write_calls = 0

    def write_text(
        self,
        relative_path: str,
        content: str,
        *,
        overwrite: bool = False,
    ) -> int:
        self.write_calls += 1

        return super().write_text(
            relative_path,
            content,
            overwrite=overwrite,
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


async def create_task_checkpoint(
    *,
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
    plan_repository: PostgresExecutionPlanRepository,
    worker_id: str,
    operation: str,
    arguments: dict[str, Any],
) -> tuple[
    Task,
    ExecutionLease,
    StepCheckpoint,
]:
    task = Task(input=f"Execute {operation}")
    task = await task_repository.create_with_event(
        task,
        make_created_event(task),
    )

    lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id=worker_id,
        lease_duration_sec=60.0,
    )
    assert lease is not None

    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation=operation,
                arguments=arguments,
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

    checkpoints = await plan_repository.list_checkpoints(plan.id)
    assert len(checkpoints) == 1

    return task, lease, checkpoints[0]


def make_invocation(
    *,
    task: Task,
    checkpoint: StepCheckpoint,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=checkpoint.operation,
        arguments=checkpoint.arguments,
        idempotency_key=(checkpoint.idempotency_key),
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )


@pytest.fixture
async def filesystem_environment(
    tmp_path: Path,
) -> AsyncIterator[dict[str, Any]]:
    engine: AsyncEngine = create_async_engine(
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

    root = tmp_path / "sandbox"
    root.mkdir()

    sandbox = CountingFilesystemSandbox(root)
    worker_id = "filesystem-worker"

    runtime = SecureToolRuntime(
        registry=create_builtin_tool_registry(),
        handlers=create_builtin_tool_handlers(sandbox),
        invocation_repository=(invocation_repository),
        authorization_repository=(authorization_repository),
        worker_id=worker_id,
        reservation_duration_sec=10.0,
        heartbeat_interval_sec=2.0,
    )

    yield {
        "task_repository": task_repository,
        "lease_repository": lease_repository,
        "plan_repository": plan_repository,
        "invocation_repository": (invocation_repository),
        "authorization_repository": (authorization_repository),
        "runtime": runtime,
        "sandbox": sandbox,
        "worker_id": worker_id,
    }

    await authorization_repository.stop()
    await invocation_repository.stop()
    await plan_repository.stop()
    await lease_repository.stop()
    await task_repository.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_is_denied_without_permissions(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    sandbox = environment["sandbox"]
    (sandbox.root / "hello.txt").write_text(
        "hello",
        encoding="utf-8",
    )

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="filesystem.read_text",
        arguments={"path": "hello.txt"},
    )

    result = await environment["runtime"].execute(
        make_invocation(
            task=task,
            checkpoint=checkpoint,
        ),
        lease_token=lease.lease_token,
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_permission_denied"


@pytest.mark.asyncio
async def test_read_succeeds_with_permissions(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    sandbox = environment["sandbox"]
    authorization_repository = environment["authorization_repository"]

    (sandbox.root / "hello.txt").write_text(
        "hello 🌎",
        encoding="utf-8",
    )

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="filesystem.read_text",
        arguments={"path": "hello.txt"},
    )

    for permission in (
        ToolPermission.READ_DATA,
        ToolPermission.FILESYSTEM_ACCESS,
    ):
        await authorization_repository.grant_permission(
            task_id=task.id,
            permission=permission,
            granted_by="test-user",
            expires_at=None,
        )

    result = await environment["runtime"].execute(
        make_invocation(
            task=task,
            checkpoint=checkpoint,
        ),
        lease_token=lease.lease_token,
    )

    assert result.success is True
    assert result.output == {
        "path": "hello.txt",
        "content": "hello 🌎",
        "size_bytes": len("hello 🌎".encode("utf-8")),
    }


@pytest.mark.asyncio
async def test_write_requires_exact_confirmation(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    authorization_repository = environment["authorization_repository"]

    arguments = {
        "path": "created.txt",
        "content": "created",
        "overwrite": False,
    }

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="filesystem.write_text",
        arguments=arguments,
    )

    for permission in (
        ToolPermission.WRITE_DATA,
        ToolPermission.FILESYSTEM_ACCESS,
    ):
        await authorization_repository.grant_permission(
            task_id=task.id,
            permission=permission,
            granted_by="test-user",
            expires_at=None,
        )

    result = await environment["runtime"].execute(
        make_invocation(
            task=task,
            checkpoint=checkpoint,
        ),
        lease_token=lease.lease_token,
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_confirmation_required"
    assert not (environment["sandbox"].root / "created.txt").exists()


@pytest.mark.asyncio
async def test_confirmed_write_is_cached_and_runs_once(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    sandbox = environment["sandbox"]
    authorization_repository = environment["authorization_repository"]

    arguments = {
        "path": "created.txt",
        "content": "created safely",
        "overwrite": False,
    }

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="filesystem.write_text",
        arguments=arguments,
    )

    for permission in (
        ToolPermission.WRITE_DATA,
        ToolPermission.FILESYSTEM_ACCESS,
    ):
        await authorization_repository.grant_permission(
            task_id=task.id,
            permission=permission,
            granted_by="test-user",
            expires_at=None,
        )

    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name=checkpoint.operation,
        arguments_digest=digest_tool_arguments(checkpoint.arguments),
        granted_by="test-user",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)),
    )

    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = await environment["runtime"].execute(
        invocation,
        lease_token=lease.lease_token,
    )
    second = await environment["runtime"].execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert first.success is True
    assert second.success is True
    assert first.output == {
        "path": "created.txt",
        "bytes_written": len("created safely".encode("utf-8")),
        "overwritten": False,
    }
    assert second.output == first.output

    assert sandbox.write_calls == 1
    assert (sandbox.root / "created.txt").read_text(
        encoding="utf-8"
    ) == "created safely"


@pytest.mark.asyncio
async def test_changed_write_arguments_cannot_reuse_confirmation(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    authorization_repository = environment["authorization_repository"]

    approved_arguments = {
        "path": "approved.txt",
        "content": "approved",
        "overwrite": False,
    }

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="filesystem.write_text",
        arguments=approved_arguments,
    )

    for permission in (
        ToolPermission.WRITE_DATA,
        ToolPermission.FILESYSTEM_ACCESS,
    ):
        await authorization_repository.grant_permission(
            task_id=task.id,
            permission=permission,
            granted_by="test-user",
            expires_at=None,
        )

    await authorization_repository.grant_confirmation(
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name=checkpoint.operation,
        arguments_digest=digest_tool_arguments(approved_arguments),
        granted_by="test-user",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)),
    )

    changed_invocation = ToolInvocation(
        tool_name=checkpoint.operation,
        arguments={
            "path": "changed.txt",
            "content": "not approved",
            "overwrite": False,
        },
        idempotency_key=(checkpoint.idempotency_key),
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )

    result = await environment["runtime"].execute(
        changed_invocation,
        lease_token=lease.lease_token,
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_confirmation_required"
    assert not (environment["sandbox"].root / "changed.txt").exists()


class AuthorizationRetryInput(BaseModel):
    message: str

    model_config = ConfigDict(extra="forbid")


class AuthorizationRetryOutput(BaseModel):
    result: str

    model_config = ConfigDict(extra="forbid")


class FlakyAuthorizationHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context
        assert isinstance(
            arguments,
            AuthorizationRetryInput,
        )

        self.calls += 1

        if self.calls == 1:
            raise ToolHandlerError(
                code="transient",
                message="Temporary failure.",
                retryable=True,
            )

        return {
            "result": arguments.message,
        }


def make_authorization_retry_runtime(
    environment: dict[str, Any],
    handler: FlakyAuthorizationHandler,
) -> SecureToolRuntime:
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="hardening.retry",
                description=("Verify authorization is reloaded before every retry."),
                input_model=AuthorizationRetryInput,
                output_model=AuthorizationRetryOutput,
                permissions=frozenset(
                    {
                        ToolPermission.READ_DATA,
                    }
                ),
                timeout_sec=5.0,
                retry_policy=ToolRetryPolicy(
                    max_attempts=2,
                    retryable_error_codes=(frozenset({"transient"})),
                ),
            )
        ]
    )

    return SecureToolRuntime(
        registry=registry,
        handlers={"hardening.retry": handler},
        invocation_repository=environment["invocation_repository"],
        authorization_repository=environment["authorization_repository"],
        worker_id=environment["worker_id"],
        reservation_duration_sec=10.0,
        heartbeat_interval_sec=2.0,
    )


@pytest.mark.asyncio
async def test_revoked_permission_blocks_retry(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    authorization_repository = environment["authorization_repository"]
    handler = FlakyAuthorizationHandler()
    runtime = make_authorization_retry_runtime(
        environment,
        handler,
    )

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="hardening.retry",
        arguments={"message": "hello"},
    )

    await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.READ_DATA,
        granted_by="test-user",
        expires_at=None,
    )

    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert first.success is False
    assert first.error is not None
    assert first.error.code == "transient"
    assert first.error.retryable is True
    assert handler.calls == 1

    revoked = await authorization_repository.revoke_permission(
        task_id=task.id,
        permission=ToolPermission.READ_DATA,
        actor_id="test-user",
        reason="Access removed.",
    )
    assert revoked is not None

    second = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert second.success is False
    assert second.error is not None
    assert second.error.code == "tool_permission_denied"
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_expired_permission_blocks_retry(
    filesystem_environment,
) -> None:
    environment = filesystem_environment
    authorization_repository = environment["authorization_repository"]
    handler = FlakyAuthorizationHandler()
    runtime = make_authorization_retry_runtime(
        environment,
        handler,
    )

    task, lease, checkpoint = await create_task_checkpoint(
        task_repository=environment["task_repository"],
        lease_repository=environment["lease_repository"],
        plan_repository=environment["plan_repository"],
        worker_id=environment["worker_id"],
        operation="hardening.retry",
        arguments={"message": "hello"},
    )

    await authorization_repository.grant_permission(
        task_id=task.id,
        permission=ToolPermission.READ_DATA,
        granted_by="test-user",
        expires_at=(datetime.now(UTC) + timedelta(seconds=1)),
    )

    invocation = make_invocation(
        task=task,
        checkpoint=checkpoint,
    )

    first = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert first.success is False
    assert first.error is not None
    assert first.error.code == "transient"
    assert handler.calls == 1

    await asyncio.sleep(1.1)

    second = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert second.success is False
    assert second.error is not None
    assert second.error.code == "tool_permission_denied"
    assert handler.calls == 1

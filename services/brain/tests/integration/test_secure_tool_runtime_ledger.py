import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

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
from friday_brain.contracts.events import Event, TaskCreatedPayload
from friday_brain.contracts.plans import Plan, PlanStep
from friday_brain.contracts.tasks import Task
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolInvocation,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
)


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://friday:friday@localhost:5432/friday",
)


class LedgerInput(BaseModel):
    message: str

    model_config = ConfigDict(extra="forbid")


class LedgerOutput(BaseModel):
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
        del context
        self.calls += 1
        assert isinstance(arguments, LedgerInput)

        return {
            "result": arguments.message,
        }


class TerminalFailureHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del arguments
        del context
        self.calls += 1

        raise ToolHandlerError(
            code="permission_denied",
            message="Permission denied.",
            retryable=False,
        )


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
async def runtime_fixture() -> AsyncIterator[tuple[Any, ...]]:
    task_repository = PostgresTaskRepository(POSTGRES_URL)
    lease_repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    plan_repository = PostgresExecutionPlanRepository(POSTGRES_URL)
    invocation_repository = PostgresToolInvocationRepository(POSTGRES_URL)

    await task_repository.start()
    await lease_repository.start()
    await plan_repository.start()
    await invocation_repository.start()

    task = Task(input="ledger runtime")
    task = await task_repository.create_with_event(
        task,
        make_event(task),
    )

    lease = await lease_repository.acquire(
        task_id=task.id,
        worker_id="runtime-worker",
        lease_duration_sec=30.0,
    )
    assert lease is not None

    plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(
                operation="test",
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
    )

    await invocation_repository.stop()
    await plan_repository.stop()
    await lease_repository.stop()
    await task_repository.stop()


def make_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name="test",
                description="Ledger integration test.",
                input_model=LedgerInput,
                output_model=LedgerOutput,
                timeout_sec=2.0,
            )
        ]
    )


@pytest.mark.asyncio
async def test_cached_success_does_not_reinvoke_handler(
    runtime_fixture,
) -> None:
    task, lease, checkpoint, repository = runtime_fixture
    handler = CountingHandler()
    runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"test": handler},
        invocation_repository=repository,
        worker_id="runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )
    invocation = ToolInvocation(
        tool_name="test",
        arguments={"message": "hello"},
        idempotency_key=checkpoint.idempotency_key,
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )

    first = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )
    second = await runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert first.success is True
    assert second.success is True
    assert first.output == {"result": "hello"}
    assert second.output == {"result": "hello"}
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_terminal_failure_is_cached(
    runtime_fixture,
) -> None:
    task, lease, checkpoint, repository = runtime_fixture
    handler = TerminalFailureHandler()
    runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"test": handler},
        invocation_repository=repository,
        worker_id="runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )
    invocation = ToolInvocation(
        tool_name="test",
        arguments={"message": "hello"},
        idempotency_key=checkpoint.idempotency_key,
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
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
    assert second.success is False
    assert first.error is not None
    assert second.error is not None
    assert first.error.code == "permission_denied"
    assert second.error.code == "permission_denied"
    assert handler.calls == 1


class BlockingCountingHandler:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context
        self.calls += 1
        assert isinstance(arguments, LedgerInput)

        self.started.set()
        await self.release.wait()

        return {
            "result": arguments.message,
        }


@pytest.mark.asyncio
async def test_two_runtime_calls_execute_handler_once(
    runtime_fixture,
) -> None:
    task, lease, checkpoint, repository = runtime_fixture
    handler = BlockingCountingHandler()

    first_runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"test": handler},
        invocation_repository=repository,
        worker_id="runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )
    second_runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"test": handler},
        invocation_repository=repository,
        worker_id="runtime-worker",
        reservation_duration_sec=5.0,
        heartbeat_interval_sec=1.0,
    )

    invocation = ToolInvocation(
        tool_name="test",
        arguments={"message": "hello"},
        idempotency_key=checkpoint.idempotency_key,
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )

    first_execution = asyncio.create_task(
        first_runtime.execute(
            invocation,
            lease_token=lease.lease_token,
        )
    )

    await asyncio.wait_for(
        handler.started.wait(),
        timeout=2.0,
    )

    second_result = await second_runtime.execute(
        invocation,
        lease_token=lease.lease_token,
    )

    assert second_result.success is False
    assert second_result.error is not None
    assert second_result.error.code == "tool_invocation_busy"

    handler.release.set()
    first_result = await first_execution

    assert first_result.success is True
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_stale_takeover_fences_previous_claim(
    runtime_fixture,
) -> None:
    task, lease, checkpoint, repository = runtime_fixture

    invocation = ToolInvocation(
        tool_name="test",
        arguments={"message": "hello"},
        idempotency_key=checkpoint.idempotency_key,
        task_id=task.id,
        checkpoint_id=checkpoint.checkpoint_id,
    )

    first_claim = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="runtime-worker",
        reservation_duration_sec=0.2,
    )
    assert first_claim.outcome == "acquired"

    first_executing = await repository.mark_executing(
        invocation_id=(first_claim.record.invocation_id),
        claim_token=(first_claim.record.claim_token),
        lease_token=lease.lease_token,
        reservation_duration_sec=0.2,
    )
    assert first_executing is not None

    await asyncio.sleep(0.35)

    second_claim = await repository.claim(
        invocation=invocation,
        lease_token=lease.lease_token,
        worker_id="runtime-worker",
        reservation_duration_sec=5.0,
    )

    assert second_claim.outcome == "acquired"
    assert second_claim.record.claim_token != first_claim.record.claim_token

    stale_completion = await repository.complete_success(
        invocation_id=(first_claim.record.invocation_id),
        claim_token=(first_claim.record.claim_token),
        lease_token=lease.lease_token,
        output={"result": "stale"},
    )

    assert stale_completion is None

    second_executing = await repository.mark_executing(
        invocation_id=(second_claim.record.invocation_id),
        claim_token=(second_claim.record.claim_token),
        lease_token=lease.lease_token,
        reservation_duration_sec=5.0,
    )
    assert second_executing is not None

    completed = await repository.complete_success(
        invocation_id=(second_claim.record.invocation_id),
        claim_token=(second_claim.record.claim_token),
        lease_token=lease.lease_token,
        output={"result": "recovered"},
    )

    assert completed is not None
    assert completed.output == {"result": "recovered"}

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from friday_brain.application.secure_tool_runtime import (
    SecureToolRuntime,
)
from friday_brain.application.tool_registry import (
    ToolRegistry,
)
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolInvocation,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
)


class HardeningInput(BaseModel):
    message: str

    model_config = ConfigDict(extra="forbid")


class HardeningOutput(BaseModel):
    result: str

    model_config = ConfigDict(extra="forbid")


class BlockingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context
        assert isinstance(
            arguments,
            HardeningInput,
        )

        self.started.set()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class FakeInvocationRepository:
    def __init__(self) -> None:
        self.record = SimpleNamespace(
            invocation_id=uuid4(),
            claim_token=uuid4(),
            output=None,
            error=None,
        )
        self.renew_calls = 0
        self.completed_success = False
        self.completed_failure = False

    async def claim(self, **kwargs):
        del kwargs

        return SimpleNamespace(
            outcome="acquired",
            record=self.record,
        )

    async def mark_executing(self, **kwargs):
        del kwargs
        return self.record

    async def renew(self, **kwargs):
        del kwargs
        self.renew_calls += 1
        return self.record

    async def complete_success(self, **kwargs):
        del kwargs
        self.completed_success = True
        return self.record

    async def complete_failure(self, **kwargs):
        del kwargs
        self.completed_failure = True
        return self.record


def make_registry(
    *,
    timeout_sec: float = 30.0,
) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name="hardening.test",
                description=("Cancellation hardening test."),
                input_model=HardeningInput,
                output_model=HardeningOutput,
                timeout_sec=timeout_sec,
            )
        ]
    )


@pytest.mark.asyncio
async def test_runtime_cancellation_stops_handler_and_heartbeat() -> None:
    repository = FakeInvocationRepository()
    handler = BlockingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(),
        handlers={"hardening.test": handler},
        invocation_repository=repository,
        worker_id="hardening-worker",
        reservation_duration_sec=1.0,
        heartbeat_interval_sec=0.01,
    )

    invocation = ToolInvocation(
        tool_name="hardening.test",
        arguments={"message": "hello"},
        idempotency_key="cancel-hardening",
        task_id=uuid4(),
        checkpoint_id=uuid4(),
    )

    execution = asyncio.create_task(
        runtime.execute(
            invocation,
            lease_token=uuid4(),
        )
    )

    await asyncio.wait_for(
        handler.started.wait(),
        timeout=1.0,
    )

    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    await asyncio.wait_for(
        handler.cancelled.wait(),
        timeout=1.0,
    )

    renew_calls = repository.renew_calls
    await asyncio.sleep(0.05)

    assert repository.renew_calls == renew_calls
    assert repository.completed_success is False
    assert repository.completed_failure is False


class LostRenewRepository(FakeInvocationRepository):
    async def renew(self, **kwargs):
        del kwargs
        self.renew_calls += 1
        return None


class RaisingRenewRepository(FakeInvocationRepository):
    async def renew(self, **kwargs):
        del kwargs
        self.renew_calls += 1
        raise RuntimeError("Database connection failed.")


class BlockingRenewRepository(FakeInvocationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.renew_started = asyncio.Event()
        self.renew_cancelled = asyncio.Event()

    async def renew(self, **kwargs):
        del kwargs
        self.renew_calls += 1
        self.renew_started.set()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.renew_cancelled.set()
            raise


def make_invocation(
    idempotency_key: str,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name="hardening.test",
        arguments={"message": "hello"},
        idempotency_key=idempotency_key,
        task_id=uuid4(),
        checkpoint_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_handler() -> None:
    repository = LostRenewRepository()
    handler = BlockingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(timeout_sec=5.0),
        handlers={"hardening.test": handler},
        invocation_repository=repository,
        worker_id="hardening-worker",
        reservation_duration_sec=1.0,
        heartbeat_interval_sec=0.01,
    )

    result = await runtime.execute(
        make_invocation("heartbeat-ownership-loss"),
        lease_token=uuid4(),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_invocation_lease_lost"

    await asyncio.wait_for(
        handler.cancelled.wait(),
        timeout=1.0,
    )

    assert repository.renew_calls >= 1
    assert repository.completed_success is False
    assert repository.completed_failure is False


@pytest.mark.asyncio
async def test_heartbeat_repository_error_fails_closed() -> None:
    repository = RaisingRenewRepository()
    handler = BlockingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(timeout_sec=5.0),
        handlers={"hardening.test": handler},
        invocation_repository=repository,
        worker_id="hardening-worker",
        reservation_duration_sec=1.0,
        heartbeat_interval_sec=0.01,
    )

    result = await runtime.execute(
        make_invocation("heartbeat-repository-error"),
        lease_token=uuid4(),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_invocation_lease_lost"

    await asyncio.wait_for(
        handler.cancelled.wait(),
        timeout=1.0,
    )

    assert repository.completed_success is False
    assert repository.completed_failure is False


@pytest.mark.asyncio
async def test_handler_timeout_wins_before_heartbeat() -> None:
    repository = FakeInvocationRepository()
    handler = BlockingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(timeout_sec=0.02),
        handlers={"hardening.test": handler},
        invocation_repository=repository,
        worker_id="hardening-worker",
        reservation_duration_sec=2.0,
        heartbeat_interval_sec=1.0,
    )

    result = await runtime.execute(
        make_invocation("handler-timeout-first"),
        lease_token=uuid4(),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_timeout"

    await asyncio.wait_for(
        handler.cancelled.wait(),
        timeout=1.0,
    )

    assert repository.renew_calls == 0
    assert repository.completed_failure is True
    assert repository.completed_success is False


@pytest.mark.asyncio
async def test_lease_loss_wins_before_handler_timeout() -> None:
    repository = LostRenewRepository()
    handler = BlockingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(timeout_sec=2.0),
        handlers={"hardening.test": handler},
        invocation_repository=repository,
        worker_id="hardening-worker",
        reservation_duration_sec=1.0,
        heartbeat_interval_sec=0.01,
    )

    result = await runtime.execute(
        make_invocation("lease-loss-before-timeout"),
        lease_token=uuid4(),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_invocation_lease_lost"

    await asyncio.wait_for(
        handler.cancelled.wait(),
        timeout=1.0,
    )

    assert repository.completed_failure is False
    assert repository.completed_success is False


@pytest.mark.asyncio
async def test_runtime_cancellation_stops_blocked_renewal() -> None:
    repository = BlockingRenewRepository()
    handler = BlockingHandler()

    runtime = SecureToolRuntime(
        registry=make_registry(timeout_sec=30.0),
        handlers={"hardening.test": handler},
        invocation_repository=repository,
        worker_id="hardening-worker",
        reservation_duration_sec=2.0,
        heartbeat_interval_sec=0.01,
    )

    execution = asyncio.create_task(
        runtime.execute(
            make_invocation("cancel-blocked-heartbeat"),
            lease_token=uuid4(),
        )
    )

    await asyncio.wait_for(
        handler.started.wait(),
        timeout=1.0,
    )
    await asyncio.wait_for(
        repository.renew_started.wait(),
        timeout=1.0,
    )

    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    await asyncio.wait_for(
        handler.cancelled.wait(),
        timeout=1.0,
    )
    await asyncio.wait_for(
        repository.renew_cancelled.wait(),
        timeout=1.0,
    )

    assert repository.completed_success is False
    assert repository.completed_failure is False

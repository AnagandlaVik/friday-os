import asyncio
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field

from friday_brain.application.secure_tool_runtime import (
    DuplicateToolHandlerError,
    SecureToolRuntime,
    ToolHandlerError,
)
from friday_brain.application.tool_registry import (
    ToolRegistry,
)
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolInvocation,
    ToolPermission,
    ToolRetryPolicy,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
)


class ExampleInput(BaseModel):
    message: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class ExampleOutput(BaseModel):
    result: str

    model_config = ConfigDict(extra="forbid")


class SuccessfulHandler:
    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        assert isinstance(arguments, ExampleInput)

        return {"result": (f"{arguments.message}:{context.idempotency_key}")}


class SlowHandler:
    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del arguments
        del context

        await asyncio.sleep(10)

        return {"result": "too late"}


class MalformedOutputHandler:
    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del arguments
        del context

        return {"wrong_field": "bad"}


class StructuredFailureHandler:
    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del arguments
        del context

        raise ToolHandlerError(
            code="temporary_failure",
            message="Provider temporarily unavailable.",
            details={"provider": "example"},
        )


class UnexpectedFailureHandler:
    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del arguments
        del context

        raise RuntimeError("secret internal detail")


class BlockingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del arguments
        del context

        self.started.set()

        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


def make_definition(
    *,
    name: str = "example",
    timeout_sec: float = 1.0,
    permissions: frozenset[ToolPermission] = frozenset(),
    requires_confirmation: bool = False,
    retryable_error_codes: frozenset[str] = frozenset(),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Example test tool.",
        input_model=ExampleInput,
        output_model=ExampleOutput,
        permissions=permissions,
        requires_confirmation=(requires_confirmation),
        timeout_sec=timeout_sec,
        retry_policy=ToolRetryPolicy(
            max_attempts=3,
            base_delay_sec=0.1,
            max_delay_sec=1.0,
            retryable_error_codes=(retryable_error_codes),
        ),
    )


def make_invocation(
    *,
    tool_name: str = "example",
    arguments: dict[str, Any] | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name,
        arguments=(arguments if arguments is not None else {"message": "hello"}),
        idempotency_key="stable-key",
        task_id=uuid4(),
        checkpoint_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_runtime_validates_and_executes_tool() -> None:
    registry = ToolRegistry([make_definition()])
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": SuccessfulHandler(),
        },
    )

    result = await runtime.execute(make_invocation())

    assert result.success is True
    assert result.output == {"result": "hello:stable-key"}
    assert result.error is None


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error() -> None:
    runtime = SecureToolRuntime(registry=ToolRegistry())

    result = await runtime.execute(make_invocation(tool_name="missing"))

    assert result.success is False
    assert result.error is not None
    assert result.error.code == ("tool_not_registered")


@pytest.mark.asyncio
async def test_invalid_arguments_are_rejected() -> None:
    registry = ToolRegistry([make_definition()])
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": SuccessfulHandler(),
        },
    )

    result = await runtime.execute(make_invocation(arguments={"message": ""}))

    assert result.success is False
    assert result.error is not None
    assert result.error.code == ("invalid_tool_arguments")


@pytest.mark.asyncio
async def test_missing_permission_is_rejected() -> None:
    registry = ToolRegistry(
        [
            make_definition(
                permissions=frozenset(
                    {
                        ToolPermission.NETWORK_ACCESS,
                    }
                )
            )
        ]
    )
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": SuccessfulHandler(),
        },
    )

    result = await runtime.execute(make_invocation())

    assert result.success is False
    assert result.error is not None
    assert result.error.code == ("tool_permission_denied")
    assert result.error.details == {"missing_permissions": ["network_access"]}


@pytest.mark.asyncio
async def test_confirmation_is_required() -> None:
    registry = ToolRegistry([make_definition(requires_confirmation=True)])
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": SuccessfulHandler(),
        },
    )

    denied = await runtime.execute(make_invocation())

    assert denied.success is False
    assert denied.error is not None
    assert denied.error.code == ("tool_confirmation_required")

    allowed = await runtime.execute(
        make_invocation(),
        confirmation_granted=True,
    )

    assert allowed.success is True


@pytest.mark.asyncio
async def test_timeout_is_structured_and_retryable() -> None:
    registry = ToolRegistry(
        [
            make_definition(
                timeout_sec=0.01,
                retryable_error_codes=frozenset({"tool_timeout"}),
            )
        ]
    )
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": SlowHandler(),
        },
    )

    result = await runtime.execute(make_invocation())

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_timeout"
    assert result.error.retryable is True


@pytest.mark.asyncio
async def test_structured_handler_failure_uses_policy() -> None:
    registry = ToolRegistry(
        [make_definition(retryable_error_codes=frozenset({"temporary_failure"}))]
    )
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": StructuredFailureHandler(),
        },
    )

    result = await runtime.execute(make_invocation())

    assert result.success is False
    assert result.error is not None
    assert result.error.code == ("temporary_failure")
    assert result.error.retryable is True
    assert result.error.details == {"provider": "example"}


@pytest.mark.asyncio
async def test_invalid_output_is_rejected() -> None:
    registry = ToolRegistry([make_definition()])
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": MalformedOutputHandler(),
        },
    )

    result = await runtime.execute(make_invocation())

    assert result.success is False
    assert result.error is not None
    assert result.error.code == ("invalid_tool_output")


@pytest.mark.asyncio
async def test_unexpected_failure_hides_internal_message() -> None:
    registry = ToolRegistry([make_definition()])
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={
            "example": UnexpectedFailureHandler(),
        },
    )

    result = await runtime.execute(make_invocation())

    assert result.success is False
    assert result.error is not None
    assert result.error.code == ("tool_execution_failed")
    assert "secret internal detail" not in (result.error.message)
    assert result.error.details == {"exception_type": "RuntimeError"}


@pytest.mark.asyncio
async def test_cancellation_is_propagated() -> None:
    registry = ToolRegistry([make_definition()])
    handler = BlockingHandler()
    runtime = SecureToolRuntime(
        registry=registry,
        handlers={"example": handler},
    )

    execution = asyncio.create_task(runtime.execute(make_invocation()))

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


def test_duplicate_handler_is_rejected() -> None:
    registry = ToolRegistry([make_definition()])
    runtime = SecureToolRuntime(registry=registry)

    runtime.register_handler(
        "example",
        SuccessfulHandler(),
    )

    with pytest.raises(DuplicateToolHandlerError):
        runtime.register_handler(
            "example",
            SuccessfulHandler(),
        )

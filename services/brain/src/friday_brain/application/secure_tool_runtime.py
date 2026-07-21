import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from friday_brain.application.tool_registry import (
    ToolConfirmationRequiredError,
    ToolNotRegisteredError,
    ToolPermissionDeniedError,
    ToolRegistry,
)
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolExecutionError,
    ToolExecutionResult,
    ToolInvocation,
    ToolPermission,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
    ToolHandler,
)
from friday_brain.protocols.tool_invocation_repository import (
    ToolInvocationConflictError,
    ToolInvocationLeaseLostError,
    ToolInvocationRecord,
    ToolInvocationRepository,
)


class SecureToolRuntimeError(RuntimeError):
    """Base error for runtime configuration failures."""


class DuplicateToolHandlerError(SecureToolRuntimeError):
    """Raised when one handler is registered twice."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"A handler for tool '{tool_name}' is already registered.")


class ToolHandlerNotRegisteredError(SecureToolRuntimeError):
    """Raised when a registered tool has no implementation."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"No handler is registered for tool '{tool_name}'.")


class ToolHandlerError(RuntimeError):
    """Expected structured failure raised by a handler."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details
        super().__init__(message)


class ToolExecutionFailedError(RuntimeError):
    """Failure returned by the secure runtime to durable execution."""

    def __init__(
        self,
        *,
        error: ToolExecutionError,
        max_attempts: int,
        base_delay_sec: float,
        max_delay_sec: float,
    ) -> None:
        self.code = error.code
        self.message = error.message
        self.retryable = error.retryable
        self.details = error.details
        self.max_attempts = max_attempts
        self.base_delay_sec = base_delay_sec
        self.max_delay_sec = max_delay_sec

        super().__init__(error.message)


class SecureToolRuntime:
    """
    Validate and execute registered tools inside a durable security boundary.

    When an invocation repository is configured, successful and terminal
    failed results are cached by idempotency key across retries and restarts.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        handlers: Mapping[str, ToolHandler] | None = None,
        invocation_repository: (ToolInvocationRepository | None) = None,
        worker_id: str | None = None,
        reservation_duration_sec: float = 30.0,
        heartbeat_interval_sec: float = 10.0,
    ) -> None:
        if reservation_duration_sec <= 0:
            raise ValueError("Invocation reservation duration must be positive.")

        if heartbeat_interval_sec <= 0:
            raise ValueError("Invocation heartbeat interval must be positive.")

        if heartbeat_interval_sec >= reservation_duration_sec:
            raise ValueError(
                "Invocation heartbeat interval must be shorter "
                "than the reservation duration."
            )

        if invocation_repository is not None and not worker_id:
            raise ValueError(
                "A worker ID is required when the invocation repository is enabled."
            )

        self._registry = registry
        self._handlers: dict[str, ToolHandler] = {}
        self._invocation_repository = invocation_repository
        self._worker_id = worker_id
        self._reservation_duration_sec = reservation_duration_sec
        self._heartbeat_interval_sec = heartbeat_interval_sec

        if handlers is not None:
            for tool_name, handler in handlers.items():
                self.register_handler(
                    tool_name,
                    handler,
                )

    def register_handler(
        self,
        tool_name: str,
        handler: ToolHandler,
    ) -> None:
        self._registry.get(tool_name)

        if tool_name in self._handlers:
            raise DuplicateToolHandlerError(tool_name)

        self._handlers[tool_name] = handler

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        granted_permissions: frozenset[ToolPermission] = frozenset(),
        confirmation_granted: bool = False,
        lease_token: UUID | None = None,
    ) -> ToolExecutionResult:
        try:
            definition = self._registry.validate_access(
                invocation.tool_name,
                granted_permissions=granted_permissions,
                confirmation_granted=confirmation_granted,
            )
        except ToolNotRegisteredError:
            return ToolExecutionResult.failed(
                code="tool_not_registered",
                message=(f"Tool '{invocation.tool_name}' is not registered."),
                retryable=False,
            )
        except ToolPermissionDeniedError as error:
            return ToolExecutionResult.failed(
                code="tool_permission_denied",
                message=str(error),
                retryable=False,
                details={
                    "missing_permissions": sorted(
                        permission.value for permission in error.missing_permissions
                    )
                },
            )
        except ToolConfirmationRequiredError as error:
            return ToolExecutionResult.failed(
                code="tool_confirmation_required",
                message=str(error),
                retryable=False,
            )

        try:
            arguments = self._registry.validate_arguments(
                invocation.tool_name,
                invocation.arguments,
            )
        except ValidationError as error:
            return ToolExecutionResult.failed(
                code="invalid_tool_arguments",
                message=(f"Arguments for tool '{invocation.tool_name}' are invalid."),
                retryable=False,
                details={"validation_errors": error.errors(include_url=False)},
            )

        handler = self._handlers.get(invocation.tool_name)

        if handler is None:
            return ToolExecutionResult.failed(
                code="tool_handler_not_registered",
                message=str(ToolHandlerNotRegisteredError(invocation.tool_name)),
                retryable=False,
            )

        context = ToolExecutionContext(
            task_id=invocation.task_id,
            checkpoint_id=invocation.checkpoint_id,
            idempotency_key=(invocation.idempotency_key),
            granted_permissions=granted_permissions,
            confirmation_granted=confirmation_granted,
        )

        if self._invocation_repository is None:
            return await self._execute_handler(
                definition=definition,
                handler=handler,
                arguments=arguments,
                context=context,
            )

        if lease_token is None:
            return ToolExecutionResult.failed(
                code="tool_invocation_lease_required",
                message=(
                    "A durable execution lease is required for this tool invocation."
                ),
                retryable=True,
            )

        if self._worker_id is None:
            raise RuntimeError("Ledger-backed runtime has no worker ID.")

        try:
            claim = await self._invocation_repository.claim(
                invocation=invocation,
                lease_token=lease_token,
                worker_id=self._worker_id,
                reservation_duration_sec=(self._reservation_duration_sec),
            )
        except ToolInvocationConflictError as error:
            return ToolExecutionResult.failed(
                code="tool_invocation_conflict",
                message=str(error),
                retryable=False,
            )
        except ToolInvocationLeaseLostError as error:
            return ToolExecutionResult.failed(
                code="tool_invocation_lease_lost",
                message=str(error),
                retryable=True,
            )

        if claim.outcome == "cached_success":
            return ToolExecutionResult.succeeded(claim.record.output)

        if claim.outcome == "cached_failure":
            return self._cached_failure(claim.record)

        if claim.outcome == "busy":
            return ToolExecutionResult.failed(
                code="tool_invocation_busy",
                message=(
                    "The tool invocation is already owned by another active execution."
                ),
                retryable=True,
                details={"invocation_id": str(claim.record.invocation_id)},
            )

        executing = await self._invocation_repository.mark_executing(
            invocation_id=claim.record.invocation_id,
            claim_token=claim.record.claim_token,
            lease_token=lease_token,
            reservation_duration_sec=(self._reservation_duration_sec),
        )

        if executing is None:
            return ToolExecutionResult.failed(
                code="tool_invocation_lease_lost",
                message=(
                    "The invocation reservation was lost before execution started."
                ),
                retryable=True,
            )

        try:
            result = await self._execute_with_heartbeat(
                definition=definition,
                handler=handler,
                arguments=arguments,
                context=context,
                record=executing,
                lease_token=lease_token,
            )
        except ToolInvocationLeaseLostError as error:
            return ToolExecutionResult.failed(
                code="tool_invocation_lease_lost",
                message=str(error),
                retryable=True,
            )

        if result.success:
            completed = await self._invocation_repository.complete_success(
                invocation_id=executing.invocation_id,
                claim_token=executing.claim_token,
                lease_token=lease_token,
                output=result.output,
            )
        else:
            if result.error is None:
                raise RuntimeError("Failed tool result has no error.")

            completed = await self._invocation_repository.complete_failure(
                invocation_id=executing.invocation_id,
                claim_token=executing.claim_token,
                lease_token=lease_token,
                error=result.error.model_dump(mode="json"),
                retryable=result.error.retryable,
            )

        if completed is None:
            return ToolExecutionResult.failed(
                code="tool_invocation_lease_lost",
                message=(
                    "The invocation reservation was lost "
                    "before its result could be persisted."
                ),
                retryable=True,
            )

        return result

    async def _execute_with_heartbeat(
        self,
        *,
        definition: ToolDefinition,
        handler: ToolHandler,
        arguments: Any,
        context: ToolExecutionContext,
        record: ToolInvocationRecord,
        lease_token: UUID,
    ) -> ToolExecutionResult:
        execution = asyncio.create_task(
            self._execute_handler(
                definition=definition,
                handler=handler,
                arguments=arguments,
                context=context,
            )
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_invocation(
                record=record,
                lease_token=lease_token,
            )
        )

        done, _ = await asyncio.wait(
            {execution, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if execution in done:
            heartbeat.cancel()
            await asyncio.gather(
                heartbeat,
                return_exceptions=True,
            )

            return await execution

        execution.cancel()
        await asyncio.gather(
            execution,
            return_exceptions=True,
        )

        await heartbeat

        raise ToolInvocationLeaseLostError("Tool invocation ownership was lost.")

    async def _heartbeat_invocation(
        self,
        *,
        record: ToolInvocationRecord,
        lease_token: UUID,
    ) -> None:
        if self._invocation_repository is None:
            return

        while True:
            await asyncio.sleep(self._heartbeat_interval_sec)

            renewed = await self._invocation_repository.renew(
                invocation_id=record.invocation_id,
                claim_token=record.claim_token,
                lease_token=lease_token,
                reservation_duration_sec=(self._reservation_duration_sec),
            )

            if renewed is None:
                raise ToolInvocationLeaseLostError(
                    "Tool invocation reservation could not be renewed."
                )

    async def _execute_handler(
        self,
        *,
        definition: ToolDefinition,
        handler: ToolHandler,
        arguments: Any,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        try:
            async with asyncio.timeout(definition.timeout_sec):
                raw_output = await handler.execute(
                    arguments,
                    context,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._failure(
                definition=definition,
                code="tool_timeout",
                message=(
                    f"Tool '{definition.name}' exceeded "
                    f"its {definition.timeout_sec} second timeout."
                ),
            )
        except ToolHandlerError as error:
            retryable = (
                error.retryable
                if error.retryable is not None
                else self._is_retryable(
                    definition,
                    error.code,
                )
            )

            return ToolExecutionResult.failed(
                code=error.code,
                message=error.message,
                retryable=retryable,
                details=error.details,
            )
        except Exception as error:
            return self._failure(
                definition=definition,
                code="tool_execution_failed",
                message=(f"Tool '{definition.name}' failed unexpectedly."),
                details={"exception_type": (type(error).__name__)},
            )

        try:
            output = self._validate_output(
                definition,
                raw_output,
            )
        except ValidationError as error:
            return ToolExecutionResult.failed(
                code="invalid_tool_output",
                message=(
                    f"Tool '{definition.name}' returned "
                    "output that did not match its "
                    "declared schema."
                ),
                retryable=False,
                details={"validation_errors": error.errors(include_url=False)},
            )

        return ToolExecutionResult.succeeded(output)

    @staticmethod
    def _cached_failure(
        record: ToolInvocationRecord,
    ) -> ToolExecutionResult:
        if record.error is None:
            return ToolExecutionResult.failed(
                code="cached_tool_failure",
                message=(
                    "A prior invocation failed without a stored structured error."
                ),
                retryable=False,
            )

        try:
            error = ToolExecutionError.model_validate(record.error)
        except ValidationError:
            return ToolExecutionResult.failed(
                code="cached_tool_failure",
                message=("A prior invocation has an invalid stored error record."),
                retryable=False,
                details={"stored_error": record.error},
            )

        return ToolExecutionResult(
            success=False,
            error=error,
        )

    @staticmethod
    def _validate_output(
        definition: ToolDefinition,
        raw_output: Any,
    ) -> Any:
        if definition.output_model is None:
            return raw_output

        validated = definition.output_model.model_validate(raw_output)

        return validated.model_dump(mode="json")

    def _failure(
        self,
        *,
        definition: ToolDefinition,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult.failed(
            code=code,
            message=message,
            retryable=self._is_retryable(
                definition,
                code,
            ),
            details=details,
        )

    @staticmethod
    def _is_retryable(
        definition: ToolDefinition,
        error_code: str,
    ) -> bool:
        return error_code in definition.retry_policy.retryable_error_codes

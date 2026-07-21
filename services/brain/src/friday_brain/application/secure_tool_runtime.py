import asyncio
from collections.abc import Mapping
from typing import Any

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
    Validates and executes registered tools inside a security boundary.

    Cancellation is always propagated. All normal execution failures are
    returned as ToolExecutionResult values.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        handlers: Mapping[str, ToolHandler] | None = None,
    ) -> None:
        self._registry = registry
        self._handlers: dict[str, ToolHandler] = {}

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
        # Require a definition before accepting an implementation.
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
                    f"Tool '{invocation.tool_name}' "
                    f"exceeded its {definition.timeout_sec} "
                    "second timeout."
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
                message=(f"Tool '{invocation.tool_name}' failed unexpectedly."),
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
                    f"Tool '{invocation.tool_name}' "
                    "returned output that did not match "
                    "its declared schema."
                ),
                retryable=False,
                details={"validation_errors": error.errors(include_url=False)},
            )

        return ToolExecutionResult.succeeded(output)

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

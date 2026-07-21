import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from structlog.contextvars import bound_contextvars

from friday_brain.application.tool_registry import (
    ToolNotRegisteredError,
    ToolRegistry,
)
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolExecutionError,
    ToolExecutionResult,
    ToolInvocation,
    ToolPermission,
)
from friday_brain.observability.metrics import MetricsRegistry
from friday_brain.protocols.authorization_repository import (
    AuthorizationRepository,
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
from friday_brain.security.authorization import (
    digest_tool_arguments,
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
    """Failure returned by the runtime to durable execution."""

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
    Validate, authorize, deduplicate, and execute registered tools.

    Durable confirmation is consumed once for an exact task, checkpoint,
    tool, and argument digest. Later retries of that exact call may reuse
    the consumed confirmation, but changed arguments cannot.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        handlers: Mapping[str, ToolHandler] | None = None,
        invocation_repository: (ToolInvocationRepository | None) = None,
        authorization_repository: (AuthorizationRepository | None) = None,
        worker_id: str | None = None,
        reservation_duration_sec: float = 30.0,
        heartbeat_interval_sec: float = 10.0,
        metrics_registry: MetricsRegistry | None = None,
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
        self._authorization_repository = authorization_repository
        self._worker_id = worker_id
        self._reservation_duration_sec = reservation_duration_sec
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._metrics_registry = metrics_registry

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
        logging_context: dict[str, object] = {
            "task_id": str(invocation.task_id),
            "checkpoint_id": str(invocation.checkpoint_id),
            "tool_name": invocation.tool_name,
        }

        if self._worker_id is not None:
            logging_context["worker_id"] = self._worker_id

        started_at = perf_counter()

        with bound_contextvars(**logging_context):
            try:
                result = await self._execute_with_context(
                    invocation,
                    granted_permissions=(granted_permissions),
                    confirmation_granted=(confirmation_granted),
                    lease_token=lease_token,
                )
            except asyncio.CancelledError:
                self._record_tool_metric(
                    tool_name=invocation.tool_name,
                    outcome="cancelled",
                    error_code="cancelled",
                    started_at=started_at,
                )
                raise

        self._record_tool_metric(
            tool_name=invocation.tool_name,
            outcome=("success" if result.success else "failure"),
            error_code=(result.error.code if result.error is not None else None),
            started_at=started_at,
        )

        return result

    def _record_tool_metric(
        self,
        *,
        tool_name: str,
        outcome: str,
        error_code: str | None,
        started_at: float,
    ) -> None:
        registry = self._metrics_registry

        if registry is None:
            return

        registry.record_tool_execution(
            tool_name=tool_name,
            outcome=outcome,
            error_code=error_code,
            duration_seconds=max(
                perf_counter() - started_at,
                0.0,
            ),
        )

    async def _execute_with_context(
        self,
        invocation: ToolInvocation,
        *,
        granted_permissions: frozenset[ToolPermission] = frozenset(),
        confirmation_granted: bool = False,
        lease_token: UUID | None = None,
    ) -> ToolExecutionResult:
        try:
            definition = self._registry.get(invocation.tool_name)
        except ToolNotRegisteredError:
            return ToolExecutionResult.failed(
                code="tool_not_registered",
                message=(f"Tool '{invocation.tool_name}' is not registered."),
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

        authorization = await self._authorize(
            definition=definition,
            invocation=invocation,
            fallback_permissions=granted_permissions,
            fallback_confirmation=confirmation_granted,
        )

        if isinstance(
            authorization,
            ToolExecutionResult,
        ):
            return authorization

        effective_permissions, effective_confirmation = authorization

        context = ToolExecutionContext(
            task_id=invocation.task_id,
            checkpoint_id=invocation.checkpoint_id,
            idempotency_key=invocation.idempotency_key,
            granted_permissions=effective_permissions,
            confirmation_granted=effective_confirmation,
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

        with bound_contextvars(
            invocation_id=str(claim.record.invocation_id),
        ):
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

    async def _authorize(
        self,
        *,
        definition: ToolDefinition,
        invocation: ToolInvocation,
        fallback_permissions: frozenset[ToolPermission],
        fallback_confirmation: bool,
    ) -> tuple[frozenset[ToolPermission], bool] | ToolExecutionResult:
        repository = self._authorization_repository

        if repository is None:
            permissions = fallback_permissions
            confirmation = fallback_confirmation
        else:
            try:
                permissions = await repository.get_active_permissions(
                    task_id=invocation.task_id,
                    at=datetime.now(UTC),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return self._authorization_unavailable(error)

            confirmation = False

        missing_permissions = definition.permissions - permissions

        if missing_permissions:
            if repository is not None:
                try:
                    for permission in sorted(
                        missing_permissions,
                        key=lambda item: item.value,
                    ):
                        await repository.record_event(
                            task_id=invocation.task_id,
                            checkpoint_id=(invocation.checkpoint_id),
                            event_type="permission_denied",
                            permission=permission,
                            tool_name=definition.name,
                            arguments_digest=(
                                digest_tool_arguments(invocation.arguments)
                            ),
                            reason=("Required permission is not currently granted."),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    return self._authorization_unavailable(error)

            return ToolExecutionResult.failed(
                code="tool_permission_denied",
                message=(
                    f"Tool '{definition.name}' is missing "
                    "one or more required permissions."
                ),
                retryable=False,
                details={
                    "missing_permissions": sorted(
                        permission.value for permission in missing_permissions
                    )
                },
            )

        if not definition.requires_confirmation:
            return permissions, False

        if repository is None:
            if confirmation:
                return permissions, True

            return ToolExecutionResult.failed(
                code="tool_confirmation_required",
                message=(f"Tool '{definition.name}' requires explicit confirmation."),
                retryable=False,
            )

        arguments_digest = digest_tool_arguments(invocation.arguments)

        try:
            confirmation = await repository.has_consumed_confirmation(
                task_id=invocation.task_id,
                checkpoint_id=(invocation.checkpoint_id),
                tool_name=definition.name,
                arguments_digest=arguments_digest,
            )

            if not confirmation:
                consumed = await repository.consume_confirmation(
                    task_id=invocation.task_id,
                    checkpoint_id=(invocation.checkpoint_id),
                    tool_name=definition.name,
                    arguments_digest=(arguments_digest),
                    at=datetime.now(UTC),
                )
                confirmation = consumed is not None

            if not confirmation:
                await repository.record_event(
                    task_id=invocation.task_id,
                    checkpoint_id=invocation.checkpoint_id,
                    event_type="confirmation_denied",
                    tool_name=definition.name,
                    arguments_digest=arguments_digest,
                    reason=("No active exact-call confirmation grant was found."),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._authorization_unavailable(error)

        if not confirmation:
            return ToolExecutionResult.failed(
                code="tool_confirmation_required",
                message=(
                    f"Tool '{definition.name}' requires "
                    "confirmation for these exact arguments."
                ),
                retryable=False,
                details={"arguments_digest": arguments_digest},
            )

        return permissions, True

    @staticmethod
    def _authorization_unavailable(
        error: Exception,
    ) -> ToolExecutionResult:
        return ToolExecutionResult.failed(
            code="authorization_unavailable",
            message=(
                "Authorization state could not be verified. Tool execution was denied."
            ),
            retryable=True,
            details={"exception_type": type(error).__name__},
        )

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

        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if execution in done:
                return await execution

            execution.cancel()
            await asyncio.gather(
                execution,
                return_exceptions=True,
            )

            # A completed heartbeat task should raise the ownership
            # error that caused execution to be cancelled.
            await heartbeat

            raise ToolInvocationLeaseLostError("Tool invocation ownership was lost.")
        finally:
            # Cancelling runtime.execute() must not leave either child
            # task running in the background. The durable invocation
            # remains executing until its reservation expires, which
            # prevents an immediate duplicate side effect.
            for task in (execution, heartbeat):
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                execution,
                heartbeat,
                return_exceptions=True,
            )

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

            try:
                renewed = await self._invocation_repository.renew(
                    invocation_id=(record.invocation_id),
                    claim_token=record.claim_token,
                    lease_token=lease_token,
                    reservation_duration_sec=(self._reservation_duration_sec),
                )
            except asyncio.CancelledError:
                raise
            except ToolInvocationLeaseLostError:
                raise
            except Exception as error:
                raise ToolInvocationLeaseLostError(
                    "Tool invocation reservation could not be safely renewed."
                ) from error

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

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from friday_brain.contracts.tools import ToolInvocation


class ToolInvocationConflictError(RuntimeError):
    """An idempotency key was reused for different work."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            "Tool invocation idempotency key "
            f"'{idempotency_key}' conflicts with an existing call."
        )


class ToolInvocationLeaseLostError(RuntimeError):
    """The caller does not hold the active task execution lease."""


ToolInvocationStatus = Literal[
    "reserved",
    "executing",
    "succeeded",
    "failed",
]

ToolInvocationClaimOutcome = Literal[
    "acquired",
    "cached_success",
    "cached_failure",
    "busy",
]


@dataclass(frozen=True, slots=True)
class ToolInvocationRecord:
    """Durable state for one idempotent tool invocation."""

    invocation_id: UUID
    idempotency_key: str
    task_id: UUID
    checkpoint_id: UUID
    tool_name: str
    arguments: dict[str, Any]
    status: ToolInvocationStatus
    claim_token: UUID
    lease_token: UUID
    worker_id: str
    execution_attempt: int
    retryable: bool
    reservation_expires_at: datetime
    reserved_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    output: Any | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ToolInvocationClaim:
    """Result of attempting to claim an idempotency key."""

    outcome: ToolInvocationClaimOutcome
    record: ToolInvocationRecord


class ToolInvocationRepository(Protocol):
    """Durable idempotency and audit boundary for tool calls."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def is_healthy(self) -> bool: ...

    async def claim(
        self,
        *,
        invocation: ToolInvocation,
        lease_token: UUID,
        worker_id: str,
        reservation_duration_sec: float,
    ) -> ToolInvocationClaim:
        """
        Claim a new, retryable-failed, or stale invocation.

        Existing successful invocations return cached_success. Active
        reservations return busy.
        """
        ...

    async def mark_executing(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        reservation_duration_sec: float,
    ) -> ToolInvocationRecord | None: ...

    async def renew(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        reservation_duration_sec: float,
    ) -> ToolInvocationRecord | None: ...

    async def complete_success(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        output: Any,
    ) -> ToolInvocationRecord | None: ...

    async def complete_failure(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        error: dict[str, Any],
        retryable: bool,
    ) -> ToolInvocationRecord | None: ...

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> ToolInvocationRecord | None: ...

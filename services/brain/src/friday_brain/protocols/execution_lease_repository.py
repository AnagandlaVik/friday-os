from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    """Exclusive, time-bounded ownership of a task."""

    task_id: UUID
    lease_token: UUID
    worker_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    execution_attempt: int


class ExecutionLeaseRepository(Protocol):
    """Coordinates durable task-processing ownership."""

    async def start(self) -> None:
        """Initialize repository resources."""
        ...

    async def stop(self) -> None:
        """Release repository resources."""
        ...

    async def is_healthy(self) -> bool:
        """Check whether the repository is operational."""
        ...

    async def acquire(
        self,
        task_id: UUID,
        worker_id: str,
        lease_duration_sec: float,
    ) -> ExecutionLease | None:
        """Acquire an absent or expired task lease."""
        ...

    async def renew(
        self,
        task_id: UUID,
        lease_token: UUID,
        worker_id: str,
        lease_duration_sec: float,
    ) -> ExecutionLease | None:
        """Renew an active lease using its fencing token."""
        ...

    async def release(
        self,
        task_id: UUID,
        lease_token: UUID,
        worker_id: str,
    ) -> bool:
        """Release a lease only when token and worker match."""
        ...

    async def discover_recoverable(
        self,
        limit: int,
    ) -> list[UUID]:
        """Find nonterminal tasks without an active lease."""
        ...

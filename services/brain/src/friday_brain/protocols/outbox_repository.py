from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A claimed transactional-outbox message."""

    event_id: UUID
    task_id: UUID
    subject: str
    event_data: dict[str, Any]
    attempt_count: int
    created_at: datetime


class OutboxRepository(Protocol):
    """Persistence operations required by the outbox publisher."""

    async def start(self) -> None:
        """Initialize repository resources."""
        ...

    async def stop(self) -> None:
        """Release repository resources."""
        ...

    async def is_healthy(self) -> bool:
        """Check whether the repository is operational."""
        ...

    async def claim_batch(
        self,
        worker_id: str,
        batch_size: int,
        lock_timeout_sec: float,
    ) -> list[OutboxMessage]:
        """Atomically claim publishable messages."""
        ...

    async def mark_published(
        self,
        event_id: UUID,
        worker_id: str,
    ) -> None:
        """Mark a claimed message as successfully published."""
        ...

    async def mark_failed(
        self,
        event_id: UUID,
        worker_id: str,
        error: str,
        retry_delay_sec: float,
        max_attempts: int,
    ) -> None:
        """Schedule a retry or move the message to dead letter."""
        ...

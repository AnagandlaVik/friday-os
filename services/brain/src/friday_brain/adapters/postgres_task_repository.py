import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from friday_brain.contracts.events import Event
from friday_brain.contracts.tasks import Task

logger = logging.getLogger(__name__)


class PostgresTaskRepository:
    """PostgreSQL-backed authoritative task repository."""

    def __init__(
        self,
        postgres_url: str,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 5.0,
        command_timeout: float = 5.0,
    ) -> None:
        self._postgres_url = postgres_url
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._command_timeout = command_timeout
        self._engine: AsyncEngine | None = None

    async def start(self) -> None:
        """Create the asynchronous SQLAlchemy engine."""
        if self._engine is not None:
            return

        self._engine = create_async_engine(
            self._postgres_url,
            pool_pre_ping=True,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_timeout=self._pool_timeout,
        )

    async def stop(self) -> None:
        """Dispose of the asynchronous SQLAlchemy engine."""
        if self._engine is None:
            return

        await self._engine.dispose()
        self._engine = None

    async def is_healthy(self) -> bool:
        """Perform a bounded live PostgreSQL readiness probe."""
        if self._engine is None:
            return False

        try:
            async with self._engine.connect() as connection:
                await asyncio.wait_for(
                    connection.execute(text("SELECT 1")),
                    timeout=self._command_timeout,
                )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "PostgreSQL health check failed.",
                exc_info=True,
            )
            return False

    async def get(self, task_id: UUID) -> Task | None:
        """Retrieve a task by ID."""
        raise NotImplementedError("Task retrieval is implemented in Milestone 3B.")

    async def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Task | None:
        """Retrieve a task by idempotency key."""
        raise NotImplementedError("Idempotency lookup is implemented in Milestone 3B.")

    async def create_with_event(
        self,
        task: Task,
        event: Event[Any],
    ) -> Task:
        """Atomically create a task and associated event records."""
        raise NotImplementedError(
            "Transactional creation is implemented in Milestone 3B."
        )

    async def update_with_event(
        self,
        task: Task,
        expected_version: int,
        event: Event[Any],
    ) -> Task:
        """Atomically update a task and associated event records."""
        raise NotImplementedError(
            "Transactional updates are implemented in Milestone 3C."
        )

    async def append_event(
        self,
        task_id: UUID,
        expected_version: int,
        event: Event[Any],
    ) -> None:
        """Atomically append an event without changing task state."""
        raise NotImplementedError(
            "Transactional event appending is implemented in Milestone 3C."
        )

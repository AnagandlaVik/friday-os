import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from friday_brain.protocols.outbox_repository import (
    OutboxMessage,
)

logger = logging.getLogger(__name__)


class PostgresOutboxRepository:
    """PostgreSQL transactional-outbox repository."""

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
        if self._engine is not None:
            return

        self._engine = create_async_engine(
            self._postgres_url,
            pool_pre_ping=True,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_timeout=self._pool_timeout,
            connect_args={
                "command_timeout": self._command_timeout,
            },
        )

    async def stop(self) -> None:
        if self._engine is None:
            return

        await self._engine.dispose()
        self._engine = None

    async def is_healthy(self) -> bool:
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
                "PostgreSQL outbox health check failed.",
                exc_info=True,
            )
            return False

    async def claim_batch(
        self,
        worker_id: str,
        batch_size: int,
        lock_timeout_sec: float,
    ) -> list[OutboxMessage]:
        engine = self._require_engine()

        if batch_size <= 0:
            return []

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    WITH candidates AS (
                        SELECT event_id
                        FROM outbox_events
                        WHERE (
                            status = 'pending'
                            AND available_at <= now()
                        )
                        OR (
                            status = 'claimed'
                            AND locked_at IS NOT NULL
                            AND locked_at <= (
                                now()
                                - (
                                    CAST(
                                        :lock_timeout_sec
                                        AS DOUBLE PRECISION
                                    )
                                    * INTERVAL '1 second'
                                )
                            )
                        )
                        ORDER BY created_at, event_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT :batch_size
                    )
                    UPDATE outbox_events AS outbox
                    SET
                        status = 'claimed',
                        attempt_count = outbox.attempt_count + 1,
                        locked_at = now(),
                        locked_by = :worker_id
                    FROM candidates
                    WHERE outbox.event_id = candidates.event_id
                    RETURNING
                        outbox.event_id,
                        outbox.task_id,
                        outbox.subject,
                        outbox.event_data,
                        outbox.attempt_count,
                        outbox.created_at
                    """
                ),
                {
                    "worker_id": worker_id,
                    "batch_size": batch_size,
                    "lock_timeout_sec": lock_timeout_sec,
                },
            )

            rows = result.mappings().all()

        return [self._row_to_message(row) for row in rows]

    async def mark_published(
        self,
        event_id: UUID,
        worker_id: str,
    ) -> None:
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET
                        status = 'published',
                        published_at = now(),
                        locked_at = NULL,
                        locked_by = NULL,
                        last_error = NULL
                    WHERE event_id = :event_id
                      AND status = 'claimed'
                      AND locked_by = :worker_id
                    """
                ),
                {
                    "event_id": event_id,
                    "worker_id": worker_id,
                },
            )

            if result.rowcount != 1:
                raise RuntimeError(
                    f"Outbox event {event_id} is not claimed by worker {worker_id}."
                )

    async def mark_failed(
        self,
        event_id: UUID,
        worker_id: str,
        error: str,
        retry_delay_sec: float,
        max_attempts: int,
    ) -> None:
        engine = self._require_engine()

        retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(0.0, retry_delay_sec)
        )

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET
                        status = CASE
                            WHEN attempt_count >= :max_attempts
                                THEN 'dead_letter'
                            ELSE 'pending'
                        END,
                        available_at = CASE
                            WHEN attempt_count >= :max_attempts
                                THEN available_at
                            ELSE :retry_at
                        END,
                        locked_at = NULL,
                        locked_by = NULL,
                        last_error = :error
                    WHERE event_id = :event_id
                      AND status = 'claimed'
                      AND locked_by = :worker_id
                    """
                ),
                {
                    "event_id": event_id,
                    "worker_id": worker_id,
                    "error": error[:4000],
                    "retry_at": retry_at,
                    "max_attempts": max_attempts,
                },
            )

            if result.rowcount != 1:
                raise RuntimeError(
                    f"Outbox event {event_id} is not claimed by worker {worker_id}."
                )

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("PostgreSQL outbox repository has not been started.")
        return self._engine

    @staticmethod
    def _row_to_message(row: Any) -> OutboxMessage:
        return OutboxMessage(
            event_id=row["event_id"],
            task_id=row["task_id"],
            subject=row["subject"],
            event_data=row["event_data"],
            attempt_count=row["attempt_count"],
            created_at=row["created_at"],
        )

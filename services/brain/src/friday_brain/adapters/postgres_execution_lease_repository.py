import asyncio
import logging
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from friday_brain.protocols.execution_lease_repository import (
    ExecutionLease,
)

logger = logging.getLogger(__name__)


class PostgresExecutionLeaseRepository:
    """PostgreSQL implementation of durable execution leases."""

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
                "Execution lease repository health check failed.",
                exc_info=True,
            )
            return False

    async def acquire(
        self,
        task_id: UUID,
        worker_id: str,
        lease_duration_sec: float,
    ) -> ExecutionLease | None:
        """Atomically acquire an absent or expired lease."""
        engine = self._require_engine()
        self._validate_duration(lease_duration_sec)

        lease_token = uuid4()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO task_execution_leases (
                        task_id,
                        lease_token,
                        worker_id,
                        acquired_at,
                        heartbeat_at,
                        expires_at,
                        execution_attempt
                    )
                    VALUES (
                        :task_id,
                        :lease_token,
                        :worker_id,
                        now(),
                        now(),
                        now() + (
                            CAST(
                                :lease_duration_sec
                                AS DOUBLE PRECISION
                            )
                            * INTERVAL '1 second'
                        ),
                        1
                    )
                    ON CONFLICT (task_id)
                    DO UPDATE SET
                        lease_token = EXCLUDED.lease_token,
                        worker_id = EXCLUDED.worker_id,
                        acquired_at = now(),
                        heartbeat_at = now(),
                        expires_at = now() + (
                            CAST(
                                :lease_duration_sec
                                AS DOUBLE PRECISION
                            )
                            * INTERVAL '1 second'
                        ),
                        execution_attempt = (
                            task_execution_leases.execution_attempt + 1
                        ),
                        updated_at = now()
                    WHERE task_execution_leases.expires_at <= now()
                    RETURNING
                        task_id,
                        lease_token,
                        worker_id,
                        acquired_at,
                        heartbeat_at,
                        expires_at,
                        execution_attempt
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "worker_id": worker_id,
                    "lease_duration_sec": lease_duration_sec,
                },
            )

            row = result.mappings().one_or_none()

        if row is None:
            return None

        return self._row_to_lease(row)

    async def renew(
        self,
        task_id: UUID,
        lease_token: UUID,
        worker_id: str,
        lease_duration_sec: float,
    ) -> ExecutionLease | None:
        """Renew only the current, unexpired fencing token."""
        engine = self._require_engine()
        self._validate_duration(lease_duration_sec)

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE task_execution_leases
                    SET
                        heartbeat_at = now(),
                        expires_at = now() + (
                            CAST(
                                :lease_duration_sec
                                AS DOUBLE PRECISION
                            )
                            * INTERVAL '1 second'
                        ),
                        updated_at = now()
                    WHERE task_id = :task_id
                      AND lease_token = :lease_token
                      AND worker_id = :worker_id
                      AND expires_at > now()
                    RETURNING
                        task_id,
                        lease_token,
                        worker_id,
                        acquired_at,
                        heartbeat_at,
                        expires_at,
                        execution_attempt
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "worker_id": worker_id,
                    "lease_duration_sec": lease_duration_sec,
                },
            )

            row = result.mappings().one_or_none()

        if row is None:
            return None

        return self._row_to_lease(row)

    async def release(
        self,
        task_id: UUID,
        lease_token: UUID,
        worker_id: str,
    ) -> bool:
        """Delete only the lease owned by the exact worker and token."""
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    DELETE FROM task_execution_leases
                    WHERE task_id = :task_id
                      AND lease_token = :lease_token
                      AND worker_id = :worker_id
                    RETURNING task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "worker_id": worker_id,
                },
            )

            released_task_id = result.scalar_one_or_none()

        return released_task_id is not None

    async def discover_recoverable(
        self,
        limit: int,
    ) -> list[UUID]:
        """Find nonterminal tasks whose lease is absent or expired."""
        engine = self._require_engine()

        if limit <= 0:
            return []

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT tasks.id
                    FROM tasks
                    LEFT JOIN task_execution_leases AS leases
                      ON leases.task_id = tasks.id
                    WHERE tasks.state IN (
                        'pending',
                        'planning',
                        'executing',
                        'cancellation_requested'
                    )
                      AND (
                        leases.task_id IS NULL
                        OR leases.expires_at <= now()
                      )
                      AND (
                        tasks.state <> 'executing'
                        OR NOT EXISTS (
                            SELECT 1
                            FROM task_step_checkpoints
                                AS checkpoints
                            WHERE checkpoints.task_id = tasks.id
                              AND checkpoints.status =
                                  'retry_wait'
                              AND checkpoints.retry_available_at
                                  > now()
                        )
                      )
                    ORDER BY
                        tasks.updated_at,
                        tasks.id
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )

            rows = result.scalars().all()

        return list(rows)

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Execution lease repository has not been started.")
        return self._engine

    @staticmethod
    def _validate_duration(lease_duration_sec: float) -> None:
        if lease_duration_sec <= 0:
            raise ValueError("Lease duration must be greater than zero.")

    @staticmethod
    def _row_to_lease(
        row: RowMapping,
    ) -> ExecutionLease:
        return ExecutionLease(
            task_id=row["task_id"],
            lease_token=row["lease_token"],
            worker_id=row["worker_id"],
            acquired_at=row["acquired_at"],
            heartbeat_at=row["heartbeat_at"],
            expires_at=row["expires_at"],
            execution_attempt=row["execution_attempt"],
        )

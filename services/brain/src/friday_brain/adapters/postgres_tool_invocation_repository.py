import asyncio
import json
import logging
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic_core import to_jsonable_python
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from friday_brain.contracts.tools import ToolInvocation
from friday_brain.protocols.tool_invocation_repository import (
    ToolInvocationClaim,
    ToolInvocationConflictError,
    ToolInvocationLeaseLostError,
    ToolInvocationRecord,
    ToolInvocationStatus,
)


logger = logging.getLogger(__name__)


class PostgresToolInvocationRepository:
    """PostgreSQL-backed idempotency and tool-call audit ledger."""

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
                "Tool invocation repository health check failed.",
                exc_info=True,
            )
            return False

    async def claim(
        self,
        *,
        invocation: ToolInvocation,
        lease_token: UUID,
        worker_id: str,
        reservation_duration_sec: float,
    ) -> ToolInvocationClaim:
        engine = self._require_engine()
        self._validate_duration(reservation_duration_sec)

        if not worker_id.strip():
            raise ValueError("Worker ID cannot be empty.")

        invocation_id = uuid4()
        claim_token = uuid4()
        normalized_arguments = to_jsonable_python(invocation.arguments)

        async with engine.begin() as connection:
            execution_attempt = await connection.scalar(
                text(
                    """
                    SELECT leases.execution_attempt
                    FROM task_execution_leases AS leases
                    JOIN task_step_checkpoints AS checkpoints
                      ON checkpoints.checkpoint_id =
                         :checkpoint_id
                     AND checkpoints.task_id = :task_id
                    WHERE leases.task_id = :task_id
                      AND leases.lease_token = :lease_token
                      AND leases.worker_id = :worker_id
                      AND leases.expires_at > now()
                    FOR UPDATE OF leases
                    """
                ),
                {
                    "task_id": invocation.task_id,
                    "checkpoint_id": invocation.checkpoint_id,
                    "lease_token": lease_token,
                    "worker_id": worker_id,
                },
            )

            if execution_attempt is None:
                raise ToolInvocationLeaseLostError(
                    "An active matching execution lease is required."
                )

            inserted_result = await connection.execute(
                text(
                    """
                    INSERT INTO tool_invocations (
                        invocation_id,
                        idempotency_key,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments,
                        status,
                        claim_token,
                        lease_token,
                        worker_id,
                        execution_attempt,
                        retryable,
                        reservation_expires_at
                    )
                    VALUES (
                        :invocation_id,
                        :idempotency_key,
                        :task_id,
                        :checkpoint_id,
                        :tool_name,
                        CAST(:arguments AS JSONB),
                        'reserved',
                        :claim_token,
                        :lease_token,
                        :worker_id,
                        1,
                        false,
                        now() + (
                            CAST(
                                :reservation_duration_sec
                                AS DOUBLE PRECISION
                            )
                            * INTERVAL '1 second'
                        )
                    )
                    ON CONFLICT (idempotency_key)
                    DO NOTHING
                    RETURNING
                        invocation_id,
                        idempotency_key,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments,
                        status,
                        claim_token,
                        lease_token,
                        worker_id,
                        execution_attempt,
                        retryable,
                        reservation_expires_at,
                        reserved_at,
                        started_at,
                        heartbeat_at,
                        completed_at,
                        output,
                        error,
                        created_at,
                        updated_at
                    """
                ),
                {
                    "invocation_id": invocation_id,
                    "idempotency_key": invocation.idempotency_key,
                    "task_id": invocation.task_id,
                    "checkpoint_id": invocation.checkpoint_id,
                    "tool_name": invocation.tool_name,
                    "arguments": self._json(normalized_arguments),
                    "claim_token": claim_token,
                    "lease_token": lease_token,
                    "worker_id": worker_id,
                    "reservation_duration_sec": (reservation_duration_sec),
                },
            )
            inserted = inserted_result.mappings().one_or_none()

            if inserted is not None:
                return ToolInvocationClaim(
                    outcome="acquired",
                    record=self._row_to_record(inserted),
                )

            existing_result = await connection.execute(
                text(
                    """
                    SELECT
                        invocation_id,
                        idempotency_key,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments,
                        status,
                        claim_token,
                        lease_token,
                        worker_id,
                        execution_attempt,
                        retryable,
                        reservation_expires_at,
                        reserved_at,
                        started_at,
                        heartbeat_at,
                        completed_at,
                        output,
                        error,
                        created_at,
                        updated_at
                    FROM tool_invocations
                    WHERE idempotency_key = :idempotency_key
                    FOR UPDATE
                    """
                ),
                {"idempotency_key": (invocation.idempotency_key)},
            )
            existing = existing_result.mappings().one()

            self._validate_identity(
                existing=existing,
                invocation=invocation,
                normalized_arguments=normalized_arguments,
            )

            status = cast(
                ToolInvocationStatus,
                existing["status"],
            )

            if status == "succeeded":
                return ToolInvocationClaim(
                    outcome="cached_success",
                    record=self._row_to_record(existing),
                )

            if status == "failed" and not existing["retryable"]:
                return ToolInvocationClaim(
                    outcome="cached_failure",
                    record=self._row_to_record(existing),
                )

            takeover_result = await connection.execute(
                text(
                    """
                    UPDATE tool_invocations
                    SET
                        status = 'reserved',
                        claim_token = :claim_token,
                        lease_token = :lease_token,
                        worker_id = :worker_id,
                        execution_attempt =
                            execution_attempt + 1,
                        retryable = false,
                        reservation_expires_at = now() + (
                            CAST(
                                :reservation_duration_sec
                                AS DOUBLE PRECISION
                            )
                            * INTERVAL '1 second'
                        ),
                        reserved_at = now(),
                        started_at = NULL,
                        heartbeat_at = NULL,
                        completed_at = NULL,
                        output = NULL,
                        error = NULL,
                        updated_at = now()
                    WHERE invocation_id = :invocation_id
                      AND (
                            (
                                status = 'failed'
                                AND retryable = true
                            )
                            OR (
                                status IN (
                                    'reserved',
                                    'executing'
                                )
                                AND reservation_expires_at
                                    <= now()
                            )
                      )
                    RETURNING
                        invocation_id,
                        idempotency_key,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments,
                        status,
                        claim_token,
                        lease_token,
                        worker_id,
                        execution_attempt,
                        retryable,
                        reservation_expires_at,
                        reserved_at,
                        started_at,
                        heartbeat_at,
                        completed_at,
                        output,
                        error,
                        created_at,
                        updated_at
                    """
                ),
                {
                    "invocation_id": existing["invocation_id"],
                    "claim_token": claim_token,
                    "lease_token": lease_token,
                    "worker_id": worker_id,
                    "reservation_duration_sec": (reservation_duration_sec),
                },
            )
            takeover = takeover_result.mappings().one_or_none()

            if takeover is not None:
                return ToolInvocationClaim(
                    outcome="acquired",
                    record=self._row_to_record(takeover),
                )

            return ToolInvocationClaim(
                outcome="busy",
                record=self._row_to_record(existing),
            )

    async def mark_executing(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        reservation_duration_sec: float,
    ) -> ToolInvocationRecord | None:
        self._validate_duration(reservation_duration_sec)

        return await self._mutate(
            """
            UPDATE tool_invocations AS invocations
            SET
                status = 'executing',
                started_at = now(),
                heartbeat_at = now(),
                reservation_expires_at = now() + (
                    CAST(
                        :reservation_duration_sec
                        AS DOUBLE PRECISION
                    )
                    * INTERVAL '1 second'
                ),
                updated_at = now()
            WHERE invocations.invocation_id = :invocation_id
              AND invocations.claim_token = :claim_token
              AND invocations.lease_token = :lease_token
              AND invocations.status = 'reserved'
              AND invocations.reservation_expires_at > now()
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = invocations.task_id
                      AND leases.lease_token =
                          invocations.lease_token
                      AND leases.worker_id =
                          invocations.worker_id
                      AND leases.expires_at > now()
              )
            RETURNING
                invocation_id,
                idempotency_key,
                task_id,
                checkpoint_id,
                tool_name,
                arguments,
                status,
                claim_token,
                lease_token,
                worker_id,
                execution_attempt,
                retryable,
                reservation_expires_at,
                reserved_at,
                started_at,
                heartbeat_at,
                completed_at,
                output,
                error,
                created_at,
                updated_at
            """,
            {
                "invocation_id": invocation_id,
                "claim_token": claim_token,
                "lease_token": lease_token,
                "reservation_duration_sec": (reservation_duration_sec),
            },
        )

    async def renew(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        reservation_duration_sec: float,
    ) -> ToolInvocationRecord | None:
        self._validate_duration(reservation_duration_sec)

        return await self._mutate(
            """
            UPDATE tool_invocations AS invocations
            SET
                heartbeat_at = now(),
                reservation_expires_at = now() + (
                    CAST(
                        :reservation_duration_sec
                        AS DOUBLE PRECISION
                    )
                    * INTERVAL '1 second'
                ),
                updated_at = now()
            WHERE invocations.invocation_id = :invocation_id
              AND invocations.claim_token = :claim_token
              AND invocations.lease_token = :lease_token
              AND invocations.status IN (
                    'reserved',
                    'executing'
              )
              AND invocations.reservation_expires_at > now()
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = invocations.task_id
                      AND leases.lease_token =
                          invocations.lease_token
                      AND leases.worker_id =
                          invocations.worker_id
                      AND leases.expires_at > now()
              )
            RETURNING
                invocation_id,
                idempotency_key,
                task_id,
                checkpoint_id,
                tool_name,
                arguments,
                status,
                claim_token,
                lease_token,
                worker_id,
                execution_attempt,
                retryable,
                reservation_expires_at,
                reserved_at,
                started_at,
                heartbeat_at,
                completed_at,
                output,
                error,
                created_at,
                updated_at
            """,
            {
                "invocation_id": invocation_id,
                "claim_token": claim_token,
                "lease_token": lease_token,
                "reservation_duration_sec": (reservation_duration_sec),
            },
        )

    async def complete_success(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        output: Any,
    ) -> ToolInvocationRecord | None:
        return await self._complete(
            invocation_id=invocation_id,
            claim_token=claim_token,
            lease_token=lease_token,
            status="succeeded",
            output=output,
            error=None,
            retryable=False,
        )

    async def complete_failure(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        error: dict[str, Any],
        retryable: bool,
    ) -> ToolInvocationRecord | None:
        return await self._complete(
            invocation_id=invocation_id,
            claim_token=claim_token,
            lease_token=lease_token,
            status="failed",
            output=None,
            error=error,
            retryable=retryable,
        )

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> ToolInvocationRecord | None:
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        invocation_id,
                        idempotency_key,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments,
                        status,
                        claim_token,
                        lease_token,
                        worker_id,
                        execution_attempt,
                        retryable,
                        reservation_expires_at,
                        reserved_at,
                        started_at,
                        heartbeat_at,
                        completed_at,
                        output,
                        error,
                        created_at,
                        updated_at
                    FROM tool_invocations
                    WHERE idempotency_key = :idempotency_key
                    """
                ),
                {"idempotency_key": idempotency_key},
            )
            row = result.mappings().one_or_none()

        return self._row_to_record(row) if row is not None else None

    async def _complete(
        self,
        *,
        invocation_id: UUID,
        claim_token: UUID,
        lease_token: UUID,
        status: str,
        output: Any | None,
        error: dict[str, Any] | None,
        retryable: bool,
    ) -> ToolInvocationRecord | None:
        return await self._mutate(
            """
            UPDATE tool_invocations AS invocations
            SET
                status = :status,
                retryable = :retryable,
                output = CAST(:output AS JSONB),
                error = CAST(:error AS JSONB),
                completed_at = now(),
                heartbeat_at = now(),
                updated_at = now()
            WHERE invocations.invocation_id = :invocation_id
              AND invocations.claim_token = :claim_token
              AND invocations.lease_token = :lease_token
              AND invocations.status = 'executing'
              AND invocations.reservation_expires_at > now()
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = invocations.task_id
                      AND leases.lease_token =
                          invocations.lease_token
                      AND leases.worker_id =
                          invocations.worker_id
                      AND leases.expires_at > now()
              )
            RETURNING
                invocation_id,
                idempotency_key,
                task_id,
                checkpoint_id,
                tool_name,
                arguments,
                status,
                claim_token,
                lease_token,
                worker_id,
                execution_attempt,
                retryable,
                reservation_expires_at,
                reserved_at,
                started_at,
                heartbeat_at,
                completed_at,
                output,
                error,
                created_at,
                updated_at
            """,
            {
                "invocation_id": invocation_id,
                "claim_token": claim_token,
                "lease_token": lease_token,
                "status": status,
                "retryable": retryable,
                "output": self._json(output),
                "error": self._json(error),
            },
        )

    async def _mutate(
        self,
        statement: str,
        parameters: dict[str, Any],
    ) -> ToolInvocationRecord | None:
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(statement),
                parameters,
            )
            row = result.mappings().one_or_none()

        return self._row_to_record(row) if row is not None else None

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Tool invocation repository has not been started.")

        return self._engine

    @staticmethod
    def _validate_duration(
        reservation_duration_sec: float,
    ) -> None:
        if reservation_duration_sec <= 0:
            raise ValueError("Reservation duration must be greater than zero.")

    @staticmethod
    def _validate_identity(
        *,
        existing: RowMapping,
        invocation: ToolInvocation,
        normalized_arguments: Any,
    ) -> None:
        matches = (
            existing["task_id"] == invocation.task_id
            and existing["checkpoint_id"] == invocation.checkpoint_id
            and existing["tool_name"] == invocation.tool_name
            and existing["arguments"] == normalized_arguments
        )

        if not matches:
            raise ToolInvocationConflictError(invocation.idempotency_key)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(to_jsonable_python(value))

    @staticmethod
    def _row_to_record(
        row: RowMapping,
    ) -> ToolInvocationRecord:
        return ToolInvocationRecord(
            invocation_id=row["invocation_id"],
            idempotency_key=row["idempotency_key"],
            task_id=row["task_id"],
            checkpoint_id=row["checkpoint_id"],
            tool_name=row["tool_name"],
            arguments=row["arguments"],
            status=cast(
                ToolInvocationStatus,
                row["status"],
            ),
            claim_token=row["claim_token"],
            lease_token=row["lease_token"],
            worker_id=row["worker_id"],
            execution_attempt=row["execution_attempt"],
            retryable=row["retryable"],
            reservation_expires_at=(row["reservation_expires_at"]),
            reserved_at=row["reserved_at"],
            started_at=row["started_at"],
            heartbeat_at=row["heartbeat_at"],
            completed_at=row["completed_at"],
            output=row["output"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

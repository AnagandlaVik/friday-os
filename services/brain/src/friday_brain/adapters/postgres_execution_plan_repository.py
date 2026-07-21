import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from friday_brain.contracts.plans import Plan
from friday_brain.protocols.execution_plan_repository import (
    CheckpointStatus,
    PersistedPlan,
    PlanStatus,
    StepCheckpoint,
)

logger = logging.getLogger(__name__)


class PostgresExecutionPlanRepository:
    """Persists validated plans and lease-fenced checkpoints."""

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
                "Execution plan repository health check failed.",
                exc_info=True,
            )
            return False

    async def save_validated_plan(
        self,
        task_id: UUID,
        task_version: int,
        execution_attempt: int,
        lease_token: UUID,
        plan: Plan,
    ) -> PersistedPlan | None:
        if plan.task_id != task_id:
            raise ValueError("Plan task_id must match the persisted task ID.")

        engine = self._require_engine()

        async with engine.begin() as connection:
            active_attempt = await connection.scalar(
                text(
                    """
                    SELECT leases.execution_attempt
                    FROM task_execution_leases AS leases
                    JOIN tasks
                      ON tasks.id = leases.task_id
                    WHERE leases.task_id = :task_id
                      AND leases.lease_token = :lease_token
                      AND leases.expires_at > now()
                      AND tasks.version = :task_version
                    FOR UPDATE OF leases
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "task_version": task_version,
                },
            )

            if active_attempt != execution_attempt:
                return None

            existing_result = await connection.execute(
                text(
                    """
                    SELECT
                        plan_id,
                        task_id,
                        task_version,
                        execution_attempt,
                        schema_version,
                        status,
                        plan_data,
                        created_at,
                        validated_at,
                        invalidated_at
                    FROM task_plans
                    WHERE task_id = :task_id
                      AND status = 'validated'
                    """
                ),
                {"task_id": task_id},
            )
            existing = existing_result.mappings().one_or_none()

            if existing is not None:
                return self._row_to_plan(existing)

            plan_result = await connection.execute(
                text(
                    """
                    INSERT INTO task_plans (
                        plan_id,
                        task_id,
                        task_version,
                        execution_attempt,
                        schema_version,
                        status,
                        plan_data,
                        validated_at
                    )
                    VALUES (
                        :plan_id,
                        :task_id,
                        :task_version,
                        :execution_attempt,
                        1,
                        'validated',
                        CAST(:plan_data AS JSONB),
                        now()
                    )
                    RETURNING
                        plan_id,
                        task_id,
                        task_version,
                        execution_attempt,
                        schema_version,
                        status,
                        plan_data,
                        created_at,
                        validated_at,
                        invalidated_at
                    """
                ),
                {
                    "plan_id": plan.id,
                    "task_id": task_id,
                    "task_version": task_version,
                    "execution_attempt": execution_attempt,
                    "plan_data": self._json(plan.model_dump(mode="json")),
                },
            )
            plan_row = plan_result.mappings().one()

            for step_index, step in enumerate(plan.steps):
                checkpoint_id = self._checkpoint_id(
                    task_id=task_id,
                    plan_id=plan.id,
                    step_index=step_index,
                )
                idempotency_key = self._idempotency_key(
                    task_id=task_id,
                    plan_id=plan.id,
                    step_index=step_index,
                )

                await connection.execute(
                    text(
                        """
                        INSERT INTO task_step_checkpoints (
                            checkpoint_id,
                            task_id,
                            plan_id,
                            step_index,
                            operation,
                            arguments,
                            status,
                            attempt_count,
                            idempotency_key
                        )
                        VALUES (
                            :checkpoint_id,
                            :task_id,
                            :plan_id,
                            :step_index,
                            :operation,
                            CAST(:arguments AS JSONB),
                            'pending',
                            0,
                            :idempotency_key
                        )
                        """
                    ),
                    {
                        "checkpoint_id": checkpoint_id,
                        "task_id": task_id,
                        "plan_id": plan.id,
                        "step_index": step_index,
                        "operation": step.operation,
                        "arguments": self._json(step.arguments),
                        "idempotency_key": idempotency_key,
                    },
                )

        return self._row_to_plan(plan_row)

    async def get_validated_plan(
        self,
        task_id: UUID,
    ) -> PersistedPlan | None:
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        plan_id,
                        task_id,
                        task_version,
                        execution_attempt,
                        schema_version,
                        status,
                        plan_data,
                        created_at,
                        validated_at,
                        invalidated_at
                    FROM task_plans
                    WHERE task_id = :task_id
                      AND status = 'validated'
                    """
                ),
                {"task_id": task_id},
            )
            row = result.mappings().one_or_none()

        return self._row_to_plan(row) if row is not None else None

    async def list_checkpoints(
        self,
        plan_id: UUID,
    ) -> list[StepCheckpoint]:
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        checkpoint_id,
                        task_id,
                        plan_id,
                        step_index,
                        operation,
                        arguments,
                        status,
                        attempt_count,
                        idempotency_key,
                        output,
                        error,
                        started_at,
                        completed_at,
                        retry_available_at,
                        created_at,
                        updated_at
                    FROM task_step_checkpoints
                    WHERE plan_id = :plan_id
                    ORDER BY step_index
                    """
                ),
                {"plan_id": plan_id},
            )
            rows = result.mappings().all()

        return [self._row_to_checkpoint(row) for row in rows]

    async def start_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
    ) -> StepCheckpoint | None:
        return await self._mutate_checkpoint(
            """
            UPDATE task_step_checkpoints AS checkpoints
            SET
                status = 'executing',
                attempt_count = checkpoints.attempt_count + 1,
                started_at = now(),
                completed_at = NULL,
                retry_available_at = NULL,
                output = NULL,
                error = NULL,
                updated_at = now()
            WHERE checkpoints.checkpoint_id = :checkpoint_id
              AND (
                    checkpoints.status = 'pending'
                    OR (
                        checkpoints.status = 'retry_wait'
                        AND checkpoints.retry_available_at <= now()
                    )
              )
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = checkpoints.task_id
                      AND leases.lease_token = :lease_token
                      AND leases.expires_at > now()
              )
            RETURNING
                checkpoint_id,
                task_id,
                plan_id,
                step_index,
                operation,
                arguments,
                status,
                attempt_count,
                idempotency_key,
                output,
                error,
                started_at,
                completed_at,
                retry_available_at,
                created_at,
                updated_at
            """,
            {
                "checkpoint_id": checkpoint_id,
                "lease_token": lease_token,
            },
        )

    async def resume_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
    ) -> StepCheckpoint | None:
        """
        Reclaim a checkpoint left executing after a worker lost its lease.

        The stable idempotency key is preserved so a real tool adapter can
        deduplicate an uncertain prior attempt.
        """
        return await self._mutate_checkpoint(
            """
            UPDATE task_step_checkpoints AS checkpoints
            SET
                attempt_count = checkpoints.attempt_count + 1,
                started_at = now(),
                completed_at = NULL,
                retry_available_at = NULL,
                output = NULL,
                error = NULL,
                updated_at = now()
            WHERE checkpoints.checkpoint_id = :checkpoint_id
              AND checkpoints.status = 'executing'
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = checkpoints.task_id
                      AND leases.lease_token = :lease_token
                      AND leases.expires_at > now()
              )
            RETURNING
                checkpoint_id,
                task_id,
                plan_id,
                step_index,
                operation,
                arguments,
                status,
                attempt_count,
                idempotency_key,
                output,
                error,
                started_at,
                completed_at,
                retry_available_at,
                created_at,
                updated_at
            """,
            {
                "checkpoint_id": checkpoint_id,
                "lease_token": lease_token,
            },
        )

    async def complete_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        output: Any,
    ) -> StepCheckpoint | None:
        return await self._mutate_checkpoint(
            """
            UPDATE task_step_checkpoints AS checkpoints
            SET
                status = 'completed',
                output = CAST(:output AS JSONB),
                error = NULL,
                completed_at = now(),
                retry_available_at = NULL,
                updated_at = now()
            WHERE checkpoints.checkpoint_id = :checkpoint_id
              AND checkpoints.status = 'executing'
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = checkpoints.task_id
                      AND leases.lease_token = :lease_token
                      AND leases.expires_at > now()
              )
            RETURNING
                checkpoint_id,
                task_id,
                plan_id,
                step_index,
                operation,
                arguments,
                status,
                attempt_count,
                idempotency_key,
                output,
                error,
                started_at,
                completed_at,
                retry_available_at,
                created_at,
                updated_at
            """,
            {
                "checkpoint_id": checkpoint_id,
                "lease_token": lease_token,
                "output": self._json(output),
            },
        )

    async def schedule_checkpoint_retry(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        error: dict[str, Any],
        retry_delay_sec: float,
    ) -> StepCheckpoint | None:
        if retry_delay_sec < 0:
            raise ValueError("Retry delay cannot be negative.")

        retry_available_at = datetime.now(timezone.utc) + timedelta(
            seconds=retry_delay_sec
        )

        return await self._mutate_checkpoint(
            """
            UPDATE task_step_checkpoints AS checkpoints
            SET
                status = 'retry_wait',
                error = CAST(:error AS JSONB),
                retry_available_at = :retry_available_at,
                completed_at = NULL,
                updated_at = now()
            WHERE checkpoints.checkpoint_id = :checkpoint_id
              AND checkpoints.status = 'executing'
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = checkpoints.task_id
                      AND leases.lease_token = :lease_token
                      AND leases.expires_at > now()
              )
            RETURNING
                checkpoint_id,
                task_id,
                plan_id,
                step_index,
                operation,
                arguments,
                status,
                attempt_count,
                idempotency_key,
                output,
                error,
                started_at,
                completed_at,
                retry_available_at,
                created_at,
                updated_at
            """,
            {
                "checkpoint_id": checkpoint_id,
                "lease_token": lease_token,
                "error": self._json(error),
                "retry_available_at": retry_available_at,
            },
        )

    async def fail_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        error: dict[str, Any],
    ) -> StepCheckpoint | None:
        return await self._mutate_checkpoint(
            """
            UPDATE task_step_checkpoints AS checkpoints
            SET
                status = 'failed',
                error = CAST(:error AS JSONB),
                completed_at = now(),
                retry_available_at = NULL,
                updated_at = now()
            WHERE checkpoints.checkpoint_id = :checkpoint_id
              AND checkpoints.status = 'executing'
              AND EXISTS (
                    SELECT 1
                    FROM task_execution_leases AS leases
                    WHERE leases.task_id = checkpoints.task_id
                      AND leases.lease_token = :lease_token
                      AND leases.expires_at > now()
              )
            RETURNING
                checkpoint_id,
                task_id,
                plan_id,
                step_index,
                operation,
                arguments,
                status,
                attempt_count,
                idempotency_key,
                output,
                error,
                started_at,
                completed_at,
                retry_available_at,
                created_at,
                updated_at
            """,
            {
                "checkpoint_id": checkpoint_id,
                "lease_token": lease_token,
                "error": self._json(error),
            },
        )

    async def cancel_incomplete_checkpoints(
        self,
        task_id: UUID,
        lease_token: UUID,
    ) -> int:
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE task_step_checkpoints AS checkpoints
                    SET
                        status = 'cancelled',
                        completed_at = now(),
                        retry_available_at = NULL,
                        updated_at = now()
                    WHERE checkpoints.task_id = :task_id
                      AND checkpoints.status IN (
                            'pending',
                            'executing',
                            'retry_wait'
                      )
                      AND EXISTS (
                            SELECT 1
                            FROM task_execution_leases AS leases
                            WHERE leases.task_id = checkpoints.task_id
                              AND leases.lease_token = :lease_token
                              AND leases.expires_at > now()
                      )
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token": lease_token,
                },
            )

        return result.rowcount

    async def _mutate_checkpoint(
        self,
        sql: str,
        parameters: dict[str, Any],
    ) -> StepCheckpoint | None:
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(sql),
                parameters,
            )
            row = result.mappings().one_or_none()

        return self._row_to_checkpoint(row) if row is not None else None

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Execution plan repository has not been started.")
        return self._engine

    @staticmethod
    def _checkpoint_id(
        task_id: UUID,
        plan_id: UUID,
        step_index: int,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"friday-checkpoint:{task_id}:{plan_id}:{step_index}",
        )

    @staticmethod
    def _idempotency_key(
        task_id: UUID,
        plan_id: UUID,
        step_index: int,
    ) -> str:
        return f"friday:{task_id}:{plan_id}:{step_index}"

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value)

    @staticmethod
    def _row_to_plan(row: RowMapping) -> PersistedPlan:
        return PersistedPlan(
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            task_version=row["task_version"],
            execution_attempt=row["execution_attempt"],
            schema_version=row["schema_version"],
            status=cast(PlanStatus, row["status"]),
            plan=Plan.model_validate(row["plan_data"]),
            created_at=row["created_at"],
            validated_at=row["validated_at"],
            invalidated_at=row["invalidated_at"],
        )

    @staticmethod
    def _row_to_checkpoint(
        row: RowMapping,
    ) -> StepCheckpoint:
        return StepCheckpoint(
            checkpoint_id=row["checkpoint_id"],
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            step_index=row["step_index"],
            operation=row["operation"],
            arguments=row["arguments"],
            status=cast(CheckpointStatus, row["status"]),
            attempt_count=row["attempt_count"],
            idempotency_key=row["idempotency_key"],
            output=row["output"],
            error=row["error"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            retry_available_at=row["retry_available_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

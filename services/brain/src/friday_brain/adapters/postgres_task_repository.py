import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from friday_brain.contracts.errors import (
    IdempotencyConflictError,
    TaskConcurrencyConflictError,
    TaskNotFoundError,
)
from friday_brain.contracts.events import Event
from friday_brain.contracts.tasks import Task, TaskState

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
        subject_prefix: str = "friday.events.brain",
    ) -> None:
        self._postgres_url = postgres_url
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._command_timeout = command_timeout
        self._subject_prefix = subject_prefix.rstrip(".")
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
            connect_args={
                "command_timeout": self._command_timeout,
            },
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
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        id,
                        input,
                        state,
                        client_request_id,
                        idempotency_key,
                        metadata,
                        result,
                        error,
                        version,
                        created_at,
                        updated_at
                    FROM tasks
                    WHERE id = :task_id
                    """
                ),
                {"task_id": task_id},
            )
            row = result.mappings().one_or_none()

        return self._row_to_task(row) if row is not None else None

    async def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Task | None:
        """Retrieve a task by its idempotency key."""
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        id,
                        input,
                        state,
                        client_request_id,
                        idempotency_key,
                        metadata,
                        result,
                        error,
                        version,
                        created_at,
                        updated_at
                    FROM tasks
                    WHERE idempotency_key = :idempotency_key
                    """
                ),
                {"idempotency_key": idempotency_key},
            )
            row = result.mappings().one_or_none()

        return self._row_to_task(row) if row is not None else None

    async def create_with_event(
        self,
        task: Task,
        event: Event[Any],
    ) -> Task:
        """Atomically create a task, task event, and outbox event."""
        engine = self._require_engine()
        self._validate_event_task_id(task.id, event)

        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO tasks (
                            id,
                            input,
                            state,
                            client_request_id,
                            idempotency_key,
                            metadata,
                            result,
                            error,
                            version,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            :id,
                            :input,
                            :state,
                            :client_request_id,
                            :idempotency_key,
                            CAST(:metadata AS JSONB),
                            CAST(:result AS JSONB),
                            CAST(:error AS JSONB),
                            :version,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    {
                        "id": task.id,
                        "input": task.input,
                        "state": self._state_value(task.state),
                        "client_request_id": task.client_request_id,
                        "idempotency_key": task.idempotency_key,
                        "metadata": self._json(task.metadata),
                        "result": self._json_or_none(task.result),
                        "error": self._json_or_none(task.error),
                        "version": task.version,
                        "created_at": task.created_at,
                        "updated_at": task.updated_at,
                    },
                )

                await self._insert_event_and_outbox(
                    connection=connection,
                    task_id=task.id,
                    task_version=task.version,
                    event=event,
                )
        except IntegrityError:
            if task.idempotency_key is None:
                raise

            existing = await self.find_by_idempotency_key(task.idempotency_key)
            if existing is None:
                raise

            if existing.input != task.input:
                raise IdempotencyConflictError(task.idempotency_key) from None

            return existing

        created = await self.get(task.id)
        if created is None:
            raise RuntimeError(
                f"Task {task.id} was committed but could not be reloaded."
            )
        return created

    async def update_with_event(
        self,
        task: Task,
        expected_version: int,
        event: Event[Any],
    ) -> Task:
        """Atomically update a task and append its event and outbox row."""
        engine = self._require_engine()
        self._validate_event_task_id(task.id, event)

        next_version = expected_version + 1

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE tasks
                    SET
                        input = :input,
                        state = :state,
                        client_request_id = :client_request_id,
                        idempotency_key = :idempotency_key,
                        metadata = CAST(:metadata AS JSONB),
                        result = CAST(:result AS JSONB),
                        error = CAST(:error AS JSONB),
                        version = :next_version,
                        updated_at = :updated_at
                    WHERE id = :task_id
                      AND version = :expected_version
                    RETURNING
                        id,
                        input,
                        state,
                        client_request_id,
                        idempotency_key,
                        metadata,
                        result,
                        error,
                        version,
                        created_at,
                        updated_at
                    """
                ),
                {
                    "task_id": task.id,
                    "input": task.input,
                    "state": self._state_value(task.state),
                    "client_request_id": task.client_request_id,
                    "idempotency_key": task.idempotency_key,
                    "metadata": self._json(task.metadata),
                    "result": self._json_or_none(task.result),
                    "error": self._json_or_none(task.error),
                    "next_version": next_version,
                    "expected_version": expected_version,
                    "updated_at": task.updated_at,
                },
            )
            row = result.mappings().one_or_none()

            if row is None:
                raise TaskConcurrencyConflictError(
                    task.id,
                    expected_version,
                )

            persisted_task = self._row_to_task(row)

            await self._insert_event_and_outbox(
                connection=connection,
                task_id=persisted_task.id,
                task_version=persisted_task.version,
                event=event,
            )

        return persisted_task

    async def append_event(
        self,
        task_id: UUID,
        expected_version: int,
        event: Event[Any],
    ) -> None:
        """Atomically append an event without changing task state."""
        engine = self._require_engine()
        self._validate_event_task_id(task_id, event)

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT version
                    FROM tasks
                    WHERE id = :task_id
                    FOR SHARE
                    """
                ),
                {"task_id": task_id},
            )
            current_version = result.scalar_one_or_none()

            if current_version is None:
                raise TaskNotFoundError(task_id)

            if current_version != expected_version:
                raise TaskConcurrencyConflictError(
                    task_id,
                    expected_version,
                )

            await self._insert_event_and_outbox(
                connection=connection,
                task_id=task_id,
                task_version=current_version,
                event=event,
            )

    async def _insert_event_and_outbox(
        self,
        connection: AsyncConnection,
        task_id: UUID,
        task_version: int,
        event: Event[Any],
    ) -> None:
        event_data = event.model_dump(mode="json")

        await connection.execute(
            text(
                """
                INSERT INTO task_events (
                    event_id,
                    task_id,
                    task_version,
                    event_type,
                    schema_version,
                    correlation_id,
                    causation_id,
                    source,
                    occurred_at,
                    payload,
                    metadata
                )
                VALUES (
                    :event_id,
                    :task_id,
                    :task_version,
                    :event_type,
                    :schema_version,
                    :correlation_id,
                    :causation_id,
                    :source,
                    :occurred_at,
                    CAST(:payload AS JSONB),
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "event_id": event.event_id,
                "task_id": task_id,
                "task_version": task_version,
                "event_type": event.event_type,
                "schema_version": event.schema_version,
                "correlation_id": event.correlation_id,
                "causation_id": event.causation_id,
                "source": event.source,
                "occurred_at": event.occurred_at,
                "payload": self._json(event.payload.model_dump(mode="json")),
                "metadata": self._json(event.metadata),
            },
        )

        await connection.execute(
            text(
                """
                INSERT INTO outbox_events (
                    event_id,
                    task_id,
                    subject,
                    event_data
                )
                VALUES (
                    :event_id,
                    :task_id,
                    :subject,
                    CAST(:event_data AS JSONB)
                )
                """
            ),
            {
                "event_id": event.event_id,
                "task_id": task_id,
                "subject": self._event_subject(event.event_type),
                "event_data": self._json(event_data),
            },
        )

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("PostgreSQL repository has not been started.")
        return self._engine

    def _event_subject(self, event_type: str) -> str:
        return f"{self._subject_prefix}.{event_type}"

    @staticmethod
    def _validate_event_task_id(
        task_id: UUID,
        event: Event[Any],
    ) -> None:
        if event.task_id != task_id:
            raise ValueError("Event task_id must match the persisted task ID.")

    @staticmethod
    def _state_value(state: TaskState | str) -> str:
        if isinstance(state, TaskState):
            return state.value
        return state

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value)

    @staticmethod
    def _json_or_none(value: Any | None) -> str | None:
        if value is None:
            return None
        return json.dumps(value)

    @staticmethod
    def _row_to_task(row: Any) -> Task:
        return Task(
            id=row["id"],
            input=row["input"],
            state=row["state"],
            client_request_id=row["client_request_id"],
            idempotency_key=row["idempotency_key"],
            metadata=row["metadata"],
            result=row["result"],
            error=row["error"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

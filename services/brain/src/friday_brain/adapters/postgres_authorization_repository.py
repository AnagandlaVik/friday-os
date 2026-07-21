import asyncio
import json
import logging
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic_core import to_jsonable_python
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from friday_brain.contracts.authorization import (
    AuthorizationEvent,
    AuthorizationEventType,
    TaskPermissionGrant,
    ToolConfirmationGrant,
)
from friday_brain.contracts.tools import ToolPermission


logger = logging.getLogger(__name__)


class PostgresAuthorizationRepository:
    """PostgreSQL-backed authorization grants and audit trail."""

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
                "Authorization repository health check failed.",
                exc_info=True,
            )
            return False

    async def grant_permission(
        self,
        *,
        task_id: UUID,
        permission: ToolPermission,
        granted_by: str,
        expires_at: datetime | None,
    ) -> TaskPermissionGrant:
        self._validate_actor(granted_by)
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO task_permission_grants (
                        grant_id,
                        task_id,
                        permission,
                        granted_by,
                        expires_at
                    )
                    VALUES (
                        :grant_id,
                        :task_id,
                        :permission,
                        :granted_by,
                        :expires_at
                    )
                    ON CONFLICT (task_id, permission)
                    DO UPDATE SET
                        granted_by = EXCLUDED.granted_by,
                        granted_at = now(),
                        expires_at = EXCLUDED.expires_at,
                        revoked_at = NULL,
                        revoke_reason = NULL,
                        updated_at = now()
                    RETURNING
                        grant_id,
                        task_id,
                        permission,
                        granted_by,
                        granted_at,
                        expires_at,
                        revoked_at,
                        revoke_reason,
                        updated_at
                    """
                ),
                {
                    "grant_id": uuid4(),
                    "task_id": task_id,
                    "permission": permission.value,
                    "granted_by": granted_by,
                    "expires_at": expires_at,
                },
            )
            row = result.mappings().one()

            await self._insert_event(
                connection,
                task_id=task_id,
                event_type="permission_granted",
                permission=permission,
                actor_id=granted_by,
                details={
                    "expires_at": (
                        expires_at.isoformat() if expires_at is not None else None
                    )
                },
            )

        return self._row_to_permission_grant(row)

    async def revoke_permission(
        self,
        *,
        task_id: UUID,
        permission: ToolPermission,
        actor_id: str,
        reason: str | None,
    ) -> TaskPermissionGrant | None:
        self._validate_actor(actor_id)
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE task_permission_grants
                    SET
                        revoked_at = now(),
                        revoke_reason = :reason,
                        updated_at = now()
                    WHERE task_id = :task_id
                      AND permission = :permission
                      AND revoked_at IS NULL
                    RETURNING
                        grant_id,
                        task_id,
                        permission,
                        granted_by,
                        granted_at,
                        expires_at,
                        revoked_at,
                        revoke_reason,
                        updated_at
                    """
                ),
                {
                    "task_id": task_id,
                    "permission": permission.value,
                    "reason": reason,
                },
            )
            row = result.mappings().one_or_none()

            if row is not None:
                await self._insert_event(
                    connection,
                    task_id=task_id,
                    event_type="permission_revoked",
                    permission=permission,
                    actor_id=actor_id,
                    reason=reason,
                )

        return self._row_to_permission_grant(row) if row is not None else None

    async def get_active_permissions(
        self,
        *,
        task_id: UUID,
        at: datetime,
    ) -> frozenset[ToolPermission]:
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT permission
                    FROM task_permission_grants
                    WHERE task_id = :task_id
                      AND revoked_at IS NULL
                      AND (
                            expires_at IS NULL
                            OR expires_at > :at
                      )
                    ORDER BY permission
                    """
                ),
                {
                    "task_id": task_id,
                    "at": at,
                },
            )

            permissions = {
                ToolPermission(row["permission"]) for row in result.mappings()
            }

        return frozenset(permissions)

    async def grant_confirmation(
        self,
        *,
        task_id: UUID,
        checkpoint_id: UUID,
        tool_name: str,
        arguments_digest: str,
        granted_by: str,
        expires_at: datetime,
    ) -> ToolConfirmationGrant:
        self._validate_actor(granted_by)
        self._validate_tool_name(tool_name)
        self._validate_digest(arguments_digest)
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO tool_confirmation_grants (
                        grant_id,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments_digest,
                        granted_by,
                        expires_at
                    )
                    VALUES (
                        :grant_id,
                        :task_id,
                        :checkpoint_id,
                        :tool_name,
                        :arguments_digest,
                        :granted_by,
                        :expires_at
                    )
                    ON CONFLICT (
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments_digest
                    )
                    DO UPDATE SET
                        granted_by = EXCLUDED.granted_by,
                        granted_at = now(),
                        expires_at = EXCLUDED.expires_at,
                        consumed_at = NULL,
                        revoked_at = NULL,
                        revoke_reason = NULL,
                        updated_at = now()
                    RETURNING
                        grant_id,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments_digest,
                        granted_by,
                        granted_at,
                        expires_at,
                        consumed_at,
                        revoked_at,
                        revoke_reason,
                        updated_at
                    """
                ),
                {
                    "grant_id": uuid4(),
                    "task_id": task_id,
                    "checkpoint_id": checkpoint_id,
                    "tool_name": tool_name,
                    "arguments_digest": arguments_digest,
                    "granted_by": granted_by,
                    "expires_at": expires_at,
                },
            )
            row = result.mappings().one()

            await self._insert_event(
                connection,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                event_type="confirmation_granted",
                tool_name=tool_name,
                arguments_digest=arguments_digest,
                actor_id=granted_by,
                details={"expires_at": expires_at.isoformat()},
            )

        return self._row_to_confirmation_grant(row)

    async def consume_confirmation(
        self,
        *,
        task_id: UUID,
        checkpoint_id: UUID,
        tool_name: str,
        arguments_digest: str,
        at: datetime,
    ) -> ToolConfirmationGrant | None:
        self._validate_tool_name(tool_name)
        self._validate_digest(arguments_digest)
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE tool_confirmation_grants
                    SET
                        consumed_at = :at,
                        updated_at = now()
                    WHERE task_id = :task_id
                      AND checkpoint_id = :checkpoint_id
                      AND tool_name = :tool_name
                      AND arguments_digest = :arguments_digest
                      AND consumed_at IS NULL
                      AND revoked_at IS NULL
                      AND expires_at > :at
                    RETURNING
                        grant_id,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments_digest,
                        granted_by,
                        granted_at,
                        expires_at,
                        consumed_at,
                        revoked_at,
                        revoke_reason,
                        updated_at
                    """
                ),
                {
                    "task_id": task_id,
                    "checkpoint_id": checkpoint_id,
                    "tool_name": tool_name,
                    "arguments_digest": arguments_digest,
                    "at": at,
                },
            )
            row = result.mappings().one_or_none()

            if row is not None:
                await self._insert_event(
                    connection,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    event_type="confirmation_consumed",
                    tool_name=tool_name,
                    arguments_digest=arguments_digest,
                    actor_id=row["granted_by"],
                )

        return self._row_to_confirmation_grant(row) if row is not None else None

    async def has_consumed_confirmation(
        self,
        *,
        task_id: UUID,
        checkpoint_id: UUID,
        tool_name: str,
        arguments_digest: str,
    ) -> bool:
        self._validate_tool_name(tool_name)
        self._validate_digest(arguments_digest)
        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM tool_confirmation_grants
                        WHERE task_id = :task_id
                          AND checkpoint_id = :checkpoint_id
                          AND tool_name = :tool_name
                          AND arguments_digest =
                              :arguments_digest
                          AND consumed_at IS NOT NULL
                          AND revoked_at IS NULL
                    )
                    """
                ),
                {
                    "task_id": task_id,
                    "checkpoint_id": checkpoint_id,
                    "tool_name": tool_name,
                    "arguments_digest": arguments_digest,
                },
            )

        return bool(result)

    async def revoke_confirmation(
        self,
        *,
        task_id: UUID,
        grant_id: UUID,
        actor_id: str,
        reason: str | None,
    ) -> ToolConfirmationGrant | None:
        self._validate_actor(actor_id)
        engine = self._require_engine()

        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE tool_confirmation_grants
                    SET
                        revoked_at = now(),
                        revoke_reason = :reason,
                        updated_at = now()
                    WHERE grant_id = :grant_id
                      AND task_id = :task_id
                      AND consumed_at IS NULL
                      AND revoked_at IS NULL
                    RETURNING
                        grant_id,
                        task_id,
                        checkpoint_id,
                        tool_name,
                        arguments_digest,
                        granted_by,
                        granted_at,
                        expires_at,
                        consumed_at,
                        revoked_at,
                        revoke_reason,
                        updated_at
                    """
                ),
                {
                    "task_id": task_id,
                    "grant_id": grant_id,
                    "reason": reason,
                },
            )
            row = result.mappings().one_or_none()

            if row is not None:
                await self._insert_event(
                    connection,
                    task_id=row["task_id"],
                    checkpoint_id=row["checkpoint_id"],
                    event_type="confirmation_revoked",
                    tool_name=row["tool_name"],
                    arguments_digest=row["arguments_digest"],
                    actor_id=actor_id,
                    reason=reason,
                )

        return self._row_to_confirmation_grant(row) if row is not None else None

    async def record_event(
        self,
        *,
        task_id: UUID,
        event_type: AuthorizationEventType,
        checkpoint_id: UUID | None = None,
        permission: ToolPermission | None = None,
        tool_name: str | None = None,
        arguments_digest: str | None = None,
        actor_id: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuthorizationEvent:
        engine = self._require_engine()

        if arguments_digest is not None:
            self._validate_digest(arguments_digest)

        async with engine.begin() as connection:
            event = await self._insert_event(
                connection,
                task_id=task_id,
                event_type=event_type,
                checkpoint_id=checkpoint_id,
                permission=permission,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
                actor_id=actor_id,
                reason=reason,
                details=details,
            )

        return event

    async def list_events(
        self,
        *,
        task_id: UUID,
        limit: int = 100,
    ) -> list[AuthorizationEvent]:
        if limit < 1 or limit > 1000:
            raise ValueError("Authorization event limit must be between 1 and 1000.")

        engine = self._require_engine()

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        event_id,
                        task_id,
                        checkpoint_id,
                        event_type,
                        permission,
                        tool_name,
                        arguments_digest,
                        actor_id,
                        reason,
                        details,
                        created_at
                    FROM authorization_events
                    WHERE task_id = :task_id
                    ORDER BY created_at DESC, event_id DESC
                    LIMIT :limit
                    """
                ),
                {
                    "task_id": task_id,
                    "limit": limit,
                },
            )

            rows = result.mappings().all()

        return [self._row_to_event(row) for row in rows]

    async def _insert_event(
        self,
        connection: AsyncConnection,
        *,
        task_id: UUID,
        event_type: AuthorizationEventType,
        checkpoint_id: UUID | None = None,
        permission: ToolPermission | None = None,
        tool_name: str | None = None,
        arguments_digest: str | None = None,
        actor_id: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuthorizationEvent:
        result = await connection.execute(
            text(
                """
                INSERT INTO authorization_events (
                    event_id,
                    task_id,
                    checkpoint_id,
                    event_type,
                    permission,
                    tool_name,
                    arguments_digest,
                    actor_id,
                    reason,
                    details
                )
                VALUES (
                    :event_id,
                    :task_id,
                    :checkpoint_id,
                    :event_type,
                    :permission,
                    :tool_name,
                    :arguments_digest,
                    :actor_id,
                    :reason,
                    CAST(:details AS JSONB)
                )
                RETURNING
                    event_id,
                    task_id,
                    checkpoint_id,
                    event_type,
                    permission,
                    tool_name,
                    arguments_digest,
                    actor_id,
                    reason,
                    details,
                    created_at
                """
            ),
            {
                "event_id": uuid4(),
                "task_id": task_id,
                "checkpoint_id": checkpoint_id,
                "event_type": event_type,
                "permission": (permission.value if permission is not None else None),
                "tool_name": tool_name,
                "arguments_digest": arguments_digest,
                "actor_id": actor_id,
                "reason": reason,
                "details": self._json(details or {}),
            },
        )

        return self._row_to_event(result.mappings().one())

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Authorization repository has not been started.")

        return self._engine

    @staticmethod
    def _validate_actor(actor_id: str) -> None:
        if not actor_id.strip():
            raise ValueError("Actor ID cannot be empty.")

    @staticmethod
    def _validate_tool_name(tool_name: str) -> None:
        if not tool_name.strip():
            raise ValueError("Tool name cannot be empty.")

    @staticmethod
    def _validate_digest(arguments_digest: str) -> None:
        if len(arguments_digest) != 64 or any(
            character not in "0123456789abcdef" for character in arguments_digest
        ):
            raise ValueError("Arguments digest must be a lowercase SHA-256 digest.")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(to_jsonable_python(value))

    @staticmethod
    def _row_to_permission_grant(
        row: RowMapping,
    ) -> TaskPermissionGrant:
        return TaskPermissionGrant(
            grant_id=row["grant_id"],
            task_id=row["task_id"],
            permission=ToolPermission(row["permission"]),
            granted_by=row["granted_by"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_confirmation_grant(
        row: RowMapping,
    ) -> ToolConfirmationGrant:
        return ToolConfirmationGrant(
            grant_id=row["grant_id"],
            task_id=row["task_id"],
            checkpoint_id=row["checkpoint_id"],
            tool_name=row["tool_name"],
            arguments_digest=row["arguments_digest"],
            granted_by=row["granted_by"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_event(
        row: RowMapping,
    ) -> AuthorizationEvent:
        permission_value = row["permission"]

        return AuthorizationEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            checkpoint_id=row["checkpoint_id"],
            event_type=cast(
                AuthorizationEventType,
                row["event_type"],
            ),
            permission=(
                ToolPermission(permission_value)
                if permission_value is not None
                else None
            ),
            tool_name=row["tool_name"],
            arguments_digest=row["arguments_digest"],
            actor_id=row["actor_id"],
            reason=row["reason"],
            details=row["details"],
            created_at=row["created_at"],
        )

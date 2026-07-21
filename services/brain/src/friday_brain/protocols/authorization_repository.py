from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from friday_brain.contracts.authorization import (
    AuthorizationEvent,
    AuthorizationEventType,
    TaskPermissionGrant,
    ToolConfirmationGrant,
)
from friday_brain.contracts.tools import ToolPermission


class AuthorizationRepository(Protocol):
    """Durable task permissions, confirmations, and audit history."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def is_healthy(self) -> bool: ...

    async def grant_permission(
        self,
        *,
        task_id: UUID,
        permission: ToolPermission,
        granted_by: str,
        expires_at: datetime | None,
    ) -> TaskPermissionGrant: ...

    async def revoke_permission(
        self,
        *,
        task_id: UUID,
        permission: ToolPermission,
        actor_id: str,
        reason: str | None,
    ) -> TaskPermissionGrant | None: ...

    async def get_active_permissions(
        self,
        *,
        task_id: UUID,
        at: datetime,
    ) -> frozenset[ToolPermission]: ...

    async def grant_confirmation(
        self,
        *,
        task_id: UUID,
        checkpoint_id: UUID,
        tool_name: str,
        arguments_digest: str,
        granted_by: str,
        expires_at: datetime,
    ) -> ToolConfirmationGrant: ...

    async def consume_confirmation(
        self,
        *,
        task_id: UUID,
        checkpoint_id: UUID,
        tool_name: str,
        arguments_digest: str,
        at: datetime,
    ) -> ToolConfirmationGrant | None: ...

    async def has_consumed_confirmation(
        self,
        *,
        task_id: UUID,
        checkpoint_id: UUID,
        tool_name: str,
        arguments_digest: str,
    ) -> bool: ...

    async def revoke_confirmation(
        self,
        *,
        task_id: UUID,
        grant_id: UUID,
        actor_id: str,
        reason: str | None,
    ) -> ToolConfirmationGrant | None: ...

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
    ) -> AuthorizationEvent: ...

    async def list_events(
        self,
        *,
        task_id: UUID,
        limit: int = 100,
    ) -> list[AuthorizationEvent]: ...

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from friday_brain.contracts.tools import ToolPermission


AuthorizationEventType = Literal[
    "permission_granted",
    "permission_revoked",
    "permission_denied",
    "confirmation_granted",
    "confirmation_consumed",
    "confirmation_revoked",
    "confirmation_denied",
    "confirmation_expired",
]


@dataclass(frozen=True, slots=True)
class TaskPermissionGrant:
    """Current durable permission grant for one task."""

    grant_id: UUID
    task_id: UUID
    permission: ToolPermission
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    updated_at: datetime

    def is_active(
        self,
        *,
        at: datetime,
    ) -> bool:
        if self.revoked_at is not None:
            return False

        return self.expires_at is None or self.expires_at > at


@dataclass(frozen=True, slots=True)
class ToolConfirmationGrant:
    """One-use confirmation bound to an exact tool invocation."""

    grant_id: UUID
    task_id: UUID
    checkpoint_id: UUID
    tool_name: str
    arguments_digest: str
    granted_by: str
    granted_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    updated_at: datetime

    def is_available(
        self,
        *,
        at: datetime,
    ) -> bool:
        return (
            self.consumed_at is None
            and self.revoked_at is None
            and self.expires_at > at
        )


@dataclass(frozen=True, slots=True)
class AuthorizationEvent:
    """Append-only authorization audit event."""

    event_id: UUID
    task_id: UUID
    checkpoint_id: UUID | None
    event_type: AuthorizationEventType
    permission: ToolPermission | None
    tool_name: str | None
    arguments_digest: str | None
    actor_id: str | None
    reason: str | None
    details: dict[str, Any]
    created_at: datetime

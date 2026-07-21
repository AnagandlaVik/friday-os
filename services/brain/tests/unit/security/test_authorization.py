from datetime import UTC, datetime, timedelta
from uuid import uuid4

from friday_brain.contracts.authorization import (
    TaskPermissionGrant,
    ToolConfirmationGrant,
)
from friday_brain.contracts.tools import ToolPermission
from friday_brain.security.authorization import (
    digest_tool_arguments,
)


def test_argument_digest_is_independent_of_key_order() -> None:
    first = digest_tool_arguments(
        {
            "message": "hello",
            "options": {
                "limit": 5,
                "enabled": True,
            },
        }
    )
    second = digest_tool_arguments(
        {
            "options": {
                "enabled": True,
                "limit": 5,
            },
            "message": "hello",
        }
    )

    assert first == second
    assert len(first) == 64


def test_argument_digest_changes_when_arguments_change() -> None:
    approved = digest_tool_arguments({"path": "/allowed/file.txt"})
    changed = digest_tool_arguments({"path": "/different/file.txt"})

    assert approved != changed


def test_permission_grant_respects_expiration_and_revocation() -> None:
    now = datetime.now(UTC)

    active = TaskPermissionGrant(
        grant_id=uuid4(),
        task_id=uuid4(),
        permission=ToolPermission.NETWORK_ACCESS,
        granted_by="user",
        granted_at=now,
        expires_at=now + timedelta(minutes=5),
        revoked_at=None,
        revoke_reason=None,
        updated_at=now,
    )

    assert active.is_active(at=now) is True
    assert active.is_active(at=now + timedelta(minutes=6)) is False

    revoked = TaskPermissionGrant(
        grant_id=active.grant_id,
        task_id=active.task_id,
        permission=active.permission,
        granted_by=active.granted_by,
        granted_at=active.granted_at,
        expires_at=active.expires_at,
        revoked_at=now,
        revoke_reason="User revoked access.",
        updated_at=now,
    )

    assert revoked.is_active(at=now) is False


def test_confirmation_is_one_use_and_expiring() -> None:
    now = datetime.now(UTC)

    grant = ToolConfirmationGrant(
        grant_id=uuid4(),
        task_id=uuid4(),
        checkpoint_id=uuid4(),
        tool_name="filesystem.write",
        arguments_digest=digest_tool_arguments({"path": "/tmp/example"}),
        granted_by="user",
        granted_at=now,
        expires_at=now + timedelta(minutes=2),
        consumed_at=None,
        revoked_at=None,
        revoke_reason=None,
        updated_at=now,
    )

    assert grant.is_available(at=now) is True
    assert grant.is_available(at=now + timedelta(minutes=3)) is False

    consumed = ToolConfirmationGrant(
        grant_id=grant.grant_id,
        task_id=grant.task_id,
        checkpoint_id=grant.checkpoint_id,
        tool_name=grant.tool_name,
        arguments_digest=grant.arguments_digest,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        consumed_at=now,
        revoked_at=None,
        revoke_reason=None,
        updated_at=now,
    )

    assert consumed.is_available(at=now) is False

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse

from friday_brain.api.routes_authorization import (
    grant_task_permission,
    grant_tool_confirmation,
)
from friday_brain.contracts.api import (
    GrantTaskPermissionRequest,
    GrantToolConfirmationRequest,
)
from friday_brain.contracts.authorization import (
    TaskPermissionGrant,
    ToolConfirmationGrant,
)
from friday_brain.contracts.tools import (
    ToolPermission,
)
from friday_brain.security.authorization import (
    digest_tool_arguments,
)


class FakeOrchestrator:
    async def get_task(self, task_id):
        return SimpleNamespace(id=task_id)


class FakeAuthorizationRepository:
    def __init__(self) -> None:
        self.permission_call = None
        self.confirmation_call = None

    async def grant_permission(self, **kwargs):
        self.permission_call = kwargs
        now = datetime.now(UTC)

        return TaskPermissionGrant(
            grant_id=uuid4(),
            task_id=kwargs["task_id"],
            permission=kwargs["permission"],
            granted_by=kwargs["granted_by"],
            granted_at=now,
            expires_at=kwargs["expires_at"],
            revoked_at=None,
            revoke_reason=None,
            updated_at=now,
        )

    async def grant_confirmation(self, **kwargs):
        self.confirmation_call = kwargs
        now = datetime.now(UTC)

        return ToolConfirmationGrant(
            grant_id=uuid4(),
            task_id=kwargs["task_id"],
            checkpoint_id=kwargs["checkpoint_id"],
            tool_name=kwargs["tool_name"],
            arguments_digest=kwargs["arguments_digest"],
            granted_by=kwargs["granted_by"],
            granted_at=now,
            expires_at=kwargs["expires_at"],
            consumed_at=None,
            revoked_at=None,
            revoke_reason=None,
            updated_at=now,
        )


class FakePlanRepository:
    def __init__(self, checkpoint) -> None:
        self.checkpoint = checkpoint

    async def get_checkpoint(self, checkpoint_id):
        if self.checkpoint.checkpoint_id == checkpoint_id:
            return self.checkpoint

        return None


class FakeCompositionRoot:
    def __init__(
        self,
        authorization_repository,
        plan_repository,
    ) -> None:
        self.authorization_repository = authorization_repository
        self.plan_repository = plan_repository
        self.orchestrator = FakeOrchestrator()

    def get_authorization_repository(self):
        return self.authorization_repository

    def get_execution_plan_repository(self):
        return self.plan_repository

    def get_orchestrator(self):
        return self.orchestrator


def make_request(root):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(composition_root=root))
    )


@pytest.mark.asyncio
async def test_permission_grant_uses_actor_header() -> None:
    repository = FakeAuthorizationRepository()
    task_id = uuid4()
    root = FakeCompositionRoot(
        repository,
        plan_repository=None,
    )

    response = await grant_task_permission(
        task_id=task_id,
        request_body=GrantTaskPermissionRequest(
            permission=(ToolPermission.NETWORK_ACCESS),
            expires_in_sec=300,
        ),
        request=make_request(root),
        actor_id="user-one",
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 201
    assert repository.permission_call is not None
    assert repository.permission_call["granted_by"] == "user-one"

    payload = json.loads(response.body)
    assert payload["task_id"] == str(task_id)
    assert payload["permission"] == ToolPermission.NETWORK_ACCESS.value


@pytest.mark.asyncio
async def test_confirmation_digest_uses_persisted_arguments() -> None:
    repository = FakeAuthorizationRepository()
    task_id = uuid4()
    checkpoint_id = uuid4()
    checkpoint = SimpleNamespace(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        operation="filesystem.write",
        arguments={
            "path": "/approved.txt",
            "content": "hello",
        },
    )
    root = FakeCompositionRoot(
        repository,
        FakePlanRepository(checkpoint),
    )

    response = await grant_tool_confirmation(
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        request_body=GrantToolConfirmationRequest(expires_in_sec=300),
        request=make_request(root),
        actor_id="user-one",
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 201
    assert repository.confirmation_call is not None
    assert repository.confirmation_call["arguments_digest"] == digest_tool_arguments(
        checkpoint.arguments
    )
    assert repository.confirmation_call["tool_name"] == checkpoint.operation


@pytest.mark.asyncio
async def test_confirmation_rejects_cross_task_checkpoint() -> None:
    repository = FakeAuthorizationRepository()
    task_id = uuid4()
    checkpoint = SimpleNamespace(
        checkpoint_id=uuid4(),
        task_id=uuid4(),
        operation="filesystem.write",
        arguments={"path": "/tmp/file"},
    )
    root = FakeCompositionRoot(
        repository,
        FakePlanRepository(checkpoint),
    )

    response = await grant_tool_confirmation(
        task_id=task_id,
        checkpoint_id=checkpoint.checkpoint_id,
        request_body=GrantToolConfirmationRequest(),
        request=make_request(root),
        actor_id="user-one",
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
    assert repository.confirmation_call is None

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import (
    APIRouter,
    Header,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse

from friday_brain.composition import CompositionRoot
from friday_brain.contracts.api import (
    AuthorizationEventListResponse,
    AuthorizationEventResponse,
    GrantTaskPermissionRequest,
    GrantToolConfirmationRequest,
    TaskPermissionGrantResponse,
    ToolConfirmationGrantResponse,
)
from friday_brain.contracts.errors import ErrorResponse
from friday_brain.contracts.tools import ToolPermission
from friday_brain.security.authorization import (
    digest_tool_arguments,
)


router = APIRouter(
    prefix="/tasks/{task_id}",
    tags=["Authorization"],
)


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code,
            message=message,
            correlation_id=None,
            retryable=False,
            details=None,
        ).model_dump(mode="json"),
    )


async def _validate_task(
    root: CompositionRoot,
    task_id: UUID,
) -> None:
    # Existing task-not-found handling remains authoritative.
    await root.get_orchestrator().get_task(task_id)


@router.post(
    "/permissions",
    response_model=TaskPermissionGrantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Grant a task permission",
)
async def grant_task_permission(
    task_id: UUID,
    request_body: GrantTaskPermissionRequest,
    request: Request,
    actor_id: str = Header(
        ...,
        alias="X-Actor-ID",
        min_length=1,
        max_length=256,
    ),
) -> JSONResponse:
    root: CompositionRoot = request.app.state.composition_root
    repository = root.get_authorization_repository()

    if repository is None:
        return _error(
            status_code=503,
            code="authorization_unavailable",
            message=("Durable authorization is not configured."),
        )

    await _validate_task(root, task_id)

    expires_at = (
        datetime.now(UTC) + timedelta(seconds=request_body.expires_in_sec)
        if request_body.expires_in_sec is not None
        else None
    )

    grant = await repository.grant_permission(
        task_id=task_id,
        permission=request_body.permission,
        granted_by=actor_id,
        expires_at=expires_at,
    )

    payload = TaskPermissionGrantResponse.model_validate(grant)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload.model_dump(mode="json"),
    )


@router.delete(
    "/permissions/{permission}",
    response_model=TaskPermissionGrantResponse,
    summary="Revoke a task permission",
)
async def revoke_task_permission(
    task_id: UUID,
    permission: ToolPermission,
    request: Request,
    actor_id: str = Header(
        ...,
        alias="X-Actor-ID",
        min_length=1,
        max_length=256,
    ),
    reason: str | None = Query(
        default=None,
        max_length=500,
    ),
) -> JSONResponse:
    root: CompositionRoot = request.app.state.composition_root
    repository = root.get_authorization_repository()

    if repository is None:
        return _error(
            status_code=503,
            code="authorization_unavailable",
            message=("Durable authorization is not configured."),
        )

    await _validate_task(root, task_id)

    grant = await repository.revoke_permission(
        task_id=task_id,
        permission=permission,
        actor_id=actor_id,
        reason=reason,
    )

    if grant is None:
        return _error(
            status_code=404,
            code="permission_grant_not_found",
            message=("No active matching permission grant was found."),
        )

    payload = TaskPermissionGrantResponse.model_validate(grant)

    return JSONResponse(content=payload.model_dump(mode="json"))


@router.post(
    "/checkpoints/{checkpoint_id}/confirmations",
    response_model=ToolConfirmationGrantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm one exact tool call",
)
async def grant_tool_confirmation(
    task_id: UUID,
    checkpoint_id: UUID,
    request_body: GrantToolConfirmationRequest,
    request: Request,
    actor_id: str = Header(
        ...,
        alias="X-Actor-ID",
        min_length=1,
        max_length=256,
    ),
) -> JSONResponse:
    root: CompositionRoot = request.app.state.composition_root
    authorization_repository = root.get_authorization_repository()
    plan_repository = root.get_execution_plan_repository()

    if authorization_repository is None or plan_repository is None:
        return _error(
            status_code=503,
            code="authorization_unavailable",
            message=("Durable authorization or execution plans are not configured."),
        )

    await _validate_task(root, task_id)

    checkpoint = await plan_repository.get_checkpoint(checkpoint_id)

    if checkpoint is None or checkpoint.task_id != task_id:
        return _error(
            status_code=404,
            code="checkpoint_not_found",
            message=("The requested task checkpoint was not found."),
        )

    arguments_digest = digest_tool_arguments(checkpoint.arguments)

    grant = await authorization_repository.grant_confirmation(
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        tool_name=checkpoint.operation,
        arguments_digest=arguments_digest,
        granted_by=actor_id,
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=(request_body.expires_in_sec))
        ),
    )

    payload = ToolConfirmationGrantResponse.model_validate(grant)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload.model_dump(mode="json"),
    )


@router.delete(
    "/confirmations/{grant_id}",
    response_model=ToolConfirmationGrantResponse,
    summary="Revoke an unused confirmation",
)
async def revoke_tool_confirmation(
    task_id: UUID,
    grant_id: UUID,
    request: Request,
    actor_id: str = Header(
        ...,
        alias="X-Actor-ID",
        min_length=1,
        max_length=256,
    ),
    reason: str | None = Query(
        default=None,
        max_length=500,
    ),
) -> JSONResponse:
    root: CompositionRoot = request.app.state.composition_root
    repository = root.get_authorization_repository()

    if repository is None:
        return _error(
            status_code=503,
            code="authorization_unavailable",
            message=("Durable authorization is not configured."),
        )

    await _validate_task(root, task_id)

    grant = await repository.revoke_confirmation(
        task_id=task_id,
        grant_id=grant_id,
        actor_id=actor_id,
        reason=reason,
    )

    if grant is None:
        return _error(
            status_code=404,
            code="confirmation_grant_not_found",
            message=("No active unused confirmation grant was found."),
        )

    payload = ToolConfirmationGrantResponse.model_validate(grant)

    return JSONResponse(content=payload.model_dump(mode="json"))


@router.get(
    "/authorization-events",
    response_model=AuthorizationEventListResponse,
    summary="List task authorization events",
)
async def list_authorization_events(
    task_id: UUID,
    request: Request,
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
) -> JSONResponse:
    root: CompositionRoot = request.app.state.composition_root
    repository = root.get_authorization_repository()

    if repository is None:
        return _error(
            status_code=503,
            code="authorization_unavailable",
            message=("Durable authorization is not configured."),
        )

    await _validate_task(root, task_id)

    events = await repository.list_events(
        task_id=task_id,
        limit=limit,
    )

    payload = AuthorizationEventListResponse(
        events=[AuthorizationEventResponse.model_validate(event) for event in events]
    )

    return JSONResponse(content=payload.model_dump(mode="json"))

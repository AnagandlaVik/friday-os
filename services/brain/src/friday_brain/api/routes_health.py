from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from structlog.stdlib import get_logger

from friday_brain.composition import CompositionRoot
from friday_brain.contracts.api import HealthResponse
from friday_brain.contracts.errors import ErrorResponse


logger = get_logger(__name__)

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
)
async def health_check() -> HealthResponse:
    """
    Check whether the service process can serve HTTP requests.

    This liveness endpoint intentionally does not inspect external
    dependencies.
    """
    return HealthResponse()


@router.get(
    "/ready",
    response_model=HealthResponse,
    tags=["Health"],
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Service Unavailable",
        }
    },
)
async def readiness_check(
    request: Request,
) -> HealthResponse | JSONResponse:
    """
    Check all configured critical dependencies and background workers.
    """
    composition_root: CompositionRoot = request.app.state.composition_root
    unhealthy_components: list[str] = []

    task_repository = composition_root.get_task_repository()
    if not await task_repository.is_healthy():
        unhealthy_components.append("task_repository")

    execution_lease_repository = composition_root.get_execution_lease_repository()
    if (
        execution_lease_repository is not None
        and not await execution_lease_repository.is_healthy()
    ):
        unhealthy_components.append("execution_lease_repository")

    execution_plan_repository = composition_root.get_execution_plan_repository()
    if (
        execution_plan_repository is not None
        and not await execution_plan_repository.is_healthy()
    ):
        unhealthy_components.append("execution_plan_repository")

    tool_invocation_repository = composition_root.get_tool_invocation_repository()
    if (
        tool_invocation_repository is not None
        and not await tool_invocation_repository.is_healthy()
    ):
        unhealthy_components.append("tool_invocation_repository")

    authorization_repository = composition_root.get_authorization_repository()
    if (
        authorization_repository is not None
        and not await authorization_repository.is_healthy()
    ):
        unhealthy_components.append("authorization_repository")

    outbox_repository = composition_root.get_outbox_repository()
    if outbox_repository is not None and not await outbox_repository.is_healthy():
        unhealthy_components.append("outbox_repository")

    outbox_publisher = composition_root.get_outbox_publisher()
    if outbox_publisher is not None and not await outbox_publisher.is_healthy():
        unhealthy_components.append("outbox_publisher")

    recovery_worker = composition_root.get_recovery_worker()
    if recovery_worker is not None and not await recovery_worker.is_healthy():
        unhealthy_components.append("recovery_worker")

    state_store = composition_root.get_state_store()
    if not await state_store.is_healthy():
        unhealthy_components.append("state_store")

    event_bus = composition_root.get_event_bus()
    if not await event_bus.is_healthy():
        unhealthy_components.append("event_bus")

    if not unhealthy_components:
        logger.debug("Readiness check: All dependencies healthy.")
        return HealthResponse()

    correlation_id = getattr(
        request.state,
        "correlation_id",
        None,
    )

    logger.error(
        "Readiness check failed: dependencies unhealthy.",
        unhealthy_components=unhealthy_components,
    )

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ErrorResponse(
            code="service_unavailable",
            message=("One or more critical dependencies are unhealthy."),
            correlation_id=(str(correlation_id) if correlation_id else None),
            retryable=False,
            details={"unhealthy_components": (unhealthy_components)},
        ).model_dump(mode="json"),
    )

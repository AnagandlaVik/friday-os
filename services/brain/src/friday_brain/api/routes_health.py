from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from structlog.stdlib import get_logger

from friday_brain.composition import CompositionRoot
from friday_brain.contracts.api import HealthResponse
from friday_brain.contracts.errors import ErrorResponse

logger = get_logger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check() -> HealthResponse:
    """
    Endpoint to check the service's liveness.
    Returns 200 OK if the application process is running and can serve HTTP requests.
    It does not check backend dependencies.
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
async def readiness_check(request: Request) -> HealthResponse | JSONResponse:
    """
    Endpoint to check the service's readiness, including backend dependencies.
    Returns 200 OK if all configured external services (e.g., Redis, NATS) are reachable and healthy.
    Returns 503 Service Unavailable if any required dependency is unhealthy.
    """
    composition_root: CompositionRoot = request.app.state.composition_root
    all_healthy = True
    unhealthy_components = []

    # Check State Store health
    task_repository = composition_root.get_task_repository()
    if not await task_repository.is_healthy():
        unhealthy_components.append("task_repository")

    state_store = composition_root.get_state_store()
    if not await state_store.is_healthy():
        all_healthy = False
        unhealthy_components.append("state_store")
        logger.warning("Readiness check: State store is unhealthy.")

    # Check Event Bus health
    event_bus = composition_root.get_event_bus()
    if not await event_bus.is_healthy():
        all_healthy = False
        unhealthy_components.append("event_bus")
        logger.warning("Readiness check: Event bus is unhealthy.")

    if all_healthy:
        logger.debug("Readiness check: All dependencies healthy.")
        return HealthResponse()
    else:
        correlation_id = getattr(request.state, "correlation_id", None)
        logger.error(
            "Readiness check failed: Some dependencies are unhealthy.",
            unhealthy_components=unhealthy_components,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ErrorResponse(
                code="service_unavailable",
                message="One or more critical dependencies are unhealthy.",
                correlation_id=str(correlation_id) if correlation_id else None,
                retryable=False,
                details={"unhealthy_components": unhealthy_components},
            ).model_dump(),
        )

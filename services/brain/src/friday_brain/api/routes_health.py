import asyncio
from collections.abc import Awaitable
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from structlog.stdlib import get_logger

from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from friday_brain.contracts.api import (
    ComponentHealthResponse,
    HealthResponse,
    ReadinessResponse,
    VersionResponse,
)
from friday_brain.contracts.errors import (
    ErrorResponse,
)


logger = get_logger(__name__)

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
)
async def health_check() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/version",
    response_model=VersionResponse,
    tags=["Health"],
)
async def version_check() -> VersionResponse:
    return VersionResponse(
        version=settings.service_version,
        build_sha=settings.build_sha,
        environment=settings.environment,
    )


async def _check_component(
    name: str,
    health_check: Awaitable[bool],
    *,
    timeout_sec: float,
) -> tuple[
    str,
    ComponentHealthResponse,
]:
    started_at = perf_counter()

    try:
        healthy = await asyncio.wait_for(
            health_check,
            timeout=timeout_sec,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        healthy = False

    duration_ms = (perf_counter() - started_at) * 1000

    return (
        name,
        ComponentHealthResponse(
            healthy=healthy,
            duration_ms=round(
                duration_ms,
                3,
            ),
        ),
    )


def _configured_checks(
    root: CompositionRoot,
) -> list[tuple[str, Awaitable[bool]]]:
    checks: list[tuple[str, Awaitable[bool]]] = [
        (
            "task_repository",
            root.get_task_repository().is_healthy(),
        ),
        (
            "state_store",
            root.get_state_store().is_healthy(),
        ),
        (
            "event_bus",
            root.get_event_bus().is_healthy(),
        ),
    ]

    optional_components: list[tuple[str, Any]] = [
        (
            "execution_lease_repository",
            root.get_execution_lease_repository(),
        ),
        (
            "execution_plan_repository",
            root.get_execution_plan_repository(),
        ),
        (
            "tool_invocation_repository",
            root.get_tool_invocation_repository(),
        ),
        (
            "authorization_repository",
            root.get_authorization_repository(),
        ),
        (
            "outbox_repository",
            root.get_outbox_repository(),
        ),
        (
            "outbox_publisher",
            root.get_outbox_publisher(),
        ),
        (
            "recovery_worker",
            root.get_recovery_worker(),
        ),
    ]

    for name, component in optional_components:
        if component is not None:
            checks.append(
                (
                    name,
                    component.is_healthy(),
                )
            )

    return checks


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    tags=["Health"],
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
        }
    },
)
async def readiness_check(
    request: Request,
) -> ReadinessResponse | JSONResponse:
    root: CompositionRoot = request.app.state.composition_root

    results = await asyncio.gather(
        *[
            _check_component(
                name,
                check,
                timeout_sec=settings.health_check_timeout_sec,
            )
            for name, check in _configured_checks(root)
        ]
    )

    components = dict(results)
    unhealthy_components = [
        name for name, component in components.items() if not component.healthy
    ]

    if not unhealthy_components:
        return ReadinessResponse(
            version=settings.service_version,
            build_sha=settings.build_sha,
            environment=settings.environment,
            components=components,
        )

    correlation_id = getattr(
        request.state,
        "correlation_id",
        None,
    )

    logger.error(
        "readiness.failed",
        unhealthy_components=(unhealthy_components),
    )

    return JSONResponse(
        status_code=(status.HTTP_503_SERVICE_UNAVAILABLE),
        content=ErrorResponse(
            code="service_unavailable",
            message=("One or more critical dependencies are unhealthy."),
            correlation_id=(str(correlation_id) if correlation_id else None),
            retryable=False,
            details={
                "unhealthy_components": (unhealthy_components),
                "components": {
                    name: component.model_dump(mode="json")
                    for name, component in components.items()
                },
                "version": (settings.service_version),
                "build_sha": settings.build_sha,
                "environment": (settings.environment),
            },
        ).model_dump(mode="json"),
    )

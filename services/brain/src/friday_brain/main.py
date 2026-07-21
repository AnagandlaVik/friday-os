from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from structlog.stdlib import get_logger

from .api import errors, routes_health, routes_tasks
from .api.middleware import CorrelationIdMiddleware
from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from .observability.logging import setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager for startup and shutdown events.
    Initializes and cleans up resources like state stores and event buses.
    """
    composition_root: CompositionRoot = app.state.composition_root

    logger.info("Application starting up...")

    # Start infrastructure adapters
    await composition_root.get_task_repository().start()
    logger.info("Task repository started.")

    execution_lease_repository = composition_root.get_execution_lease_repository()
    if execution_lease_repository is not None:
        await execution_lease_repository.start()
        logger.info("Execution lease repository started.")

    execution_plan_repository = composition_root.get_execution_plan_repository()
    if execution_plan_repository is not None:
        await execution_plan_repository.start()
        logger.info("Execution plan repository started.")
    await composition_root.get_state_store().start()
    logger.info("State store started.")

    await composition_root.get_event_bus().start()
    logger.info("Event bus started.")

    outbox_repository = composition_root.get_outbox_repository()
    if outbox_repository is not None:
        await outbox_repository.start()
        logger.info("Outbox repository started.")

    outbox_publisher = composition_root.get_outbox_publisher()
    if outbox_publisher is not None:
        await outbox_publisher.start()
        logger.info("Outbox publisher started.")

    yield

    logger.info("Application shutting down...")
    # Stop infrastructure adapters
    outbox_publisher = composition_root.get_outbox_publisher()
    if outbox_publisher is not None:
        await outbox_publisher.stop()
        logger.info("Outbox publisher stopped.")

    outbox_repository = composition_root.get_outbox_repository()
    if outbox_repository is not None:
        await outbox_repository.stop()
        logger.info("Outbox repository stopped.")

    await composition_root.get_event_bus().stop()
    logger.info("Event bus stopped.")

    execution_plan_repository = composition_root.get_execution_plan_repository()
    if execution_plan_repository is not None:
        await execution_plan_repository.stop()
        logger.info("Execution plan repository stopped.")

    execution_lease_repository = composition_root.get_execution_lease_repository()
    if execution_lease_repository is not None:
        await execution_lease_repository.stop()
        logger.info("Execution lease repository stopped.")

    await composition_root.get_state_store().stop()
    await composition_root.get_task_repository().stop()
    logger.info("Task repository stopped.")
    logger.info("State store stopped.")


def create_app(composition_root: CompositionRoot | None = None) -> FastAPI:
    """
    Factory function to create the FastAPI application.
    """
    setup_logging()

    if composition_root is None:
        composition_root = CompositionRoot(app_settings=settings)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="FRIDAY Brain Service - Milestone 1",
        lifespan=lifespan,  # Wire lifespan events
    )
    app.state.composition_root = composition_root

    # Middleware
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.cors_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    errors.add_exception_handlers(app)

    # Routers
    app.include_router(routes_health.router)
    app.include_router(routes_tasks.router, prefix="/api/v1")

    return app

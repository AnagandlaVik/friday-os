from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from .api import errors, routes_health, routes_tasks
from .api.middleware import CorrelationIdMiddleware
from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from .observability.logging import setup_logging


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

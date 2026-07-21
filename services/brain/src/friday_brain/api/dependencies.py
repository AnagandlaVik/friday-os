import uuid

from fastapi import Depends, Request

from friday_brain.application.orchestrator import Orchestrator
from friday_brain.composition import CompositionRoot


def get_composition_root(http_request: Request) -> CompositionRoot:
    """
    FastAPI dependency to get the composition root from the app state.
    """
    root: object = http_request.app.state.composition_root

    if not isinstance(root, CompositionRoot):
        raise RuntimeError("Application composition root is not configured")

    return root


def get_orchestrator(
    composition_root: CompositionRoot = Depends(get_composition_root),
) -> Orchestrator:
    """
    FastAPI dependency to get the singleton orchestrator instance.
    """
    return composition_root.get_orchestrator()


def get_correlation_id(http_request: Request) -> uuid.UUID:
    """
    FastAPI dependency to get the correlation ID from the request state.
    """
    return uuid.UUID(http_request.state.correlation_id)

from fastapi import APIRouter

from friday_brain.contracts.api import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check() -> HealthResponse:
    """
    Endpoint to check the service's health.
    """
    return HealthResponse()

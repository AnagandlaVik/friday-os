from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from friday_brain.contracts.errors import (
    BrainError,
    ErrorResponse,
    IdempotencyConflictError,
    InvalidPlanError,
    InvalidStateTransitionError,
    TaskNotFoundError,
    ValidationError,
)


async def brain_error_handler(request: Request, exc: BrainError) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)

    status_code = HTTP_500_INTERNAL_SERVER_ERROR
    if isinstance(exc, TaskNotFoundError):
        status_code = HTTP_404_NOT_FOUND
    elif isinstance(exc, IdempotencyConflictError):
        status_code = HTTP_409_CONFLICT
    elif isinstance(exc, (InvalidPlanError, InvalidStateTransitionError)):
        status_code = HTTP_400_BAD_REQUEST
    elif isinstance(exc, ValidationError):
        status_code = HTTP_422_UNPROCESSABLE_CONTENT

    error_model = ErrorResponse(
        code=exc.code,
        message=exc.message,
        correlation_id=str(correlation_id) if correlation_id else None,
        retryable=exc.retryable,
        details=exc.details,
    )

    return JSONResponse(
        status_code=status_code,
        content=error_model.model_dump(exclude_none=True),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = str(getattr(request.state, "correlation_id", uuid4()))
    error_model = ErrorResponse(
        code="internal_error",
        message="An unexpected internal error occurred.",
        correlation_id=correlation_id,
        retryable=False,
        details=None,
    )
    return JSONResponse(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_model.model_dump(),
    )


def add_exception_handlers(app: FastAPI) -> None:
    """
    Adds custom exception handlers to the FastAPI application.
    """
    app.add_exception_handler(BrainError, brain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_exception_handler)

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from friday_brain_client import (
    BrainClient,
    BrainClientError,
    BrainConnectionError,
    BrainProtocolError,
    BrainResponseError,
    BrainTaskCancelledError,
    BrainTaskFailedError,
    BrainTaskTimeoutError,
)

from friday_assistant.config import Settings
from friday_assistant.models import (
    AssistRequest,
    AssistResponse,
    ErrorResponse,
    HealthResponse,
)
from friday_assistant.service import (
    AssistantService,
)


def create_app(
    *,
    service: AssistantService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    configured_settings = settings or Settings()
    provided_service = service

    @asynccontextmanager
    async def lifespan(
        app: FastAPI,
    ) -> AsyncIterator[None]:
        if provided_service is not None:
            app.state.assistant_service = provided_service
            yield
            return

        brain_client = BrainClient(
            configured_settings.brain_url,
            request_timeout_sec=(configured_settings.brain_request_timeout_sec),
            poll_interval_sec=(configured_settings.brain_poll_interval_sec),
        )

        app.state.assistant_service = AssistantService(
            brain_client,
            task_timeout_sec=(configured_settings.brain_task_timeout_sec),
        )

        try:
            yield
        finally:
            await brain_client.aclose()

    app = FastAPI(
        title="FRIDAY Assistant",
        version="0.1.0",
        lifespan=lifespan,
    )

    if provided_service is not None:
        app.state.assistant_service = provided_service

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["Health"],
    )
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/v1/assist",
        response_model=AssistResponse,
        tags=["Assistant"],
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    async def assist(
        body: AssistRequest,
        request: Request,
    ) -> AssistResponse | JSONResponse:
        assistant_service: AssistantService = request.app.state.assistant_service

        try:
            return await assistant_service.assist(body)
        except BrainTaskTimeoutError as error:
            return _error_response(
                status_code=(status.HTTP_504_GATEWAY_TIMEOUT),
                code="brain_task_timeout",
                message=str(error),
                retryable=True,
                details={
                    "task_id": str(error.task_id),
                    "timeout_sec": (error.timeout_sec),
                },
            )
        except BrainConnectionError as error:
            return _error_response(
                status_code=(status.HTTP_503_SERVICE_UNAVAILABLE),
                code="brain_unavailable",
                message=str(error),
                retryable=True,
            )
        except BrainTaskCancelledError as error:
            return _error_response(
                status_code=status.HTTP_409_CONFLICT,
                code="task_cancelled",
                message=str(error),
                retryable=False,
                details={"task_id": str(error.task.id)},
            )
        except BrainTaskFailedError as error:
            return _error_response(
                status_code=(status.HTTP_422_UNPROCESSABLE_ENTITY),
                code=error.code,
                message=str(error),
                retryable=False,
                details={
                    "task_id": str(error.task.id),
                    "error": error.task.error,
                },
            )
        except BrainResponseError as error:
            return _error_response(
                status_code=(status.HTTP_502_BAD_GATEWAY),
                code=error.code,
                message=error.message,
                retryable=error.retryable,
                details=error.details,
            )
        except BrainProtocolError as error:
            return _error_response(
                status_code=(status.HTTP_502_BAD_GATEWAY),
                code="invalid_brain_response",
                message=str(error),
                retryable=True,
            )
        except BrainClientError as error:
            return _error_response(
                status_code=(status.HTTP_502_BAD_GATEWAY),
                code="brain_client_error",
                message=str(error),
                retryable=True,
            )

    return app


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        code=code,
        message=message,
        retryable=retryable,
        details=details,
    )

    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
    )

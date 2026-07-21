import structlog
from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """
        Adds a correlation ID to the request state and response headers.
        Binds it to the logging context.
        """
        correlation_id_str = request.headers.get("X-Correlation-ID")

        if correlation_id_str:
            correlation_id = correlation_id_str
        else:
            correlation_id = str(uuid4())

        request.state.correlation_id = correlation_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

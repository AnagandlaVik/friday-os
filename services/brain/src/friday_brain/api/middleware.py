from time import perf_counter
from uuid import UUID, uuid4

import structlog
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import Request
from starlette.responses import Response


logger = structlog.get_logger(__name__)

_CORRELATION_HEADER = "X-Correlation-ID"


def _resolve_correlation_id(
    request: Request,
) -> str:
    supplied = request.headers.get(_CORRELATION_HEADER)

    if supplied:
        try:
            return str(UUID(supplied))
        except ValueError:
            pass

    return str(uuid4())


def _route_template(
    request: Request,
) -> str:
    route = request.scope.get("route")
    route_path = getattr(
        route,
        "path",
        None,
    )

    if isinstance(route_path, str):
        return route_path

    # Avoid logging an unmatched raw URL path because it can contain
    # arbitrary user-controlled text.
    return "<unmatched>"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind safe request context and emit one request log."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        correlation_id = _resolve_correlation_id(request)
        request.state.correlation_id = correlation_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
        )

        started_at = perf_counter()

        try:
            response = await call_next(request)
        except Exception as error:
            duration_ms = (perf_counter() - started_at) * 1000

            logger.error(
                "http.request.failed",
                http_method=request.method,
                http_route=_route_template(request),
                http_status_code=500,
                duration_ms=round(
                    duration_ms,
                    3,
                ),
                exception_type=(type(error).__name__),
            )
            raise
        else:
            duration_ms = (perf_counter() - started_at) * 1000
            response.headers[_CORRELATION_HEADER] = correlation_id

            log_fields = {
                "http_method": request.method,
                "http_route": _route_template(request),
                "http_status_code": (response.status_code),
                "duration_ms": round(
                    duration_ms,
                    3,
                ),
            }

            if response.status_code >= 500:
                logger.error(
                    "http.request.completed",
                    **log_fields,
                )
            elif response.status_code >= 400:
                logger.warning(
                    "http.request.completed",
                    **log_fields,
                )
            else:
                logger.info(
                    "http.request.completed",
                    **log_fields,
                )

            try:
                root = request.app.state.composition_root
                root.get_metrics_registry().record_http_request(
                    method=request.method,
                    route=log_fields["http_route"],
                    status_code=response.status_code,
                    duration_seconds=(duration_ms / 1000),
                )
            except Exception as error:
                logger.warning(
                    "metrics.http_record_failed",
                    exception_type=(type(error).__name__),
                )

            return response
        finally:
            structlog.contextvars.clear_contextvars()

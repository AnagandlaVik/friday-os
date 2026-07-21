import json
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from friday_brain.api.middleware import (
    CorrelationIdMiddleware,
)
from friday_brain.observability.logging import (
    setup_logging,
)


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.post("/items/{item_id}")
    async def submit_item(
        item_id: str,
        request: Request,
    ) -> dict[str, str]:
        del request
        return {"item_id": item_id}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.mark.asyncio
async def test_request_log_uses_route_and_correlation_id(
    capsys,
) -> None:
    setup_logging("INFO")
    app = make_app()
    correlation_id = str(uuid4())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/items/123",
            headers={
                "X-Correlation-ID": (correlation_id),
                "Authorization": ("Bearer private-token"),
                "Cookie": "session=private",
                "X-API-Key": "private-api-key",
            },
            content="private prompt and file content",
        )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == correlation_id

    output = capsys.readouterr().out

    for secret in (
        "private-token",
        "session=private",
        "private-api-key",
        "private prompt",
        "file content",
    ):
        assert secret not in output

    records = []

    for line in output.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    request_records = [
        record for record in records if record.get("event") == "http.request.completed"
    ]

    assert len(request_records) == 1

    record = request_records[0]
    assert record["correlation_id"] == correlation_id
    assert record["http_method"] == "POST"
    assert record["http_route"] == "/items/{item_id}"
    assert record["http_status_code"] == 200
    assert record["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_invalid_correlation_id_is_replaced() -> None:
    setup_logging("WARNING")
    app = make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health",
            headers={"X-Correlation-ID": ("not-a-valid-uuid")},
        )

    generated = response.headers["X-Correlation-ID"]

    assert str(UUID(generated)) == generated
    assert generated != "not-a-valid-uuid"


@pytest.mark.asyncio
async def test_request_context_is_cleared() -> None:
    setup_logging("WARNING")
    app = make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert structlog.contextvars.get_contextvars() == {}

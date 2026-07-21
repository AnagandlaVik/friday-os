from typing import AsyncGenerator
import pytest
from httpx import AsyncClient, ASGITransport
import asyncio
import uuid

from friday_brain.main import create_app
from friday_brain.composition import CompositionRoot
from friday_brain.config import settings


@pytest.fixture
def composition_root() -> CompositionRoot:
    return CompositionRoot(app_settings=settings)


@pytest.fixture
async def client(
    composition_root: CompositionRoot,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Test client fixture that creates a new application instance
    for each test function.
    """
    app = create_app(composition_root)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "friday-brain"}


@pytest.mark.asyncio
async def test_full_task_lifecycle(client: AsyncClient):
    create_response = await client.post("/api/v1/tasks", json={"input": "hello world"})
    assert create_response.status_code == 201
    task_data = create_response.json()
    task_id = task_data["id"]
    assert task_data["state"] == "pending"

    # Poll for completion
    for _ in range(10):
        get_response = await client.get(f"/api/v1/tasks/{task_id}")
        if get_response.json()["state"] == "completed":
            break
        await asyncio.sleep(0.1)

    final_task_data = get_response.json()
    assert final_task_data["state"] == "completed"
    assert final_task_data["result"] == "Echo: hello world"


@pytest.mark.asyncio
async def test_task_not_found(client: AsyncClient):
    task_id = uuid.uuid4()
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 404
    error_data = response.json()
    assert error_data["code"] == "task_not_found"


@pytest.mark.asyncio
async def test_idempotency_api_success(client: AsyncClient):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    json_payload = {"input": "idempotent test"}

    response1 = await client.post("/api/v1/tasks", json=json_payload, headers=headers)
    assert response1.status_code == 201
    task_id1 = response1.json()["id"]

    response2 = await client.post("/api/v1/tasks", json=json_payload, headers=headers)
    assert response2.status_code == 200
    task_id2 = response2.json()["id"]

    assert task_id1 == task_id2


@pytest.mark.asyncio
async def test_idempotency_api_conflict(client: AsyncClient):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}

    response1 = await client.post(
        "/api/v1/tasks", json={"input": "one"}, headers=headers
    )
    assert response1.status_code == 201

    response2 = await client.post(
        "/api/v1/tasks", json={"input": "two"}, headers=headers
    )
    assert response2.status_code == 409
    assert response2.json()["code"] == "idempotency_conflict"

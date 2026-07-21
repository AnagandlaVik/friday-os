
import asyncio
import uuid
from typing import AsyncGenerator

import pytest
from httpx import AsyncClient, ASGITransport

from friday_brain.main import create_app
from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from friday_brain.security.tool_policy import ToolPolicy
from .fakes import ControllableToolExecutor


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """
    Test client fixture that creates a new application instance
    for each test function.
    """
    composition_root = CompositionRoot(app_settings=settings)
    app = create_app(composition_root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def controllable_client() -> AsyncGenerator[dict, None]:
    """
    Provides a client and a controllable executor for cancellation tests
    that require pausing task execution.
    """
    composition_root = CompositionRoot(app_settings=settings)
    tool_policy = ToolPolicy(allowed_operations=["echo"])
    controllable_executor = ControllableToolExecutor(tool_policy=tool_policy)
    composition_root._tool_executor = controllable_executor

    app = create_app(composition_root)
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield {"client": c, "executor": controllable_executor}


@pytest.mark.asyncio
async def test_cancel_non_existent_task(client: AsyncClient):
    """
    Ensures that requesting cancellation for a task that does not exist
    returns a 404 Not Found error.
    """
    task_id = uuid.uuid4()
    response = await client.delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


@pytest.mark.asyncio
async def test_cancel_completed_task(client: AsyncClient):
    """
    Ensures that requesting cancellation for a task that has already completed
    is a no-op and returns the task's final state.
    """
    create_response = await client.post("/api/v1/tasks", json={"input": "test"})
    assert create_response.status_code == 202
    task_id = create_response.json()["id"]

    for _ in range(10):
        get_response = await client.get(f"/api/v1/tasks/{task_id}")
        if get_response.json()["state"] == "completed":
            break
        await asyncio.sleep(0.1)

    delete_response = await client.delete(f"/api/v1/tasks/{task_id}")
    assert delete_response.status_code == 200
    
    final_task_data = delete_response.json()
    assert final_task_data["state"] == "completed"


@pytest.mark.asyncio
async def test_repeated_cancellation_requests(controllable_client):
    """
    Tests that sending multiple cancellation requests for the same task
    is handled gracefully while the task is executing.
     """
     client = controllable_client["client"]
     executor = controllable_client["executor"]

     # Start a task and wait for it to begin execution
     create_response_task = asyncio.create_task(
         client.post("/api/v1/tasks", json={"input": "test"})
     )
     print(f'{{"timestamp": {asyncio.get_event_loop().time()}, "task_id": "unknown", "state": "waiting_for_execution_start", "event": "before_wait_execution_started"}}')
     await executor.execution_started.wait()
     print(f'{{"timestamp": {asyncio.get_event_loop().time()}, "task_id": "unknown", "state": "waiting_for_execution_start", "event": "after_wait_execution_started"}}')
     
     # Now that execution is paused, get the task ID
     create_response = await create_response_task
     assert create_response.status_code == 202
     task_id = create_response.json()["id"]

     # Wait until the task is in the EXECUTING state to ensure it's persisted
     for _ in range(10):
         get_response = await client.get(f"/api/v1/tasks/{task_id}")
         if get_response.json()["state"] == "executing":
             break
         await asyncio.sleep(0.01)
     else:
         raise AssertionError("Task never reached executing state")

    # Send multiple cancellation requests
    responses = await asyncio.gather(
        client.delete(f"/api/v1/tasks/{task_id}"),
        client.delete(f"/api/v1/tasks/{task_id}"),
        client.delete(f"/api/v1/tasks/{task_id}"),
    )

    # All requests should be successful
    for response in responses:
        assert response.status_code == 200
        assert response.json()["state"] in ["cancellation_requested", "cancelled"]
    
    # Resume execution and let the task finish
    executor.resume_execution.set()
    await asyncio.sleep(0.1) # allow time for processing
    
    final_response = await client.get(f"/api/v1/tasks/{task_id}")
    assert final_response.json()["state"] == "cancelled"

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from friday_brain_client import (
    BrainClient,
    BrainResponseError,
    BrainTaskFailedError,
    BrainTaskTimeoutError,
    TaskState,
)


def task_payload(
    *,
    task_id: UUID | None = None,
    state: str = "pending",
    result: object | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC).isoformat()

    return {
        "id": str(task_id or uuid4()),
        "input": "Test FRIDAY request",
        "state": state,
        "client_request_id": "voice-request",
        "idempotency_key": "request-key",
        "metadata": {"source": "test"},
        "result": result,
        "error": error,
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_create_task_sends_typed_request() -> None:
    task_id = uuid4()

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/tasks"

        correlation_id = request.headers["X-Correlation-ID"]
        UUID(correlation_id)

        body = json.loads(request.content)

        assert body == {
            "input": "Open my notes",
            "client_request_id": "voice-1",
            "idempotency_key": "key-1",
            "metadata": {"source": "voice"},
        }

        return httpx.Response(
            201,
            json=task_payload(task_id=task_id),
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = BrainClient(
            "http://brain.test/",
            http_client=http_client,
        )

        task = await client.create_task(
            "Open my notes",
            client_request_id="voice-1",
            idempotency_key="key-1",
            metadata={"source": "voice"},
        )

    assert task.id == task_id
    assert task.state == TaskState.PENDING


@pytest.mark.asyncio
async def test_get_and_cancel_task() -> None:
    task_id = uuid4()
    methods: list[str] = []

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        methods.append(request.method)

        state = "cancellation_requested" if request.method == "DELETE" else "executing"

        return httpx.Response(
            200,
            json=task_payload(
                task_id=task_id,
                state=state,
            ),
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = BrainClient(
            "http://brain.test",
            http_client=http_client,
        )

        fetched = await client.get_task(task_id)
        cancelled = await client.cancel_task(task_id)

    assert methods == ["GET", "DELETE"]
    assert fetched.state == TaskState.EXECUTING
    assert cancelled.state == TaskState.CANCELLATION_REQUESTED


@pytest.mark.asyncio
async def test_wait_for_terminal_polls_until_complete() -> None:
    task_id = uuid4()
    states = iter(
        [
            "pending",
            "executing",
            "completed",
        ]
    )

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.method == "GET"

        state = next(states)

        return httpx.Response(
            200,
            json=task_payload(
                task_id=task_id,
                state=state,
                result=({"answer": "done"} if state == "completed" else None),
            ),
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = BrainClient(
            "http://brain.test",
            poll_interval_sec=0.001,
            http_client=http_client,
        )

        task = await client.wait_for_terminal(
            task_id,
            timeout_sec=1,
        )

    assert task.state == TaskState.COMPLETED
    assert task.result == {"answer": "done"}


@pytest.mark.asyncio
async def test_wait_for_terminal_times_out() -> None:
    task_id = uuid4()

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json=task_payload(
                task_id=task_id,
                state="executing",
            ),
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = BrainClient(
            "http://brain.test",
            poll_interval_sec=0.005,
            http_client=http_client,
        )

        with pytest.raises(BrainTaskTimeoutError) as captured:
            await client.wait_for_terminal(
                task_id,
                timeout_sec=0.02,
            )

    assert captured.value.task_id == task_id


@pytest.mark.asyncio
async def test_structured_http_error() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "code": "service_unavailable",
                "message": "Brain is not ready.",
                "retryable": True,
                "details": {"component": "postgres"},
                "correlation_id": "server-correlation",
            },
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = BrainClient(
            "http://brain.test",
            http_client=http_client,
        )

        with pytest.raises(BrainResponseError) as captured:
            await client.create_task("Test")

    error = captured.value

    assert error.status_code == 503
    assert error.code == "service_unavailable"
    assert error.retryable is True
    assert error.correlation_id == "server-correlation"


@pytest.mark.asyncio
async def test_run_task_raises_failed_task() -> None:
    task_id = uuid4()
    calls = 0

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1

        if request.method == "POST":
            return httpx.Response(
                201,
                json=task_payload(
                    task_id=task_id,
                ),
            )

        return httpx.Response(
            200,
            json=task_payload(
                task_id=task_id,
                state="failed",
                error={
                    "code": "planner_failed",
                    "message": "Planning failed.",
                },
            ),
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = BrainClient(
            "http://brain.test",
            http_client=http_client,
        )

        with pytest.raises(BrainTaskFailedError) as captured:
            await client.run_task(
                "Complete this task",
                timeout_sec=1,
            )

    assert calls == 2
    assert captured.value.code == "planner_failed"

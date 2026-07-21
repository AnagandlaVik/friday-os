from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from friday_brain_client import (
    BrainTaskTimeoutError,
    TaskResponse,
    TaskState,
)

from friday_assistant.api import create_app
from friday_assistant.models import (
    AssistRequest,
)
from friday_assistant.service import (
    AssistantService,
)


def completed_task(
    result: Any,
) -> TaskResponse:
    now = datetime.now(UTC)

    return TaskResponse(
        id=uuid4(),
        input="User request",
        state=TaskState.COMPLETED,
        client_request_id="request-1",
        idempotency_key="request-1",
        metadata={"source": "assistant"},
        result=result,
        error=None,
        version=1,
        created_at=now,
        updated_at=now,
    )


class FakeBrainClient:
    def __init__(
        self,
        result: Any,
    ) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def run_task(
        self,
        input_text: str,
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, str] | None = None,
        timeout_sec: float = 60.0,
        poll_interval_sec: float | None = None,
    ) -> TaskResponse:
        self.calls.append(
            {
                "input_text": input_text,
                "client_request_id": (client_request_id),
                "idempotency_key": (idempotency_key),
                "metadata": metadata,
                "timeout_sec": timeout_sec,
                "poll_interval_sec": (poll_interval_sec),
            }
        )

        return completed_task(self.result)


class TimeoutBrainClient(FakeBrainClient):
    async def run_task(
        self,
        input_text: str,
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, str] | None = None,
        timeout_sec: float = 60.0,
        poll_interval_sec: float | None = None,
    ) -> TaskResponse:
        del input_text
        del client_request_id
        del idempotency_key
        del metadata
        del poll_interval_sec

        raise BrainTaskTimeoutError(
            task_id=UUID("00000000-0000-0000-0000-000000000001"),
            timeout_sec=timeout_sec,
        )


@pytest.mark.asyncio
async def test_service_sends_request_to_brain() -> None:
    brain = FakeBrainClient({"answer": "The task is complete."})
    service = AssistantService(
        brain,
        task_timeout_sec=15,
    )

    response = await service.assist(
        AssistRequest(
            text="Do the thing",
            request_id="request-1",
            metadata={"channel": "test"},
        )
    )

    assert response.response == "The task is complete."
    assert response.state == TaskState.COMPLETED

    assert brain.calls == [
        {
            "input_text": "Do the thing",
            "client_request_id": "request-1",
            "idempotency_key": "request-1",
            "metadata": {
                "source": "assistant",
                "channel": "test",
            },
            "timeout_sec": 15,
            "poll_interval_sec": None,
        }
    ]


@pytest.mark.asyncio
async def test_api_returns_assistant_response() -> None:
    service = AssistantService(FakeBrainClient({"response": "Hello from FRIDAY."}))
    app = create_app(service=service)

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://assistant.test",
    ) as client:
        response = await client.post(
            "/v1/assist",
            json={
                "text": "Say hello",
                "request_id": "voice-1",
            },
        )

    assert response.status_code == 200

    payload = response.json()

    assert payload["request_id"] == "voice-1"
    assert payload["response"] == "Hello from FRIDAY."
    assert payload["state"] == "completed"


@pytest.mark.asyncio
async def test_api_maps_task_timeout() -> None:
    service = AssistantService(
        TimeoutBrainClient(None),
        task_timeout_sec=0.5,
    )
    app = create_app(service=service)

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://assistant.test",
    ) as client:
        response = await client.post(
            "/v1/assist",
            json={"text": "Slow task"},
        )

    assert response.status_code == 504

    payload = response.json()

    assert payload["code"] == "brain_task_timeout"
    assert payload["retryable"] is True

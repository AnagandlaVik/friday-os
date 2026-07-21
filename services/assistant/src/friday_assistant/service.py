import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

from friday_brain_client import (
    TaskResponse,
)

from friday_assistant.models import (
    AssistRequest,
    AssistResponse,
)


class BrainTaskRunner(Protocol):
    async def run_task(
        self,
        input_text: str,
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
        timeout_sec: float = 60.0,
        poll_interval_sec: float | None = None,
    ) -> TaskResponse: ...


class AssistantService:
    def __init__(
        self,
        brain_client: BrainTaskRunner,
        *,
        task_timeout_sec: float = 60.0,
    ) -> None:
        if task_timeout_sec <= 0:
            raise ValueError("Task timeout must be positive.")

        self._brain_client = brain_client
        self._task_timeout_sec = task_timeout_sec

    async def assist(
        self,
        request: AssistRequest,
    ) -> AssistResponse:
        request_id = request.request_id or str(uuid4())

        metadata = {
            "source": "assistant",
            **request.metadata,
        }

        task = await self._brain_client.run_task(
            request.text,
            client_request_id=request_id,
            idempotency_key=request_id,
            metadata=metadata,
            timeout_sec=self._task_timeout_sec,
        )

        return AssistResponse(
            request_id=request_id,
            task_id=task.id,
            state=task.state,
            response=self._response_text(task.result),
            result=task.result,
        )

    @staticmethod
    def _response_text(
        result: Any,
    ) -> str:
        if isinstance(result, str):
            cleaned = result.strip()
            return cleaned or "Done."

        if isinstance(result, Mapping):
            for key in (
                "response",
                "answer",
                "message",
                "text",
            ):
                value = result.get(key)

                if isinstance(value, str):
                    cleaned = value.strip()

                    if cleaned:
                        return cleaned

        if result is None:
            return "Done."

        return json.dumps(
            result,
            sort_keys=True,
            default=str,
        )

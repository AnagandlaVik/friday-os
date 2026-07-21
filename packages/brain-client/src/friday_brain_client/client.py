import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from friday_brain_client.errors import (
    BrainConnectionError,
    BrainProtocolError,
    BrainResponseError,
    BrainTaskCancelledError,
    BrainTaskFailedError,
    BrainTaskTimeoutError,
)
from friday_brain_client.models import (
    CreateTaskRequest,
    TaskResponse,
    TaskState,
)


class BrainClient:
    """Typed asynchronous client for the FRIDAY Brain task API."""

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout_sec: float = 10.0,
        poll_interval_sec: float = 0.25,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")

        if not normalized_url:
            raise ValueError("Brain base URL cannot be empty.")

        if request_timeout_sec <= 0:
            raise ValueError("Request timeout must be positive.")

        if poll_interval_sec <= 0:
            raise ValueError("Poll interval must be positive.")

        self._base_url = normalized_url
        self._poll_interval_sec = poll_interval_sec
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=request_timeout_sec
        )

    async def __aenter__(self) -> "BrainClient":
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type
        del exc
        del traceback
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    async def create_task(
        self,
        input_text: str,
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> TaskResponse:
        request = CreateTaskRequest(
            input=input_text,
            client_request_id=client_request_id,
            idempotency_key=idempotency_key,
            metadata=(dict(metadata) if metadata is not None else None),
        )

        return await self._request_task(
            "POST",
            "/tasks",
            json=request.model_dump(
                mode="json",
                exclude_none=True,
            ),
        )

    async def get_task(
        self,
        task_id: UUID,
    ) -> TaskResponse:
        return await self._request_task(
            "GET",
            f"/tasks/{task_id}",
        )

    async def cancel_task(
        self,
        task_id: UUID,
    ) -> TaskResponse:
        return await self._request_task(
            "DELETE",
            f"/tasks/{task_id}",
        )

    async def wait_for_terminal(
        self,
        task_id: UUID,
        *,
        timeout_sec: float = 60.0,
        poll_interval_sec: float | None = None,
    ) -> TaskResponse:
        if timeout_sec <= 0:
            raise ValueError("Task timeout must be positive.")

        interval = (
            poll_interval_sec
            if poll_interval_sec is not None
            else self._poll_interval_sec
        )

        if interval <= 0:
            raise ValueError("Poll interval must be positive.")

        try:
            async with asyncio.timeout(timeout_sec):
                while True:
                    task = await self.get_task(task_id)

                    if task.is_terminal:
                        return task

                    await asyncio.sleep(interval)
        except TimeoutError as error:
            raise BrainTaskTimeoutError(
                task_id=task_id,
                timeout_sec=timeout_sec,
            ) from error

    async def run_task(
        self,
        input_text: str,
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
        timeout_sec: float = 60.0,
        poll_interval_sec: float | None = None,
    ) -> TaskResponse:
        task = await self.create_task(
            input_text,
            client_request_id=client_request_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

        completed = await self.wait_for_terminal(
            task.id,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

        if completed.state == TaskState.FAILED:
            raise BrainTaskFailedError(completed)

        if completed.state == TaskState.CANCELLED:
            raise BrainTaskCancelledError(completed)

        return completed

    async def _request_task(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> TaskResponse:
        correlation_id = str(uuid4())

        try:
            response = await self._http_client.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                headers={
                    "Accept": "application/json",
                    "X-Correlation-ID": correlation_id,
                },
            )
        except (
            httpx.TimeoutException,
            httpx.RequestError,
        ) as error:
            raise BrainConnectionError(
                f"Could not reach the Brain API: {type(error).__name__}."
            ) from error

        if response.is_error:
            self._raise_response_error(
                response,
                fallback_correlation_id=correlation_id,
            )

        try:
            payload = response.json()
            return TaskResponse.model_validate(payload)
        except (
            ValueError,
            ValidationError,
        ) as error:
            raise BrainProtocolError(
                "Brain API returned an invalid task response."
            ) from error

    @staticmethod
    def _raise_response_error(
        response: httpx.Response,
        *,
        fallback_correlation_id: str,
    ) -> None:
        payload: dict[str, Any] = {}

        try:
            candidate = response.json()

            if isinstance(candidate, dict):
                payload = candidate
        except ValueError:
            pass

        raw_details = payload.get("details")
        details = raw_details if isinstance(raw_details, dict) else None

        raw_retryable = payload.get("retryable", False)
        retryable = raw_retryable if isinstance(raw_retryable, bool) else False

        correlation_id = (
            response.headers.get("X-Correlation-ID")
            or payload.get("correlation_id")
            or fallback_correlation_id
        )

        raise BrainResponseError(
            status_code=response.status_code,
            code=str(
                payload.get(
                    "code",
                    "brain_http_error",
                )
            ),
            message=str(
                payload.get(
                    "message",
                    response.reason_phrase or "Brain API request failed.",
                )
            ),
            retryable=retryable,
            details=details,
            correlation_id=str(correlation_id),
        )

import uuid
import time

from fastapi import APIRouter, Depends, status, Header
from fastapi.responses import JSONResponse

from friday_brain.application.orchestrator import Orchestrator
from friday_brain.contracts.api import CreateTaskRequest, TaskResponse

from .dependencies import get_orchestrator, get_correlation_id


router = APIRouter(tags=["Tasks"])


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new task",
)
async def create_task(
    task_request: CreateTaskRequest,
    correlation_id: uuid.UUID = Depends(get_correlation_id),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> JSONResponse:
    """
    Creates a new task and starts its processing.
    """
    task_request.idempotency_key = idempotency_key
    task, created = await orchestrator.create_task(task_request, correlation_id)

    if created:
        await orchestrator.start_task_processing(task)
        headers = {"Location": f"/api/v1/tasks/{task.id}"}
        return JSONResponse(
            content=task.model_dump(mode="json"),
            status_code=status.HTTP_201_CREATED,
            headers=headers,
        )
    else:
        return JSONResponse(
            content=task.model_dump(mode="json"), status_code=status.HTTP_200_OK
        )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Get task status and result",
)
async def get_task(
    task_id: uuid.UUID,
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> JSONResponse:
    """
    Retrieves the current state and result of a task.
    """
    task = await orchestrator.get_task(task_id)
    return JSONResponse(content=task.model_dump(mode="json"))


@router.delete(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Request task cancellation",
)
async def cancel_task(
    task_id: uuid.UUID,
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> JSONResponse:
    """
    Requests the cancellation of a running task.
    """
    print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "unknown", "event": "before_request_cancellation"}}')
    task = await orchestrator.request_cancellation(task_id)
    print(f'{{"timestamp": {__import__("time").time()}, "task_id": "{task_id}", "state": "{task.state}", "event": "after_request_cancellation"}}')
    return JSONResponse(content=task.model_dump(mode="json"))

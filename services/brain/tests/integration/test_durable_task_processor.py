import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.placeholder_planner import (
    PlaceholderPlanner,
)
from friday_brain.adapters.postgres_execution_lease_repository import (
    PostgresExecutionLeaseRepository,
)
from friday_brain.adapters.postgres_execution_plan_repository import (
    PostgresExecutionPlanRepository,
)
from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.application.durable_checkpoint_runner import (
    DurableCheckpointRunner,
)
from friday_brain.application.durable_task_processor import (
    DurableTaskProcessor,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.contracts.plans import PlanStep
from friday_brain.contracts.tasks import Task, TaskState


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://friday:friday@localhost:5432/friday",
)


class RecordingStepExecutor:
    def __init__(
        self,
        *,
        failures: int = 0,
        blocked: bool = False,
    ) -> None:
        self.failures = failures
        self.calls: list[tuple[UUID, str]] = []
        self.execution_started = asyncio.Event()
        self.resume_execution = asyncio.Event()

        if not blocked:
            self.resume_execution.set()

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        checkpoint_id: UUID,
        idempotency_key: str,
    ) -> Any:
        del task

        self.calls.append((step.id, idempotency_key))
        self.execution_started.set()

        await self.resume_execution.wait()

        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("temporary tool failure")

        return f"Echo: {step.arguments.get('message', '')}"


def make_created_event(
    task: Task,
) -> Event[TaskCreatedPayload]:
    return Event(
        event_type="task.created",
        task_id=task.id,
        correlation_id=uuid4(),
        payload=TaskCreatedPayload(
            input=task.input,
            state=task.state,
            client_request_id=task.client_request_id,
            idempotency_key=task.idempotency_key,
        ),
    )


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        POSTGRES_URL,
        pool_pre_ping=True,
    )

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL integration service is unavailable.")

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE
                    task_step_checkpoints,
                    task_plans,
                    task_execution_leases,
                    outbox_events,
                    task_events,
                    tasks
                RESTART IDENTITY CASCADE
                """
            )
        )

    yield engine

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE
                    task_step_checkpoints,
                    task_plans,
                    task_execution_leases,
                    outbox_events,
                    task_events,
                    tasks
                RESTART IDENTITY CASCADE
                """
            )
        )

    await engine.dispose()


@pytest.fixture
async def repositories(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[
    tuple[
        PostgresTaskRepository,
        PostgresExecutionLeaseRepository,
        PostgresExecutionPlanRepository,
    ]
]:
    del postgres_engine

    task_repository = PostgresTaskRepository(POSTGRES_URL)
    lease_repository = PostgresExecutionLeaseRepository(POSTGRES_URL)
    plan_repository = PostgresExecutionPlanRepository(POSTGRES_URL)

    await task_repository.start()
    await lease_repository.start()
    await plan_repository.start()

    yield (
        task_repository,
        lease_repository,
        plan_repository,
    )

    await plan_repository.stop()
    await lease_repository.stop()
    await task_repository.stop()


async def create_task(
    task_repository: PostgresTaskRepository,
    *,
    input_text: str = "durable processor task",
) -> Task:
    task = Task(input=input_text)

    return await task_repository.create_with_event(
        task,
        make_created_event(task),
    )


def make_processor(
    *,
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
    plan_repository: PostgresExecutionPlanRepository,
    executor: RecordingStepExecutor,
    worker_id: str,
    lease_duration_sec: float = 1.0,
    heartbeat_interval_sec: float = 0.2,
    retry_delay_sec: float = 0.05,
) -> DurableTaskProcessor:
    runner = DurableCheckpointRunner(
        plan_repository=plan_repository,
        step_executor=executor,
        max_attempts=3,
        retry_delay_sec=retry_delay_sec,
    )

    return DurableTaskProcessor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        planner=PlaceholderPlanner(),
        plan_validator=DeterministicPlanValidator(
            allowed_operations={"echo"},
            max_steps=10,
        ),
        checkpoint_runner=runner,
        worker_id=worker_id,
        lease_duration_sec=lease_duration_sec,
        heartbeat_interval_sec=heartbeat_interval_sec,
    )


@pytest.mark.asyncio
async def test_processor_completes_and_persists_execution(
    repositories,
    postgres_engine: AsyncEngine,
) -> None:
    (
        task_repository,
        lease_repository,
        plan_repository,
    ) = repositories

    task = await create_task(task_repository)
    executor = RecordingStepExecutor()
    processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=executor,
        worker_id="worker-complete",
    )

    completed = await processor.process_task(task.id)

    assert completed.state == TaskState.COMPLETED
    assert completed.result == "Echo: durable processor task"
    assert len(executor.calls) == 1

    persisted_plan = await plan_repository.get_validated_plan(task.id)

    assert persisted_plan is not None

    checkpoints = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert len(checkpoints) == 1
    assert checkpoints[0].status == "completed"
    assert checkpoints[0].attempt_count == 1
    assert checkpoints[0].output == ("Echo: durable processor task")

    async with postgres_engine.connect() as connection:
        lease_count = await connection.scalar(
            text(
                """
                SELECT count(*)
                FROM task_execution_leases
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task.id},
        )

    assert lease_count == 0


@pytest.mark.asyncio
async def test_processor_resumes_persisted_retry(
    repositories,
) -> None:
    (
        task_repository,
        lease_repository,
        plan_repository,
    ) = repositories

    task = await create_task(task_repository)
    executor = RecordingStepExecutor(failures=1)
    processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=executor,
        worker_id="worker-retry",
        retry_delay_sec=0.05,
    )

    first_result = await processor.process_task(task.id)

    assert first_result.state == TaskState.EXECUTING
    assert len(executor.calls) == 1

    persisted_plan = await plan_repository.get_validated_plan(task.id)
    assert persisted_plan is not None

    first_checkpoints = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert first_checkpoints[0].status == "retry_wait"
    assert first_checkpoints[0].attempt_count == 1

    first_key = first_checkpoints[0].idempotency_key

    await asyncio.sleep(0.08)

    completed = await processor.process_task(task.id)

    assert completed.state == TaskState.COMPLETED
    assert completed.result == "Echo: durable processor task"
    assert len(executor.calls) == 2
    assert executor.calls[0][1] == first_key
    assert executor.calls[1][1] == first_key

    final_checkpoints = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert final_checkpoints[0].status == "completed"
    assert final_checkpoints[0].attempt_count == 2


@pytest.mark.asyncio
async def test_competing_processor_cannot_duplicate_execution(
    repositories,
) -> None:
    (
        task_repository,
        lease_repository,
        plan_repository,
    ) = repositories

    task = await create_task(task_repository)
    executor = RecordingStepExecutor(blocked=True)

    first_processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=executor,
        worker_id="worker-one",
    )
    second_processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=executor,
        worker_id="worker-two",
    )

    first_processing = asyncio.create_task(first_processor.process_task(task.id))

    await asyncio.wait_for(
        executor.execution_started.wait(),
        timeout=2.0,
    )

    second_result = await second_processor.process_task(task.id)

    assert second_result.state == TaskState.EXECUTING
    assert len(executor.calls) == 1

    executor.resume_execution.set()

    completed = await asyncio.wait_for(
        first_processing,
        timeout=2.0,
    )

    assert completed.state == TaskState.COMPLETED
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_heartbeat_prevents_lease_takeover(
    repositories,
) -> None:
    (
        task_repository,
        lease_repository,
        plan_repository,
    ) = repositories

    task = await create_task(task_repository)
    executor = RecordingStepExecutor(blocked=True)

    processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=executor,
        worker_id="heartbeat-owner",
        lease_duration_sec=0.25,
        heartbeat_interval_sec=0.05,
    )

    processing = asyncio.create_task(processor.process_task(task.id))

    await asyncio.wait_for(
        executor.execution_started.wait(),
        timeout=2.0,
    )

    # This exceeds the original lease duration. Heartbeats should keep
    # extending ownership while the step remains blocked.
    await asyncio.sleep(0.35)

    takeover = await lease_repository.acquire(
        task_id=task.id,
        worker_id="takeover-worker",
        lease_duration_sec=1.0,
    )

    assert takeover is None

    executor.resume_execution.set()

    completed = await asyncio.wait_for(
        processing,
        timeout=2.0,
    )

    assert completed.state == TaskState.COMPLETED

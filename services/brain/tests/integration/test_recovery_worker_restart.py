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
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
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
from friday_brain.application.recovery_worker import RecoveryWorker
from friday_brain.contracts.events import Event, TaskCreatedPayload
from friday_brain.contracts.plans import PlanStep
from friday_brain.contracts.tasks import Task, TaskState


POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://friday:friday@localhost:5432/friday",
)


class ControlledStepExecutor:
    def __init__(
        self,
        *,
        failures: int = 0,
        blocked: bool = False,
    ) -> None:
        self.failures = failures
        self.calls: list[tuple[UUID, str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        if not blocked:
            self.release.set()

    async def execute_step(
        self,
        step: PlanStep,
        task: Task,
        checkpoint_id: UUID,
        idempotency_key: str,
        lease_token: UUID | None = None,
    ) -> Any:
        del task

        self.calls.append((step.id, idempotency_key))
        self.started.set()

        await self.release.wait()

        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("temporary recovery test failure")

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
    repository: PostgresTaskRepository,
    input_text: str,
) -> Task:
    task = Task(input=input_text)

    return await repository.create_with_event(
        task,
        make_created_event(task),
    )


def make_processor(
    *,
    task_repository: PostgresTaskRepository,
    lease_repository: PostgresExecutionLeaseRepository,
    plan_repository: PostgresExecutionPlanRepository,
    executor: ControlledStepExecutor,
    worker_id: str,
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
        lease_duration_sec=1.0,
        heartbeat_interval_sec=0.2,
    )


async def wait_for_state(
    repository: PostgresTaskRepository,
    task_id: UUID,
    expected: TaskState,
    *,
    timeout_sec: float = 3.0,
) -> Task:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec

    while loop.time() < deadline:
        task = await repository.get(task_id)

        if task is not None and task.state == expected:
            return task

        await asyncio.sleep(0.02)

    task = await repository.get(task_id)
    actual = task.state if task is not None else None

    raise AssertionError(
        f"Task {task_id} did not reach {expected}; last state was {actual}."
    )


@pytest.mark.asyncio
async def test_worker_recovers_interrupted_checkpoint(
    repositories,
) -> None:
    (
        task_repository,
        lease_repository,
        plan_repository,
    ) = repositories

    task = await create_task(
        task_repository,
        "recover interrupted execution",
    )

    crashed_executor = ControlledStepExecutor(blocked=True)
    crashed_processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=crashed_executor,
        worker_id="crashed-worker",
    )

    crashed_processing = asyncio.create_task(crashed_processor.process_task(task.id))

    await asyncio.wait_for(
        crashed_executor.started.wait(),
        timeout=2.0,
    )

    persisted_plan = await plan_repository.get_validated_plan(task.id)
    assert persisted_plan is not None

    before_restart = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert before_restart[0].status == "executing"
    assert before_restart[0].attempt_count == 1

    stable_key = before_restart[0].idempotency_key

    # Simulate the original process disappearing while the tool call is
    # in progress. The checkpoint remains executing, while the processor
    # releases its lease during local cancellation cleanup.
    crashed_processing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await crashed_processing

    recovered_executor = ControlledStepExecutor()
    recovered_processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=recovered_executor,
        worker_id="restarted-worker",
    )
    recovery_worker = RecoveryWorker(
        lease_repository=lease_repository,
        processor=recovered_processor,
        poll_interval_sec=0.01,
        batch_size=10,
        max_concurrency=1,
    )

    await recovery_worker.start()

    try:
        completed = await wait_for_state(
            task_repository,
            task.id,
            TaskState.COMPLETED,
        )
    finally:
        await recovery_worker.stop()

    assert completed.result == "Echo: recover interrupted execution"
    assert len(recovered_executor.calls) == 1
    assert recovered_executor.calls[0][1] == stable_key

    after_restart = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert after_restart[0].status == "completed"
    assert after_restart[0].attempt_count == 2
    assert after_restart[0].idempotency_key == stable_key


@pytest.mark.asyncio
async def test_worker_automatically_resumes_due_retry(
    repositories,
) -> None:
    (
        task_repository,
        lease_repository,
        plan_repository,
    ) = repositories

    task = await create_task(
        task_repository,
        "recover scheduled retry",
    )

    executor = ControlledStepExecutor(failures=1)
    processor = make_processor(
        task_repository=task_repository,
        lease_repository=lease_repository,
        plan_repository=plan_repository,
        executor=executor,
        worker_id="retry-worker",
        retry_delay_sec=0.08,
    )

    first_pass = await processor.process_task(task.id)

    assert first_pass.state == TaskState.EXECUTING
    assert len(executor.calls) == 1

    persisted_plan = await plan_repository.get_validated_plan(task.id)
    assert persisted_plan is not None

    waiting = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert waiting[0].status == "retry_wait"
    assert waiting[0].attempt_count == 1

    stable_key = waiting[0].idempotency_key

    recovery_worker = RecoveryWorker(
        lease_repository=lease_repository,
        processor=processor,
        poll_interval_sec=0.01,
        batch_size=10,
        max_concurrency=1,
    )

    await recovery_worker.start()

    try:
        completed = await wait_for_state(
            task_repository,
            task.id,
            TaskState.COMPLETED,
        )
    finally:
        await recovery_worker.stop()

    assert completed.result == "Echo: recover scheduled retry"
    assert len(executor.calls) == 2
    assert executor.calls[0][1] == stable_key
    assert executor.calls[1][1] == stable_key

    final_checkpoints = await plan_repository.list_checkpoints(persisted_plan.plan_id)

    assert final_checkpoints[0].status == "completed"
    assert final_checkpoints[0].attempt_count == 2

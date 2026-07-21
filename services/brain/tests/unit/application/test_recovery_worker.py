import asyncio
from uuid import UUID, uuid4

import pytest

from friday_brain.application.recovery_worker import (
    RecoveryWorker,
)


class FakeLeaseRepository:
    def __init__(
        self,
        task_ids: list[UUID],
    ) -> None:
        self.task_ids = task_ids
        self.discovery_calls = 0
        self.requested_limits: list[int] = []

    async def discover_recoverable(
        self,
        limit: int,
    ) -> list[UUID]:
        self.discovery_calls += 1
        self.requested_limits.append(limit)
        return self.task_ids[:limit]


class BlockingProcessor:
    def __init__(self) -> None:
        self.started: list[UUID] = []
        self.release = asyncio.Event()

    async def process_task(
        self,
        task_id: UUID,
    ) -> object:
        self.started.append(task_id)
        await self.release.wait()
        return object()


class CompletingProcessor:
    def __init__(self) -> None:
        self.started: list[UUID] = []

    async def process_task(
        self,
        task_id: UUID,
    ) -> object:
        self.started.append(task_id)
        return object()


class FailingProcessor:
    def __init__(self) -> None:
        self.calls = 0

    async def process_task(
        self,
        task_id: UUID,
    ) -> object:
        del task_id
        self.calls += 1
        raise RuntimeError("processor failed")


@pytest.mark.asyncio
async def test_scan_respects_concurrency_limit() -> None:
    task_ids = [
        uuid4(),
        uuid4(),
        uuid4(),
    ]
    repository = FakeLeaseRepository(task_ids)
    processor = BlockingProcessor()
    worker = RecoveryWorker(
        lease_repository=repository,
        processor=processor,
        max_concurrency=2,
        batch_size=10,
    )

    scheduled = await worker.scan_once()
    await asyncio.sleep(0)

    assert scheduled == 2
    assert processor.started == task_ids[:2]
    assert repository.requested_limits == [2]

    second_scan = await worker.scan_once()

    assert second_scan == 0
    assert processor.started == task_ids[:2]

    processor.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_worker_does_not_schedule_duplicate_ids() -> None:
    task_id = uuid4()
    repository = FakeLeaseRepository([task_id, task_id])
    processor = BlockingProcessor()
    worker = RecoveryWorker(
        lease_repository=repository,
        processor=processor,
        max_concurrency=4,
        batch_size=10,
    )

    scheduled = await worker.scan_once()
    await asyncio.sleep(0)

    assert scheduled == 1
    assert processor.started == [task_id]

    processor.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_completed_task_can_be_discovered_again() -> None:
    task_id = uuid4()
    repository = FakeLeaseRepository([task_id])
    processor = CompletingProcessor()
    worker = RecoveryWorker(
        lease_repository=repository,
        processor=processor,
        max_concurrency=1,
    )

    assert await worker.scan_once() == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert await worker.scan_once() == 1
    await asyncio.sleep(0)

    assert processor.started == [
        task_id,
        task_id,
    ]


@pytest.mark.asyncio
async def test_processing_failure_does_not_kill_worker() -> None:
    task_id = uuid4()
    repository = FakeLeaseRepository([task_id])
    processor = FailingProcessor()
    worker = RecoveryWorker(
        lease_repository=repository,
        processor=processor,
        poll_interval_sec=0.01,
        max_concurrency=1,
    )

    await worker.start()
    await asyncio.sleep(0.04)

    assert await worker.is_healthy()
    assert processor.calls >= 1

    await worker.stop()

    assert not await worker.is_healthy()


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_processing() -> None:
    task_id = uuid4()
    repository = FakeLeaseRepository([task_id])
    processor = BlockingProcessor()
    worker = RecoveryWorker(
        lease_repository=repository,
        processor=processor,
        poll_interval_sec=0.01,
    )

    await worker.start()

    for _ in range(20):
        if processor.started:
            break
        await asyncio.sleep(0.01)

    assert processor.started == [task_id]

    await worker.stop()

    assert not await worker.is_healthy()


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("poll_interval_sec", 0),
        ("batch_size", 0),
        ("max_concurrency", 0),
    ],
)
def test_invalid_configuration_is_rejected(
    keyword: str,
    value: int,
) -> None:
    arguments = {
        "lease_repository": FakeLeaseRepository([]),
        "processor": CompletingProcessor(),
        keyword: value,
    }

    with pytest.raises(ValueError):
        RecoveryWorker(**arguments)

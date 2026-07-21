import asyncio
import logging
from functools import partial
from collections.abc import Awaitable
from typing import Protocol
from uuid import UUID

from friday_brain.protocols.execution_lease_repository import (
    ExecutionLeaseRepository,
)


logger = logging.getLogger(__name__)


class TaskProcessor(Protocol):
    """Minimal processing boundary used by the recovery worker."""

    def process_task(
        self,
        task_id: UUID,
    ) -> Awaitable[object]: ...


class RecoveryWorker:
    """
    Periodically finds durable tasks that no active worker owns.

    Work is bounded by max_concurrency, and a task ID cannot be scheduled
    twice within the same worker process.
    """

    def __init__(
        self,
        *,
        lease_repository: ExecutionLeaseRepository,
        processor: TaskProcessor,
        poll_interval_sec: float = 1.0,
        batch_size: int = 100,
        max_concurrency: int = 4,
    ) -> None:
        if poll_interval_sec <= 0:
            raise ValueError("Recovery poll interval must be positive.")

        if batch_size < 1:
            raise ValueError("Recovery batch size must be at least one.")

        if max_concurrency < 1:
            raise ValueError("Recovery concurrency must be at least one.")

        self._lease_repository = lease_repository
        self._processor = processor
        self._poll_interval_sec = poll_interval_sec
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency

        self._loop_task: asyncio.Task[None] | None = None
        self._in_flight: dict[
            UUID,
            asyncio.Task[None],
        ] = {}

    async def start(self) -> None:
        """Start periodic recovery scanning."""
        if self._loop_task is not None and not self._loop_task.done():
            return

        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name="friday-recovery-worker",
        )

    async def stop(self) -> None:
        """Stop scanning and cancel active recovery calls."""
        loop_task = self._loop_task
        self._loop_task = None

        if loop_task is not None:
            loop_task.cancel()
            await asyncio.gather(
                loop_task,
                return_exceptions=True,
            )

        processing_tasks = list(self._in_flight.values())

        for task in processing_tasks:
            task.cancel()

        if processing_tasks:
            await asyncio.gather(
                *processing_tasks,
                return_exceptions=True,
            )

        self._in_flight.clear()

    async def is_healthy(self) -> bool:
        """Return whether the periodic scan loop is active."""
        return self._loop_task is not None and not self._loop_task.done()

    async def scan_once(self) -> int:
        """Discover and schedule one bounded batch."""
        self._prune_finished()

        capacity = self._max_concurrency - len(self._in_flight)

        if capacity <= 0:
            return 0

        task_ids = await self._lease_repository.discover_recoverable(
            limit=min(
                self._batch_size,
                capacity,
            )
        )

        scheduled = 0

        for task_id in task_ids:
            if task_id in self._in_flight:
                continue

            processing_task = asyncio.create_task(
                self._process_one(task_id),
                name=f"friday-recover-{task_id}",
            )

            self._in_flight[task_id] = processing_task
            processing_task.add_done_callback(
                partial(
                    self._processing_finished,
                    task_id,
                )
            )
            scheduled += 1

            if scheduled >= capacity:
                break

        return scheduled

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Recovery task discovery failed.")

            await asyncio.sleep(self._poll_interval_sec)

    async def _process_one(
        self,
        task_id: UUID,
    ) -> None:
        try:
            await self._processor.process_task(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Recovery processing failed for task %s.",
                task_id,
            )

    def _processing_finished(
        self,
        task_id: UUID,
        finished: asyncio.Task[None],
    ) -> None:
        current = self._in_flight.get(task_id)

        if current is finished:
            self._in_flight.pop(task_id, None)

        # Retrieve any exception so asyncio does not report an
        # unobserved task failure.
        if not finished.cancelled():
            finished.exception()

    def _prune_finished(self) -> None:
        completed = [
            task_id for task_id, task in self._in_flight.items() if task.done()
        ]

        for task_id in completed:
            task = self._in_flight.pop(
                task_id,
                None,
            )

            if task is not None and not task.cancelled():
                task.exception()

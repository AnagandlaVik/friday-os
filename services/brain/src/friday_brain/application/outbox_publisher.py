import asyncio
import json
import logging
import socket
from uuid import uuid4

from friday_brain.adapters.jetstream_event_bus import (
    deserialize_event,
)
from friday_brain.protocols.event_bus import EventBus
from friday_brain.protocols.outbox_repository import (
    OutboxMessage,
    OutboxRepository,
)

logger = logging.getLogger(__name__)


class OutboxPublisher:
    """Publishes committed outbox messages to the event bus."""

    def __init__(
        self,
        repository: OutboxRepository,
        event_bus: EventBus,
        batch_size: int = 50,
        poll_interval_sec: float = 0.25,
        lock_timeout_sec: float = 30.0,
        max_attempts: int = 5,
        retry_base_sec: float = 1.0,
        worker_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus
        self._batch_size = batch_size
        self._poll_interval_sec = poll_interval_sec
        self._lock_timeout_sec = lock_timeout_sec
        self._max_attempts = max_attempts
        self._retry_base_sec = retry_base_sec
        self._worker_id = worker_id or (f"{socket.gethostname()}-{uuid4()}")

        self._running = False
        self._publisher_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._running:
            return

        self._running = True
        self._publisher_task = asyncio.create_task(
            self._run(),
            name="friday-outbox-publisher",
        )

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False

        if self._publisher_task is not None:
            self._publisher_task.cancel()

            try:
                await self._publisher_task
            except asyncio.CancelledError:
                pass

        self._publisher_task = None

    async def is_healthy(self) -> bool:
        if not self._running:
            return False

        if self._publisher_task is None or self._publisher_task.done():
            return False

        return await self._repository.is_healthy()

    async def publish_once(self) -> int:
        messages = await self._repository.claim_batch(
            worker_id=self._worker_id,
            batch_size=self._batch_size,
            lock_timeout_sec=self._lock_timeout_sec,
        )

        for message in messages:
            await self._publish_message(message)

        return len(messages)

    async def _run(self) -> None:
        while self._running:
            try:
                published_count = await self.publish_once()

                if published_count == 0:
                    await asyncio.sleep(self._poll_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected outbox publisher iteration failure.")
                await asyncio.sleep(self._poll_interval_sec)

    async def _publish_message(
        self,
        message: OutboxMessage,
    ) -> None:
        try:
            event_bytes = json.dumps(message.event_data).encode("utf-8")
            event = deserialize_event(event_bytes)

            await self._event_bus.publish(event)

            await self._repository.mark_published(
                event_id=message.event_id,
                worker_id=self._worker_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            retry_delay = self._retry_delay(message.attempt_count)

            logger.warning(
                "Outbox publication failed for event %s on attempt %s.",
                message.event_id,
                message.attempt_count,
                exc_info=True,
            )

            await self._repository.mark_failed(
                event_id=message.event_id,
                worker_id=self._worker_id,
                error=str(error),
                retry_delay_sec=retry_delay,
                max_attempts=self._max_attempts,
            )

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = max(0, attempt_count - 1)
        delay = float(self._retry_base_sec * (2**exponent))
        return min(delay, 60.0)

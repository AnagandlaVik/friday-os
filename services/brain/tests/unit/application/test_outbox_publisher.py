from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from friday_brain.adapters.in_memory_event_bus import (
    InMemoryEventBus,
)
from friday_brain.application.outbox_publisher import (
    OutboxPublisher,
)
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
)
from friday_brain.protocols.outbox_repository import (
    OutboxMessage,
)


class FakeOutboxRepository:
    def __init__(
        self,
        messages: list[OutboxMessage],
    ) -> None:
        self.messages = list(messages)
        self.published: list[UUID] = []
        self.failed: list[tuple[UUID, str, float, int]] = []
        self.healthy = True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def is_healthy(self) -> bool:
        return self.healthy

    async def claim_batch(
        self,
        worker_id: str,
        batch_size: int,
        lock_timeout_sec: float,
    ) -> list[OutboxMessage]:
        claimed = self.messages[:batch_size]
        self.messages = self.messages[batch_size:]
        return claimed

    async def mark_published(
        self,
        event_id: UUID,
        worker_id: str,
    ) -> None:
        self.published.append(event_id)

    async def mark_failed(
        self,
        event_id: UUID,
        worker_id: str,
        error: str,
        retry_delay_sec: float,
        max_attempts: int,
    ) -> None:
        self.failed.append(
            (
                event_id,
                error,
                retry_delay_sec,
                max_attempts,
            )
        )


class FailingEventBus:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def is_healthy(self) -> bool:
        return True

    def subscribe(self, subscriber: Any) -> None:
        pass

    async def publish(self, event: Event[Any]) -> None:
        raise RuntimeError("Event bus unavailable")


def make_outbox_message(
    attempt_count: int = 1,
) -> OutboxMessage:
    task_id = uuid4()

    event = Event(
        event_type="task.created",
        task_id=task_id,
        correlation_id=uuid4(),
        payload=TaskCreatedPayload(
            input="Publish this task",
            state="pending",
        ),
    )

    return OutboxMessage(
        event_id=event.event_id,
        task_id=task_id,
        subject="friday.events.brain.task.created",
        event_data=event.model_dump(mode="json"),
        attempt_count=attempt_count,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_publish_once_marks_message_published() -> None:
    message = make_outbox_message()
    repository = FakeOutboxRepository([message])
    event_bus = InMemoryEventBus()
    await event_bus.start()

    publisher = OutboxPublisher(
        repository=repository,
        event_bus=event_bus,
        worker_id="test-worker",
    )

    count = await publisher.publish_once()

    assert count == 1
    assert repository.published == [message.event_id]
    assert repository.failed == []
    assert len(event_bus.published_events) == 1
    assert event_bus.published_events[0].event_id == message.event_id


@pytest.mark.asyncio
async def test_publish_failure_schedules_retry() -> None:
    message = make_outbox_message(attempt_count=3)
    repository = FakeOutboxRepository([message])

    publisher = OutboxPublisher(
        repository=repository,
        event_bus=FailingEventBus(),
        max_attempts=5,
        retry_base_sec=2.0,
        worker_id="test-worker",
    )

    count = await publisher.publish_once()

    assert count == 1
    assert repository.published == []
    assert len(repository.failed) == 1

    event_id, error, retry_delay, max_attempts = repository.failed[0]

    assert event_id == message.event_id
    assert "Event bus unavailable" in error
    assert retry_delay == 8.0
    assert max_attempts == 5


@pytest.mark.asyncio
async def test_empty_batch_publishes_nothing() -> None:
    repository = FakeOutboxRepository([])
    event_bus = InMemoryEventBus()
    await event_bus.start()

    publisher = OutboxPublisher(
        repository=repository,
        event_bus=event_bus,
        worker_id="test-worker",
    )

    assert await publisher.publish_once() == 0
    assert repository.published == []
    assert event_bus.published_events == []

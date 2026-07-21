import asyncio
import uuid
import pytest

from friday_brain.contracts.events import Event, TaskCreatedPayload
from friday_brain.protocols.event_bus import EventBus


class EventBusContract:
    """A contract of tests that any EventBus implementation should satisfy."""

    @pytest.fixture
    def event_bus(self) -> EventBus:
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_lifecycle(self, event_bus: EventBus):
        try:
            await event_bus.start()
            await event_bus.stop()
        except Exception as e:
            pytest.fail(f"EventBus lifecycle (start/stop) failed with error: {e}")

    @pytest.mark.asyncio
    async def test_publish_event(self, event_bus: EventBus):
        event = Event(
            event_type="task.created",
            task_id=uuid.uuid4(),
            payload=TaskCreatedPayload(input="test", state="pending"),
        )

        try:
            await event_bus.publish(event)
        except Exception as e:
            pytest.fail(f"EventBus.publish() raised an exception: {e}")

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, event_bus: EventBus):
        event = Event(
            event_type="task.created",
            task_id=uuid.uuid4(),
            payload=TaskCreatedPayload(input="test", state="pending"),
        )

        subscriber1_called = asyncio.Event()
        subscriber2_called = asyncio.Event()

        async def subscriber1(e: Event):
            subscriber1_called.set()

        async def subscriber2(e: Event):
            subscriber2_called.set()

        event_bus.subscribe(subscriber1)
        event_bus.subscribe(subscriber2)

        await event_bus.publish(event)

        # Wait for both events with a bounded timeout
        await asyncio.wait_for(
            asyncio.gather(subscriber1_called.wait(), subscriber2_called.wait()),
            timeout=5.0,
        )

        assert subscriber1_called.is_set()
        assert subscriber2_called.is_set()

    @pytest.mark.asyncio
    async def test_subscriber_failure(self, event_bus: EventBus):
        event = Event(
            event_type="task.created",
            task_id=uuid.uuid4(),
            payload=TaskCreatedPayload(input="test", state="pending"),
        )

        async def failing_subscriber(e: Event):
            raise ValueError("Test failure")

        event_bus.subscribe(failing_subscriber)

        try:
            await event_bus.publish(event)
        except Exception as e:
            pytest.fail(
                f"EventBus.publish() should not raise on subscriber failure: {e}"
            )

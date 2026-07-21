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

        subscriber1_called = False
        subscriber2_called = False

        async def subscriber1(e: Event):
            nonlocal subscriber1_called
            subscriber1_called = True

        async def subscriber2(e: Event):
            nonlocal subscriber2_called
            subscriber2_called = True

        event_bus.subscribe(subscriber1)
        event_bus.subscribe(subscriber2)

        await event_bus.publish(event)

        assert subscriber1_called
        assert subscriber2_called

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

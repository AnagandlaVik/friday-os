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

        # This test can only check that publish doesn't raise an exception.
        # Verification of delivery would require subscribers or inspecting the bus state.
        try:
            await event_bus.publish(event)
        except Exception as e:
            pytest.fail(f"EventBus.publish() raised an exception: {e}")

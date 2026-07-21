import os
import uuid
import asyncio
import pytest

import nats
from friday_brain.adapters.jetstream_event_bus import JetStreamEventBus
from friday_brain.contracts.events import Event, TaskCreatedPayload
from friday_brain.contracts.errors import BrainError
from friday_brain.protocols.event_bus import EventBus
from tests.contract.test_event_bus import EventBusContract

TEST_NATS_URL = os.getenv("TEST_NATS_URL", "nats://127.0.0.1:4222")


async def is_nats_available() -> bool:
    try:
        nc = await nats.connect(servers=[TEST_NATS_URL], connect_timeout=1.0)
        await nc.close()
        return True
    except Exception:
        return False


class TestJetStreamEventBus(EventBusContract):
    @pytest.fixture
    async def event_bus(self):
        if not await is_nats_available():
            pytest.skip("NATS is not available for testing")

        # Use unique stream and consumer names per test run to avoid collision
        unique_id = uuid.uuid4().hex[:8]
        bus = JetStreamEventBus(
            nats_url=TEST_NATS_URL,
            stream_name=f"TEST_EVENTS_{unique_id}",
            consumer_name=f"test_consumer_{unique_id}",
            subject_prefix=f"test.events.{unique_id}",
            ack_wait=1.0,
        )
        await bus.start()
        try:
            yield bus
        finally:
            # Clean up stream
            if bus._js:
                try:
                    await bus._js.delete_stream(bus._stream_name)
                except Exception:
                    pass
            await bus.stop()

    @pytest.mark.asyncio
    async def test_lifecycle_idempotence(self, event_bus: EventBus):
        # Repeated start/stop should be safe
        await event_bus.start()
        await event_bus.start()
        await event_bus.stop()
        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_subscriber_failure_and_redelivery(
        self, event_bus: JetStreamEventBus
    ):
        event = Event(
            event_type="task.created",
            task_id=uuid.uuid4(),
            payload=TaskCreatedPayload(input="test", state="pending"),
        )

        fail_count = 0
        success_called = False
        done_event = asyncio.Event()

        async def failing_subscriber(e: Event):
            nonlocal fail_count
            if fail_count < 1:
                fail_count += 1
                raise ValueError("Simulated subscriber failure")
            nonlocal success_called
            success_called = True
            done_event.set()

        event_bus.subscribe(failing_subscriber)
        await event_bus.publish(event)

        try:
            await asyncio.wait_for(done_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        assert fail_count == 1
        assert success_called

    @pytest.mark.asyncio
    async def test_nats_unavailable_error_mapping(self):
        # Point to an invalid port to trigger connection failure
        bus = JetStreamEventBus(nats_url="nats://127.0.0.1:9999", connect_timeout=0.5)
        with pytest.raises(BrainError) as exc_info:
            await bus.start()
        assert exc_info.value.code in ("nats_unavailable", "nats_connection_failed")

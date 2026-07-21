import pytest

from friday_brain.adapters.in_memory_event_bus import InMemoryEventBus
from friday_brain.protocols.event_bus import EventBus
from tests.contract.test_event_bus import EventBusContract


class TestInMemoryEventBus(EventBusContract):
    """
    Runs the EventBus contract tests against the InMemoryEventBus.
    """

    @pytest.fixture
    def event_bus(self) -> EventBus:
        return InMemoryEventBus()

import logging
from typing import List, Any

from friday_brain.contracts.events import Event
from friday_brain.protocols.event_bus import Subscriber


logger = logging.getLogger(__name__)


class InMemoryEventBus:
    """
    In-memory implementation of the EventBus protocol.
    It logs events to simulate publishing. This is not suitable for production.
    """

    def __init__(self) -> None:
        self._subscribers: List[Subscriber] = []
        self.published_events: List[Event[Any]] = []

    async def start(self) -> None:
        """No-op for in-memory event bus."""
        pass

    async def stop(self) -> None:
        """No-op for in-memory event bus."""
        pass

    async def is_healthy(self) -> bool:
        """In-memory event bus is always healthy."""
        return True

    def reset(self) -> None:
        """Clears subscribers and events for testing purposes."""
        self._subscribers.clear()
        self.published_events.clear()

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    async def publish(self, event: Event[Any]) -> None:
        """
        "Publishes" an event by calling all subscribers and storing it for
        test inspection.
        """
        self.published_events.append(event)
        logger.info(
            "Publishing event: %s for task %s",
            event.event_type,
            event.task_id,
            extra={"event_details": event.model_dump()},
        )
        for subscriber in self._subscribers:
            try:
                await subscriber(event)
            except Exception:
                logger.exception(
                    "Subscriber %r failed to handle event %s",
                    subscriber,
                    event.event_id,
                )

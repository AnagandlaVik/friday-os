
from typing import Protocol, Callable, Awaitable, Any

from friday_brain.contracts.events import Event

Subscriber = Callable[[Event[Any]], Awaitable[None]]


class EventBus(Protocol):
    """
    Protocol for an event bus to publish domain events.
    """

    def subscribe(self, subscriber: "Subscriber") -> None:
        """
        Registers a subscriber to handle events.
        """
        ...

    async def publish(self, event: Event[Any]) -> None:
        """
        Publishes an event to the bus.
        """
        ...

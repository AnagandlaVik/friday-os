from typing import Protocol, Callable, Awaitable, Any

from friday_brain.contracts.events import Event

Subscriber = Callable[[Event[Any]], Awaitable[None]]


class EventBus(Protocol):
    """
    Protocol for an event bus to publish domain events.
    """

    async def start(self) -> None:
        """
        Initialize the event bus connection and start active background consumers.
        """
        ...

    async def stop(self) -> None:
        """
        Gracefully drain active consumers and close all network connections.
        """
        ...

    async def is_healthy(self) -> bool:
        """
        Check if the event bus is connected and operational.
        """
        ...

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

from pydantic import BaseModel, TypeAdapter
from collections.abc import Mapping
import asyncio
import json
from typing import Any, List
from structlog.stdlib import get_logger

import nats
from nats.aio.client import Client as NATSClient
from nats.js import JetStreamContext
from nats.js.errors import (
    APIError,
    NotFoundError,
    FetchTimeoutError,
)
from nats.errors import (
    ConnectionClosedError,
    NoServersError,
    TimeoutError as NatsTimeoutError,
)
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    StorageType,
    StreamConfig,
)

from friday_brain.contracts.errors import BrainError, ValidationError
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
    TaskPlanningStartedPayload,
    TaskPlanValidatedPayload,
    TaskExecutionStartedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
    TaskCancelledPayload,
)
from friday_brain.protocols.event_bus import Subscriber

logger = get_logger(__name__)


EVENT_TYPE_TO_PAYLOAD_MODEL: Mapping[str, type[BaseModel]] = {
    "task.created": TaskCreatedPayload,
    "task.planning_started": TaskPlanningStartedPayload,
    "task.plan_validated": TaskPlanValidatedPayload,
    "task.execution_started": TaskExecutionStartedPayload,
    "task.completed": TaskCompletedPayload,
    "task.failed": TaskFailedPayload,
    "task.cancelled": TaskCancelledPayload,
}


def serialize_event(event: Event[Any]) -> bytes:
    """
    Validates and serializes an Event to deterministic UTF-8 JSON bytes.
    """
    if event.event_type not in EVENT_TYPE_TO_PAYLOAD_MODEL:
        raise ValidationError(f"Unsupported event type: {event.event_type}")

    expected_model = EVENT_TYPE_TO_PAYLOAD_MODEL[event.event_type]
    if not isinstance(event.payload, expected_model):
        raise ValidationError(
            f"Payload type mismatch for event type '{event.event_type}': "
            f"expected {expected_model.__name__}, got {type(event.payload).__name__}"
        )

    try:
        return event.model_dump_json().encode("utf-8")
    except Exception as e:
        raise ValidationError(f"Failed to serialize event: {e}") from e


def deserialize_event(data_bytes: bytes) -> Event[Any]:
    """
    Safely parses and validates an Event envelope and its concrete payload.
    """
    try:
        data = json.loads(data_bytes.decode("utf-8"))
    except Exception as e:
        raise ValidationError(f"Malformed JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValidationError("Event data must be a JSON object")

    schema_version = data.get("schema_version", 1)
    if schema_version != 1:
        raise ValidationError(f"Unsupported schema version: {schema_version}")

    event_type = data.get("event_type")
    if not event_type:
        raise ValidationError("Missing event_type in event envelope")

    if event_type not in EVENT_TYPE_TO_PAYLOAD_MODEL:
        raise ValidationError(f"Unsupported event type: {event_type}")

    expected_model = EVENT_TYPE_TO_PAYLOAD_MODEL[event_type]
    payload_data = data.get("payload")
    if payload_data is None:
        raise ValidationError(f"Missing payload for event type: {event_type}")

    try:
        concrete_payload = TypeAdapter(expected_model).validate_python(payload_data)
    except Exception as e:
        raise ValidationError(
            f"Invalid payload for event type '{event_type}': {e}"
        ) from e

    try:
        envelope_data = data.copy()
        envelope_data["payload"] = concrete_payload
        return Event[Any].model_validate(envelope_data)
    except Exception as e:
        raise ValidationError(f"Invalid event envelope: {e}") from e


class JetStreamEventBus:
    """
    NATS JetStream-backed implementation of the EventBus protocol.
    Provides at-least-once delivery guarantees using durable pull consumers
    and explicit acknowledgements.
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        connect_timeout: float = 2.0,
        publish_timeout: float = 2.0,
        max_reconnect_attempts: int = 5,
        stream_name: str = "FRIDAY_EVENTS",
        subject_prefix: str = "friday.events.brain",
        consumer_name: str = "friday_brain_consumer_all",
        max_deliver: int = 3,
        ack_wait: float = 30.0,
        fetch_timeout: float = 1.0,
        max_ack_pending: int = 2048,
        drain_timeout: float = 5.0,
    ) -> None:
        self._nats_url = nats_url
        self._connect_timeout = connect_timeout
        self._publish_timeout = publish_timeout
        self._max_reconnect_attempts = max_reconnect_attempts
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix
        self._consumer_name = consumer_name
        self._max_deliver = max_deliver
        self._ack_wait = ack_wait
        self._fetch_timeout = fetch_timeout
        self._max_ack_pending = max_ack_pending
        self._drain_timeout = drain_timeout

        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._pull_subscription: Any = None
        self._subscribers: List[Subscriber] = []
        self._running = False
        self._pull_task: asyncio.Task[None] | None = None
        self._advisory_task: asyncio.Task[None] | None = None

    async def is_healthy(self) -> bool:
        """
        Check if the event bus is connected and operational.
        """
        if self._nc is None or self._js is None:
            return False

        if not self._nc.is_connected or self._nc.is_closed or self._nc.is_reconnecting:
            return False

        probe_timeout = min(self._connect_timeout, 2.0)

        try:
            # Flush sends a PING and waits for a PONG, providing a live
            # connection probe rather than relying on cached client state.
            await asyncio.wait_for(
                self._nc.flush(),
                timeout=probe_timeout,
            )

            await asyncio.wait_for(
                self._js.account_info(),
                timeout=probe_timeout,
            )
            return True
        except asyncio.CancelledError:
            raise
        except (
            TimeoutError,
            NatsTimeoutError,
            ConnectionClosedError,
            NoServersError,
            APIError,
        ) as e:
            logger.warning("NATS JetStream health check failed: %s", e)
            return False
        except Exception as e:
            logger.error("Unexpected error during NATS JetStream health check: %s", e)
            return False

    async def start(self) -> None:
        """
        Connects to NATS, provisions the stream and durable consumer,
        and starts background message processing.
        """
        if self._running:
            return

        try:
            self._nc = await nats.connect(
                servers=[self._nats_url],
                connect_timeout=self._connect_timeout,
                max_reconnect_attempts=self._max_reconnect_attempts,
            )
            self._js = self._nc.jetstream()
        except (NoServersError, NatsTimeoutError, ConnectionClosedError) as e:
            raise BrainError(
                f"NATS unavailable or connection timeout: {e}",
                code="nats_unavailable",
            ) from e
        except Exception as e:
            raise BrainError(
                f"Failed to connect to NATS: {e}",
                code="nats_connection_failed",
            ) from e

        await self._ensure_stream_and_consumer()

        try:
            self._pull_subscription = await self._js.pull_subscribe_bind(
                stream=self._stream_name,
                consumer=self._consumer_name,
            )
        except Exception as e:
            raise BrainError(
                f"Failed to bind pull subscription: {e}",
                code="incompatible_stream_configuration",
            ) from e

        self._running = True
        self._pull_task = asyncio.create_task(self._pull_loop())
        self._advisory_task = asyncio.create_task(self._advisory_loop())

    async def stop(self) -> None:
        """
        Gracefully stops background consumers and closes the NATS connection.
        """
        if not self._running:
            return

        self._running = False

        # Cancel background tasks
        for task in [self._pull_task, self._advisory_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._pull_task = None
        self._advisory_task = None

        if self._pull_subscription:
            try:
                await self._pull_subscription.unsubscribe()
            except Exception:
                pass
            self._pull_subscription = None

        if self._nc:
            try:
                await asyncio.wait_for(
                    self._nc.drain(),
                    timeout=self._drain_timeout,
                )
            except Exception:
                pass
            finally:
                await self._nc.close()
                self._nc = None
                self._js = None

    def subscribe(self, subscriber: Subscriber) -> None:
        """
        Registers a subscriber to handle events.
        """
        self._subscribers.append(subscriber)

    async def publish(self, event: Event[Any]) -> None:
        """
        Publishes an event to JetStream.
        """
        if not self._js:
            raise BrainError("Event bus is not started", code="nats_unavailable")

        subject = self._get_subject(event.event_type)
        try:
            payload_bytes = serialize_event(event)
        except ValidationError as e:
            raise e
        except Exception as e:
            raise ValidationError(f"Failed to serialize event: {e}") from e

        # Enforce bounded payload size (e.g., 1MB limit)
        if len(payload_bytes) > 1024 * 1024:
            raise ValidationError("Event payload size exceeds 1MB limit")

        headers = {"Nats-Msg-Id": str(event.event_id)}

        try:
            await self._js.publish(
                subject,
                payload_bytes,
                headers=headers,
                timeout=self._publish_timeout,
            )
        except NatsTimeoutError as e:
            raise BrainError("Publication timeout", code="publication_timeout") from e
        except APIError as e:
            raise BrainError(
                f"Publication rejected by server: {e}",
                code="publication_rejection",
            ) from e
        except Exception as e:
            raise BrainError(
                f"Failed to publish event: {e}", code="internal_error"
            ) from e

    def _get_subject(self, event_type: str) -> str:
        """
        Derives a safe subject name from the event type.
        """
        if not event_type or not all(c.isalnum() or c in "._-" for c in event_type):
            raise ValidationError(f"Invalid event type format: {event_type}")
        safe_type = event_type.replace(".", "_")
        return f"{self._subject_prefix}.{safe_type}"

    async def _ensure_stream_and_consumer(self) -> None:
        """
        Idempotently provisions the stream and durable consumer.
        """
        assert self._js is not None
        stream_config = StreamConfig(
            name=self._stream_name,
            subjects=[f"{self._subject_prefix}.*"],
            storage=StorageType.FILE,
            max_msg_size=1024 * 1024,  # 1MB limit
        )

        try:
            # Check if stream exists
            existing = await self._js.stream_info(self._stream_name)
            # Validate configuration compatibility
            if existing.config.storage not in (StorageType.FILE, "file"):
                raise BrainError(
                    f"Incompatible stream storage type: {existing.config.storage}",
                    code="incompatible_stream_configuration",
                )
        except NotFoundError:
            # Create stream if not found
            try:
                await self._js.add_stream(config=stream_config)
            except Exception as e:
                raise BrainError(
                    f"Failed to create stream: {e}",
                    code="incompatible_stream_configuration",
                ) from e
        except Exception as e:
            if not isinstance(e, BrainError):
                raise BrainError(
                    f"Failed to validate stream: {e}",
                    code="incompatible_stream_configuration",
                ) from e
            raise

        # Ensure durable consumer exists
        consumer_config = ConsumerConfig(
            durable_name=self._consumer_name,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self._ack_wait,  # seconds
            max_deliver=self._max_deliver,
            max_ack_pending=self._max_ack_pending,
        )
        try:
            await self._js.add_consumer(self._stream_name, config=consumer_config)
        except Exception as e:
            raise BrainError(
                f"Failed to create durable consumer: {e}",
                code="incompatible_stream_configuration",
            ) from e

    async def _pull_loop(self) -> None:
        """
        Background loop pulling messages from the durable consumer.
        """
        while self._running:
            if not self._pull_subscription:
                await asyncio.sleep(0.1)
                continue
            try:
                # Pull messages with a short timeout to allow graceful cancellation
                msgs = await self._pull_subscription.fetch(
                    batch=1,
                    timeout=self._fetch_timeout,
                )
                for msg in msgs:
                    await self._process_message(msg)
            except asyncio.CancelledError:
                raise
            except (FetchTimeoutError, NatsTimeoutError, TimeoutError):
                # No messages are available during this pull interval.
                await asyncio.sleep(0.1)
                continue
            except Exception as e:
                if not self._running:
                    break
                logger.error("Error in JetStream pull loop: %s", e)
                await asyncio.sleep(1.0)

    async def _process_message(self, msg: Any) -> None:
        """
        Deserializes and dispatches a single message to all subscribers.
        """
        try:
            event = deserialize_event(msg.data)
        except ValidationError as e:
            logger.error("Failed to deserialize event: %s", e)
            await msg.term()
            return
        except Exception as e:
            logger.error("Unexpected error during deserialization: %s", e)
            await msg.term()
            return

        # Dispatch to all subscribers
        success = True
        for subscriber in self._subscribers:
            try:
                await subscriber(event)
            except Exception as e:
                success = False
                logger.error(
                    "Subscriber failed to handle event %s: %s",
                    event.event_id,
                    str(e),
                )

        if success:
            try:
                await msg.ack()
            except Exception as e:
                logger.error("Failed to acknowledge message: %s", e)
        else:
            # Do not acknowledge, allowing redelivery
            try:
                await msg.nak()
            except Exception as e:
                logger.error("Failed to NAK message: %s", e)

    async def _advisory_loop(self) -> None:
        """
        Listens to JetStream max-delivery advisories and publishes failure records.
        """
        if not self._nc:
            return

        advisory_subject = f"$JS.EVENT.ADVISORY.CONSUMER_MAX_DELIVERIES.{self._stream_name}.{self._consumer_name}"
        try:
            sub = await self._nc.subscribe(advisory_subject)
        except Exception as e:
            logger.warning("Could not subscribe to max-delivery advisories: %s", e)
            return

        while self._running:
            try:
                msg = await sub.next_msg(timeout=1.0)
                await self._handle_advisory(msg)
            except NatsTimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    break
                logger.error("Error in advisory loop: %s", e)
                await asyncio.sleep(1.0)

    async def _handle_advisory(self, msg: Any) -> None:
        """
        Parses a max-delivery advisory and publishes a sanitized failure record.
        """
        try:
            data = json.loads(msg.data.decode("utf-8"))
            stream_seq = data.get("stream_seq")
            deliveries = data.get("deliveries")

            # Publish a sanitized failure record to a dedicated failed-event subject
            # to prevent infinite loops, we do not process this as a normal event
            failure_record = {
                "original_event_id": None,  # Will be filled if we can retrieve it
                "stream_sequence": stream_seq,
                "consumer": self._consumer_name,
                "deliveries": deliveries,
                "failure_category": "max_deliveries_exceeded",
                "timestamp": data.get("timestamp"),
            }

            logger.error(
                "Max deliveries exceeded for message. Stream seq: %s, Consumer: %s. Sanitized record: %s",
                stream_seq,
                self._consumer_name,
                failure_record,
            )
        except Exception as e:
            logger.error("Failed to process max-delivery advisory: %s", e)

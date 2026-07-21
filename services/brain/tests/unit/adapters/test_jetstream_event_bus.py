import pytest
import uuid

from friday_brain.adapters.jetstream_event_bus import (
    JetStreamEventBus,
    serialize_event,
    deserialize_event,
)
from friday_brain.contracts.errors import ValidationError
from friday_brain.contracts.events import (
    Event,
    TaskCreatedPayload,
    TaskPlanningStartedPayload,
)


def test_subject_generation() -> None:
    bus = JetStreamEventBus()
    subject = bus._get_subject("task.created")
    assert subject == "friday.events.brain.task_created"

    with pytest.raises(ValidationError):
        bus._get_subject("invalid/subject")

    with pytest.raises(ValidationError):
        bus._get_subject("")


def test_event_serialization_and_deserialization() -> None:
    event = Event(
        event_type="task.created",
        task_id=uuid.uuid4(),
        payload=TaskCreatedPayload(input="test", state="pending"),
    )
    serialized = serialize_event(event)
    parsed = deserialize_event(serialized)
    assert parsed.event_id == event.event_id
    assert isinstance(parsed.payload, TaskCreatedPayload)
    assert parsed.payload.input == "test"


def test_malformed_json() -> None:
    with pytest.raises(ValidationError):
        deserialize_event(b"{invalid json")


def test_unknown_event_type() -> None:
    # Test serialization of unknown event type
    event = Event(
        event_type="unknown.event",
        task_id=uuid.uuid4(),
        payload=TaskCreatedPayload(input="test", state="pending"),
    )
    with pytest.raises(ValidationError):
        serialize_event(event)

    # Test deserialization of unknown event type
    bad_data = b'{"event_type": "unknown.event", "schema_version": 1, "task_id": "00000000-0000-0000-0000-000000000000", "payload": {}}'
    with pytest.raises(ValidationError):
        deserialize_event(bad_data)


def test_payload_type_mismatch() -> None:
    # Payload type mismatch
    event = Event(
        event_type="task.created",
        task_id=uuid.uuid4(),
        payload=TaskPlanningStartedPayload(),  # Wrong payload type for task.created
    )
    with pytest.raises(ValidationError):
        serialize_event(event)


def test_unsupported_schema_version() -> None:
    bad_data = b'{"event_type": "task.created", "schema_version": 2, "task_id": "00000000-0000-0000-0000-000000000000", "payload": {"input": "test", "state": "pending"}}'
    with pytest.raises(ValidationError):
        deserialize_event(bad_data)

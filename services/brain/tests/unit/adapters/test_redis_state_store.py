import json
import uuid
import pytest

from friday_brain.adapters.redis_state_store import (
    RedisStateStore,
    _handle_redis_exception,
)
from friday_brain.contracts.errors import (
    BrainError,
    ValidationError,
)
from friday_brain.contracts.tasks import Task


def test_key_generation() -> None:
    store = RedisStateStore(redis_url="redis://localhost:6379/15")
    task_id = uuid.uuid4()
    assert store._get_task_key(task_id) == f"friday:brain:tasks:{task_id}"
    assert store._get_idemp_key("key123") == "friday:brain:idempotency:key123"


async def test_serialization_deserialization() -> None:
    store = RedisStateStore(redis_url="redis://localhost:6379/15")
    task = Task(input="test serialisation")

    # Check serialization includes schema_version
    task_copy = task.model_copy(deep=True)
    task_copy.version += 1
    task_json_str = task_copy.model_dump_json()
    task_dict = json.loads(task_json_str)
    task_dict["schema_version"] = 1
    final_json_str = json.dumps(task_dict)

    loaded_task = store._deserialize_task(
        {"data": final_json_str, "schema_version": "1"}
    )
    assert loaded_task.id == task.id
    assert loaded_task.version == task.version + 1
    assert loaded_task.input == "test serialisation"


def test_schema_version_validation() -> None:
    store = RedisStateStore(redis_url="redis://localhost:6379/15")

    # Unsupported version
    bad_data = {
        "data": '{"input": "test"}',
        "schema_version": "2",
    }
    with pytest.raises(ValidationError) as exc_info:
        store._deserialize_task(bad_data)
    assert "Unsupported schema version" in str(exc_info.value)

    # Non-integer version
    bad_data_format = {
        "data": '{"input": "test"}',
        "schema_version": "abc",
    }
    with pytest.raises(ValidationError) as exc_info:
        store._deserialize_task(bad_data_format)
    assert "Invalid schema version format" in str(exc_info.value)


def test_error_mapping() -> None:
    from redis.exceptions import ConnectionError, RedisError, TimeoutError

    err1 = _handle_redis_exception(TimeoutError("Command timed out"))
    assert isinstance(err1, BrainError)
    assert err1.code == "database_timeout"
    assert err1.retryable is True

    err2 = _handle_redis_exception(ConnectionError("No connection"))
    assert isinstance(err2, BrainError)
    assert err2.code == "database_unavailable"
    assert err2.retryable is True

    err3 = _handle_redis_exception(RedisError("Some other redis error"))
    assert isinstance(err3, BrainError)
    assert err3.code == "database_error"
    assert err3.retryable is False

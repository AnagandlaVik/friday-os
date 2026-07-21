import asyncio
import os
import uuid
import pytest
import redis.asyncio as redis

from friday_brain.adapters.redis_state_store import RedisStateStore
from friday_brain.contracts.errors import (
    BrainError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ValidationError,
)
from friday_brain.contracts.tasks import Task
from tests.contract.test_state_store import StateStoreContract

# Check if redis is available
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


async def is_redis_available() -> bool:
    client = None
    try:
        client = redis.from_url(TEST_REDIS_URL, socket_timeout=1.0)
        await client.ping()
        await client.aclose()
        return True
    except Exception:
        if client:
            try:
                await client.aclose()
            except Exception:
                pass
        return False


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store():
    if not await is_redis_available():
        pytest.skip("Redis is not available for testing")

    store = RedisStateStore(redis_url=TEST_REDIS_URL)
    await store.start()

    # Flush DB to ensure isolation before test
    if store._client is not None:
        await store._client.flushdb()

    try:
        yield store
    finally:

        async def cleanup():
            if store._client is not None:
                try:
                    await store._client.flushdb()
                except Exception:
                    pass
            await store.stop()

        await asyncio.shield(cleanup())


class TestRedisStateStore(StateStoreContract):
    @pytest.fixture
    async def store(self):
        if not await is_redis_available():
            pytest.skip("Redis is not available for testing")

        store = RedisStateStore(redis_url=TEST_REDIS_URL)
        await store.start()

        # Flush DB to ensure isolation before test
        if store._client is not None:
            await store._client.flushdb()

        try:
            yield store
        finally:

            async def cleanup():
                if store._client is not None:
                    try:
                        await store._client.flushdb()
                    except Exception:
                        pass
                await store.stop()

            await asyncio.shield(cleanup())


async def test_lifecycle_idempotence() -> None:
    if not await is_redis_available():
        pytest.skip("Redis is not available")
    store = RedisStateStore(redis_url=TEST_REDIS_URL)

    # Start twice
    await store.start()
    await store.start()

    assert store._client is not None
    await store._client.ping()

    # Stop twice
    await store.stop()
    await store.stop()

    assert store._client is None


async def test_integration_save_and_retrieve(store: RedisStateStore) -> None:
    task = Task(input="hello integration")
    await store.save(task)

    retrieved = await store.get(task.id)
    assert retrieved is not None
    assert retrieved.input == "hello integration"
    assert retrieved.version == 2


async def test_integration_task_not_found(store: RedisStateStore) -> None:
    non_existent = uuid.uuid4()
    assert await store.get(non_existent) is None


async def test_integration_idempotency_lookup_registration(
    store: RedisStateStore,
) -> None:
    key = "key-unique"
    task = Task(input="idemp test", idempotency_key=key)
    await store.save(task)

    retrieved = await store.find_by_idempotency_key(key)
    assert retrieved is not None
    assert retrieved.id == task.id


async def test_integration_concurrent_idempotency_registration(
    store: RedisStateStore,
) -> None:
    key = "conflict-key"
    task1 = Task(input="task1", idempotency_key=key)
    task2 = Task(input="task2", idempotency_key=key)

    await store.save(task1)

    # Attempting to save task2 with same idempotency key must raise IdempotencyConflictError
    with pytest.raises(IdempotencyConflictError):
        await store.save(task2)


async def test_integration_stale_write_rejection(store: RedisStateStore) -> None:
    task = Task(input="version-test")
    await store.save(task)  # version is now 2 in DB, and task.version is now 2

    # Create a stale copy of version 1 (which would try to save expecting version 1, but db has 2)
    stale_task = task.model_copy(deep=True)
    stale_task.version = 1

    with pytest.raises(InvalidStateTransitionError):
        await store.save(stale_task)


async def test_integration_corrupted_stored_data(store: RedisStateStore) -> None:
    task_id = uuid.uuid4()
    task_key = f"friday:brain:tasks:{task_id}"

    # Write corrupt data directly
    assert store._client is not None
    await store._client.hset(
        task_key, mapping={"data": "{corrupt json", "schema_version": "1"}
    )

    with pytest.raises(ValidationError):
        await store.get(task_id)


async def test_integration_unsupported_schema_version(store: RedisStateStore) -> None:
    task_id = uuid.uuid4()
    task_key = f"friday:brain:tasks:{task_id}"

    assert store._client is not None
    await store._client.hset(
        task_key, mapping={"data": '{"input": "test"}', "schema_version": "2"}
    )

    with pytest.raises(ValidationError):
        await store.get(task_id)


async def test_integration_connection_loss(store: RedisStateStore) -> None:
    # Stop the store to simulate database unavailability
    await store.stop()

    # Attempting to get or save should raise BrainError database_unavailable
    with pytest.raises(BrainError) as exc_info:
        await store.get(uuid.uuid4())
    assert exc_info.value.code == "database_unavailable"

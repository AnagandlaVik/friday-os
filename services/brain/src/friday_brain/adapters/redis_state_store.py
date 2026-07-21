import json
import uuid
from typing import Dict

import redis.asyncio as redis
from redis import exceptions as redis_exc

from friday_brain.contracts.errors import (
    BrainError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ValidationError,
)
from friday_brain.contracts.tasks import Task


def _handle_redis_exception(e: Exception) -> Exception:
    if isinstance(e, redis_exc.TimeoutError):
        return BrainError(
            "Redis command timed out", code="database_timeout", retryable=True
        )
    if isinstance(e, redis_exc.ConnectionError):
        return BrainError(
            "Redis is unavailable", code="database_unavailable", retryable=True
        )
    if isinstance(e, redis_exc.RedisError):
        return BrainError(
            f"Database error: {str(e)}", code="database_error", retryable=False
        )
    return e


def _normalize_redis_dict(data: dict[bytes | str, bytes | str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for k, v in data.items():
        try:
            key = k.decode("utf-8") if isinstance(k, bytes) else k
            val = v.decode("utf-8") if isinstance(v, bytes) else v
            normalized[key] = val
        except Exception as e:
            raise ValidationError(f"Failed to decode Redis data: {str(e)}")
    return normalized


class RedisStateStore:
    """
    Redis-backed implementation of the StateStore protocol.
    Provides atomic concurrency via optimistic locking (WATCH/MULTI/EXEC),
    idempotency checks, and schema version checking.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        connect_timeout: float = 2.0,
        command_timeout: float = 1.0,
    ) -> None:
        self.redis_url = redis_url
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._pool: redis.ConnectionPool | None = None
        self._client: redis.Redis | None = None

    def _get_task_key(self, task_id: uuid.UUID) -> str:
        return f"friday:brain:tasks:{task_id}"

    def _get_idemp_key(self, idempotency_key: str) -> str:
        return f"friday:brain:idempotency:{idempotency_key}"

    async def start(self) -> None:
        """
        Initialize the connection pool and client, and verify connectivity.
        """
        if self._pool is not None:
            return

        try:
            # We configure decode_responses=True so that we get strings instead of bytes.
            self._pool = redis.ConnectionPool.from_url(
                self.redis_url,
                socket_timeout=self.command_timeout,
                socket_connect_timeout=self.connect_timeout,
                max_connections=10,
                decode_responses=True,
            )
            self._client = redis.Redis(connection_pool=self._pool)
            await self._client.ping()
        except Exception as e:
            await self.stop()
            raise _handle_redis_exception(e)

    async def stop(self) -> None:
        """
        Gracefully release connection pools and close clients.
        """
        pool = self._pool
        client = self._client

        self._pool = None
        self._client = None

        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        if pool is not None:
            try:
                await pool.disconnect()
            except Exception:
                pass

    def _deserialize_task(self, data: Dict[str, str]) -> Task:
        stored_json_str = data.get("data")
        if not stored_json_str:
            raise ValidationError("Missing data field in task hash")

        schema_version_str = data.get("schema_version", "1")
        try:
            schema_version = int(schema_version_str)
        except ValueError:
            raise ValidationError(
                f"Invalid schema version format: {schema_version_str}"
            )

        if schema_version != 1:
            raise ValidationError(f"Unsupported schema version: {schema_version}")

        try:
            task_dict = json.loads(stored_json_str)
        except Exception as e:
            raise ValidationError(f"Corrupted stored state: {str(e)}")

        task_dict.pop("schema_version", None)
        try:
            return Task.model_validate(task_dict)
        except Exception as e:
            raise ValidationError(f"Corrupted stored state (schema mismatch): {str(e)}")

    async def save(self, task: Task) -> None:
        """
        Saves the complete task state.
        Uses WATCH/MULTI/EXEC for optimistic concurrency control and idempotency safety.
        """
        if self._client is None:
            raise BrainError("State store is not started", code="database_unavailable")

        task_key = self._get_task_key(task.id)
        idemp_key = (
            self._get_idemp_key(task.idempotency_key) if task.idempotency_key else None
        )

        max_retries = 5
        for _ in range(max_retries):
            try:
                async with self._client.pipeline(transaction=True) as pipe:
                    await pipe.watch(task_key)
                    if idemp_key:
                        await pipe.watch(idemp_key)

                    # Fetch current stored task to check version
                    current_data = await self._client.hgetall(task_key)
                    if current_data:
                        stored_task = self._deserialize_task(
                            _normalize_redis_dict(current_data)
                        )
                        if stored_task.version != task.version:
                            raise InvalidStateTransitionError(
                                from_state=f"version {stored_task.version}",
                                to_state=f"version {task.version}",
                            )

                    # Check idempotency conflict
                    if idemp_key:
                        existing_task_id = await self._client.get(idemp_key)
                        if existing_task_id and existing_task_id != str(task.id):
                            raise IdempotencyConflictError(task.idempotency_key or "")

                    # Prepare serialization
                    task_copy = task.model_copy(deep=True)
                    task_copy.version += 1
                    task_json_str = task_copy.model_dump_json()

                    task_dict = json.loads(task_json_str)
                    task_dict["schema_version"] = 1
                    final_json_str = json.dumps(task_dict)

                    # Execute write in transaction
                    pipe.multi()  # type: ignore[no-untyped-call] # redis-py pipeline.multi is untyped
                    pipe.hset(
                        task_key,
                        mapping={
                            "data": final_json_str,
                            "schema_version": "1",
                        },
                    )
                    pipe.expire(task_key, 7 * 24 * 3600)
                    if idemp_key:
                        pipe.set(idemp_key, str(task.id))
                        pipe.expire(idemp_key, 7 * 24 * 3600)

                    results = await pipe.execute()
                    if results is not None:
                        # Success! Update input task version
                        task.version += 1
                        return
            except redis_exc.WatchError:
                continue
            except (BrainError, ValidationError):
                raise
            except Exception as e:
                raise _handle_redis_exception(e)

        raise BrainError(
            "Transaction failed due to concurrent modification, retries exhausted",
            code="concurrency_conflict",
            retryable=True,
        )

    async def get(self, task_id: uuid.UUID) -> Task | None:
        """
        Retrieves a task by its ID.
        """
        if self._client is None:
            raise BrainError("State store is not started", code="database_unavailable")

        task_key = self._get_task_key(task_id)
        try:
            data = await self._client.hgetall(task_key)
            if not data:
                return None
            return self._deserialize_task(_normalize_redis_dict(data))
        except ValidationError:
            raise
        except Exception as e:
            raise _handle_redis_exception(e)

    async def find_by_idempotency_key(self, idempotency_key: str) -> Task | None:
        """
        Finds a task by its idempotency key.
        """
        if self._client is None:
            raise BrainError("State store is not started", code="database_unavailable")

        idemp_key = self._get_idemp_key(idempotency_key)
        try:
            task_id_str = await self._client.get(idemp_key)
            if not task_id_str:
                return None
            try:
                task_id_normal = (
                    task_id_str.decode("utf-8")
                    if isinstance(task_id_str, bytes)
                    else task_id_str
                )
            except Exception as e:
                raise ValidationError(f"Failed to decode task ID from Redis: {str(e)}")
            return await self.get(uuid.UUID(task_id_normal))
        except Exception as e:
            raise _handle_redis_exception(e)

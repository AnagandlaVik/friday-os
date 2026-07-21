import uuid
import pytest
from friday_brain.contracts.tasks import Task, TaskState
from friday_brain.protocols.state_store import StateStore


class StateStoreContract:
    """A contract of tests that any StateStore implementation should satisfy."""

    @pytest.fixture
    def store(self) -> StateStore:
        """Subclasses must override this to provide a StateStore instance."""
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_save_and_get_task(self, store: StateStore):
        task = Task(input="test")
        await store.save(task)
        retrieved_task = await store.get(task.id)
        assert retrieved_task is not None
        assert retrieved_task.id == task.id
        assert retrieved_task.input == task.input
        assert retrieved_task is not task  # Must return a copy

    @pytest.mark.asyncio
    async def test_get_non_existent_task(self, store: StateStore):
        task = await store.get(uuid.uuid4())
        assert task is None

    @pytest.mark.asyncio
    async def test_find_by_idempotency_key(self, store: StateStore):
        key = "idempotency-key"
        task = Task(input="test", idempotency_key=key)
        await store.save(task)
        retrieved_task = await store.find_by_idempotency_key(key)
        assert retrieved_task is not None
        assert retrieved_task.id == task.id

    @pytest.mark.asyncio
    async def test_update_task(self, store: StateStore):
        task = Task(input="test")
        await store.save(task)

        updated_task = await store.get(task.id)
        updated_task.state = TaskState.COMPLETED
        await store.save(updated_task)

        final_task = await store.get(task.id)
        assert final_task.state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_immutability(self, store: StateStore):
        """Tests that the store returns copies to prevent mutation."""
        task = Task(input="test")
        await store.save(task)

        retrieved_task = await store.get(task.id)
        retrieved_task.input = "modified"  # Modify the copy

        # Get the task again, it should be unchanged
        store_task = await store.get(task.id)
        assert store_task.input == "test"

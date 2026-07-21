import pytest

from friday_brain.adapters.in_memory_state_store import InMemoryStateStore
from friday_brain.protocols.state_store import StateStore
from tests.contract.test_state_store import StateStoreContract


class TestInMemoryStateStore(StateStoreContract):
    """
    Runs the StateStore contract tests against the InMemoryStateStore.
    """

    @pytest.fixture
    def store(self) -> StateStore:
        return InMemoryStateStore()

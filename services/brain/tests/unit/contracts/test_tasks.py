import pytest
from friday_brain.contracts.errors import InvalidStateTransitionError
from friday_brain.contracts.tasks import Task, TaskState


def test_task_defaults():
    task = Task(input="test")
    assert task.state == TaskState.PENDING
    assert task.id is not None
    assert task.created_at is not None
    assert task.updated_at is not None


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        (TaskState.PENDING, TaskState.PLANNING),
        (TaskState.PENDING, TaskState.CANCELLATION_REQUESTED),
        (TaskState.PENDING, TaskState.FAILED),
        (TaskState.PLANNING, TaskState.EXECUTING),
        (TaskState.PLANNING, TaskState.CANCELLATION_REQUESTED),
        (TaskState.PLANNING, TaskState.FAILED),
        (TaskState.EXECUTING, TaskState.COMPLETED),
        (TaskState.EXECUTING, TaskState.CANCELLATION_REQUESTED),
        (TaskState.EXECUTING, TaskState.FAILED),
        (TaskState.CANCELLATION_REQUESTED, TaskState.CANCELLED),
    ],
)
def test_valid_state_transitions(from_state, to_state):
    task = Task(input="test", state=from_state)
    task.update_state(to_state)
    assert task.state == to_state


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        (TaskState.COMPLETED, TaskState.PENDING),
        (TaskState.FAILED, TaskState.PENDING),
        (TaskState.CANCELLED, TaskState.PENDING),
        (TaskState.PENDING, TaskState.EXECUTING),
        (TaskState.PLANNING, TaskState.COMPLETED),
    ],
)
def test_invalid_state_transitions(from_state, to_state):
    task = Task(input="test", state=from_state)
    with pytest.raises(InvalidStateTransitionError):
        task.update_state(to_state)

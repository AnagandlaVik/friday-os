from types import SimpleNamespace
from typing import Any

import pytest

from friday_brain.application.durable_task_processor import (
    DurableTaskProcessor,
)
from friday_brain.contracts.tasks import TaskState
from friday_brain.observability.metrics import (
    MetricsRegistry,
)


@pytest.mark.asyncio
async def test_transition_wrapper_records_new_state() -> None:
    metrics = MetricsRegistry()

    processor = object.__new__(DurableTaskProcessor)
    processor._metrics_registry = metrics

    async def fake_transition(
        *args: Any,
        **kwargs: Any,
    ) -> SimpleNamespace:
        del args
        del kwargs

        return SimpleNamespace(state=TaskState.COMPLETED)

    processor._transition_with_event = fake_transition

    transitioned = await processor._transition_with_metrics()

    assert transitioned.state == TaskState.COMPLETED

    output = metrics.render_prometheus()

    assert 'friday_brain_task_transitions_total{state="completed"} 1.0' in output

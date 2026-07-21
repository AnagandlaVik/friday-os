from types import SimpleNamespace

import pytest
from fastapi.responses import PlainTextResponse

from friday_brain.api.routes_metrics import (
    metrics,
)
from friday_brain.observability.metrics import (
    MetricsRegistry,
)


class FakeCompositionRoot:
    def __init__(self) -> None:
        self.registry = MetricsRegistry()

    def get_metrics_registry(
        self,
    ) -> MetricsRegistry:
        return self.registry


@pytest.mark.asyncio
async def test_metrics_endpoint() -> None:
    root = FakeCompositionRoot()
    root.registry.record_task_transition(state="completed")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(composition_root=root))
    )

    response = await metrics(request)

    assert isinstance(
        response,
        PlainTextResponse,
    )
    assert response.status_code == 200
    assert b"friday_brain_task_transitions_total" in response.body

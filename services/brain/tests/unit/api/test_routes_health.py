import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from friday_brain.api.routes_health import (
    readiness_check,
    version_check,
)
from friday_brain.contracts.api import (
    ReadinessResponse,
    VersionResponse,
)


class HealthComponent:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    async def is_healthy(self) -> bool:
        return self.healthy


class FakeCompositionRoot:
    def __init__(
        self,
        *,
        task_repository: bool = True,
        execution_lease_repository: bool = True,
        execution_plan_repository: bool = True,
        tool_invocation_repository: bool = True,
        authorization_repository: bool = True,
        outbox_repository: bool = True,
        outbox_publisher: bool = True,
        recovery_worker: bool = True,
        state_store: bool = True,
        event_bus: bool = True,
    ) -> None:
        self.task_repository = HealthComponent(task_repository)
        self.execution_lease_repository = HealthComponent(execution_lease_repository)
        self.execution_plan_repository = HealthComponent(execution_plan_repository)
        self.tool_invocation_repository = HealthComponent(tool_invocation_repository)
        self.authorization_repository = HealthComponent(authorization_repository)
        self.outbox_repository = HealthComponent(outbox_repository)
        self.outbox_publisher = HealthComponent(outbox_publisher)
        self.recovery_worker = HealthComponent(recovery_worker)
        self.state_store = HealthComponent(state_store)
        self.event_bus = HealthComponent(event_bus)

    def get_task_repository(self) -> HealthComponent:
        return self.task_repository

    def get_execution_lease_repository(
        self,
    ) -> HealthComponent:
        return self.execution_lease_repository

    def get_execution_plan_repository(
        self,
    ) -> HealthComponent:
        return self.execution_plan_repository

    def get_authorization_repository(
        self,
    ) -> HealthComponent:
        return self.authorization_repository

    def get_tool_invocation_repository(
        self,
    ) -> HealthComponent:
        return self.tool_invocation_repository

    def get_outbox_repository(
        self,
    ) -> HealthComponent:
        return self.outbox_repository

    def get_outbox_publisher(
        self,
    ) -> HealthComponent:
        return self.outbox_publisher

    def get_recovery_worker(
        self,
    ) -> HealthComponent:
        return self.recovery_worker

    def get_state_store(self) -> HealthComponent:
        return self.state_store

    def get_event_bus(self) -> HealthComponent:
        return self.event_bus


def make_request(
    root: FakeCompositionRoot,
) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(composition_root=root)),
        state=SimpleNamespace(correlation_id="readiness-test"),
    )


@pytest.mark.asyncio
async def test_readiness_succeeds_when_all_healthy() -> None:
    response = await readiness_check(make_request(FakeCompositionRoot()))

    assert isinstance(response, ReadinessResponse)


@pytest.mark.asyncio
async def test_unhealthy_recovery_worker_returns_503() -> None:
    response = await readiness_check(
        make_request(FakeCompositionRoot(recovery_worker=False))
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503

    payload = json.loads(response.body)

    assert payload["details"]["unhealthy_components"] == ["recovery_worker"]


@pytest.mark.asyncio
async def test_unhealthy_task_repository_returns_503() -> None:
    response = await readiness_check(
        make_request(FakeCompositionRoot(task_repository=False))
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503

    payload = json.loads(response.body)

    assert "task_repository" in payload["details"]["unhealthy_components"]


@pytest.mark.asyncio
async def test_multiple_failures_are_all_reported() -> None:
    response = await readiness_check(
        make_request(
            FakeCompositionRoot(
                execution_lease_repository=False,
                execution_plan_repository=False,
                recovery_worker=False,
            )
        )
    )

    assert isinstance(response, JSONResponse)

    payload = json.loads(response.body)
    unhealthy = payload["details"]["unhealthy_components"]

    assert unhealthy == [
        "execution_lease_repository",
        "execution_plan_repository",
        "recovery_worker",
    ]


@pytest.mark.asyncio
async def test_readiness_reports_unhealthy_tool_invocation_repository() -> None:
    response = await readiness_check(
        make_request(
            FakeCompositionRoot(
                tool_invocation_repository=False,
            )
        )
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503

    payload = json.loads(response.body)

    assert "tool_invocation_repository" in payload["details"]["unhealthy_components"]


@pytest.mark.asyncio
async def test_readiness_reports_unhealthy_authorization_repository() -> None:
    response = await readiness_check(
        make_request(
            FakeCompositionRoot(
                authorization_repository=False,
            )
        )
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503

    payload = json.loads(response.body)

    assert "authorization_repository" in payload["details"]["unhealthy_components"]


@pytest.mark.asyncio
async def test_version_endpoint() -> None:
    response = await version_check()

    assert isinstance(
        response,
        VersionResponse,
    )
    assert response.service == "friday-brain"
    assert response.version
    assert response.build_sha
    assert response.environment


@pytest.mark.asyncio
async def test_readiness_includes_component_details() -> None:
    response = await readiness_check(make_request(FakeCompositionRoot()))

    assert isinstance(
        response,
        ReadinessResponse,
    )
    assert response.components["task_repository"].healthy is True
    assert response.components["task_repository"].duration_ms >= 0


class HangingHealthComponent:
    async def is_healthy(self) -> bool:
        await __import__("asyncio").sleep(60)
        return True


@pytest.mark.asyncio
async def test_readiness_times_out_hanging_component(
    monkeypatch,
) -> None:
    from friday_brain.api import routes_health

    root = FakeCompositionRoot()
    root.task_repository = HangingHealthComponent()

    monkeypatch.setattr(
        routes_health.settings,
        "health_check_timeout_sec",
        0.01,
    )

    response = await readiness_check(make_request(root))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503

    payload = json.loads(response.body)

    assert "task_repository" in payload["details"]["unhealthy_components"]
    assert payload["details"]["components"]["task_repository"]["healthy"] is False

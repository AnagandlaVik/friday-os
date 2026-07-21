from friday_brain.composition import CompositionRoot
from friday_brain.config import Settings


def test_postgres_configuration_builds_recovery_worker() -> None:
    root = CompositionRoot(
        app_settings=Settings(
            brain_adapter_task_repository="postgres",
            recovery_enabled=True,
        )
    )

    assert root.get_durable_processor() is not None
    assert root.get_execution_lease_repository() is not None
    assert root.get_recovery_worker() is not None


def test_recovery_worker_can_be_disabled() -> None:
    root = CompositionRoot(
        app_settings=Settings(
            brain_adapter_task_repository="postgres",
            recovery_enabled=False,
        )
    )

    assert root.get_durable_processor() is not None
    assert root.get_recovery_worker() is None


def test_in_memory_configuration_has_no_recovery_worker() -> None:
    root = CompositionRoot(
        app_settings=Settings(
            brain_adapter_task_repository="in_memory",
            recovery_enabled=True,
        )
    )

    assert root.get_durable_processor() is None
    assert root.get_recovery_worker() is None

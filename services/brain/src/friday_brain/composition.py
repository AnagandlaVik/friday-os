from pathlib import Path
from uuid import uuid4

from friday_brain.application.durable_checkpoint_runner import (
    DurableCheckpointRunner,
)
from friday_brain.application.durable_task_processor import (
    DurableTaskProcessor,
)
from friday_brain.observability.metrics import MetricsRegistry
from friday_brain.protocols.execution_lease_repository import (
    ExecutionLeaseRepository,
)
from friday_brain.protocols.execution_plan_repository import (
    ExecutionPlanRepository,
)
from friday_brain.adapters.builtin_tool_handlers import (
    create_builtin_tool_handlers,
)
from friday_brain.adapters.builtin_tools import (
    create_builtin_tool_registry,
)
from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.in_memory_event_bus import InMemoryEventBus
from friday_brain.adapters.in_memory_state_store import InMemoryStateStore
from friday_brain.adapters.in_memory_task_repository import (
    InMemoryTaskRepository,
)
from friday_brain.adapters.jetstream_event_bus import JetStreamEventBus
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
from friday_brain.adapters.placeholder_tool_executor import (
    PlaceholderToolExecutor,
)
from friday_brain.adapters.postgres_execution_lease_repository import (
    PostgresExecutionLeaseRepository,
)
from friday_brain.adapters.postgres_execution_plan_repository import (
    PostgresExecutionPlanRepository,
)
from friday_brain.adapters.postgres_outbox_repository import (
    PostgresOutboxRepository,
)
from friday_brain.adapters.postgres_authorization_repository import (
    PostgresAuthorizationRepository,
)
from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.adapters.postgres_tool_invocation_repository import (
    PostgresToolInvocationRepository,
)
from friday_brain.adapters.redis_state_store import RedisStateStore
from friday_brain.application.outbox_publisher import OutboxPublisher
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.application.recovery_worker import RecoveryWorker
from friday_brain.application.secure_tool_runtime import (
    SecureToolRuntime,
)
from friday_brain.application.tool_registry import ToolRegistry
from friday_brain.config import Settings
from friday_brain.protocols.authorization_repository import (
    AuthorizationRepository,
)
from friday_brain.protocols.event_bus import EventBus
from friday_brain.protocols.outbox_repository import OutboxRepository
from friday_brain.protocols.state_store import StateStore
from friday_brain.protocols.task_repository import TaskRepository
from friday_brain.protocols.tool_invocation_repository import (
    ToolInvocationRepository,
)
from friday_brain.security.filesystem_sandbox import (
    FilesystemSandbox,
)
from friday_brain.security.tool_policy import ToolPolicy


class CompositionRoot:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self._metrics_registry = MetricsRegistry()
        self._tool_policy = ToolPolicy(
            allowed_operations=self.settings.allowed_operations
        )
        self._tool_registry = create_builtin_tool_registry()
        self._filesystem_sandbox: FilesystemSandbox | None = None

        if self.settings.filesystem_tools_enabled:
            sandbox_root = Path(self.settings.filesystem_sandbox_root).expanduser()
            sandbox_root.mkdir(
                mode=0o700,
                parents=True,
                exist_ok=True,
            )
            sandbox_root.chmod(0o700)

            self._filesystem_sandbox = FilesystemSandbox(
                sandbox_root,
                max_read_bytes=(self.settings.filesystem_max_read_bytes),
                max_write_bytes=(self.settings.filesystem_max_write_bytes),
                max_directory_entries=(self.settings.filesystem_max_directory_entries),
            )

        self._worker_id = f"brain-{uuid4()}"

        # Authoritative task repository.
        if self.settings.brain_adapter_task_repository == "postgres":
            self._task_repository: TaskRepository = PostgresTaskRepository(
                postgres_url=self.settings.postgres_url,
                pool_size=self.settings.postgres_pool_size,
                max_overflow=self.settings.postgres_max_overflow,
                pool_timeout=self.settings.postgres_pool_timeout_sec,
                command_timeout=self.settings.postgres_command_timeout_sec,
                subject_prefix=self.settings.nats_subject_prefix,
            )
        else:
            self._task_repository = InMemoryTaskRepository()

        self._execution_lease_repository: ExecutionLeaseRepository | None = None
        self._execution_plan_repository: ExecutionPlanRepository | None = None

        if self.settings.brain_adapter_task_repository == "postgres":
            self._execution_lease_repository = PostgresExecutionLeaseRepository(
                postgres_url=self.settings.postgres_url,
                pool_size=self.settings.postgres_pool_size,
                max_overflow=self.settings.postgres_max_overflow,
                pool_timeout=(self.settings.postgres_pool_timeout_sec),
                command_timeout=(self.settings.postgres_command_timeout_sec),
            )
            self._execution_plan_repository = PostgresExecutionPlanRepository(
                postgres_url=self.settings.postgres_url,
                pool_size=self.settings.postgres_pool_size,
                max_overflow=self.settings.postgres_max_overflow,
                pool_timeout=(self.settings.postgres_pool_timeout_sec),
                command_timeout=(self.settings.postgres_command_timeout_sec),
            )

        self._tool_invocation_repository: ToolInvocationRepository | None = None

        if self.settings.brain_adapter_task_repository == "postgres":
            self._tool_invocation_repository = PostgresToolInvocationRepository(
                postgres_url=self.settings.postgres_url,
                pool_size=self.settings.postgres_pool_size,
                max_overflow=self.settings.postgres_max_overflow,
                pool_timeout=(self.settings.postgres_pool_timeout_sec),
                command_timeout=(self.settings.postgres_command_timeout_sec),
            )

        self._authorization_repository: AuthorizationRepository | None = None

        if self.settings.brain_adapter_task_repository == "postgres":
            self._authorization_repository = PostgresAuthorizationRepository(
                postgres_url=self.settings.postgres_url,
                pool_size=self.settings.postgres_pool_size,
                max_overflow=self.settings.postgres_max_overflow,
                pool_timeout=(self.settings.postgres_pool_timeout_sec),
                command_timeout=(self.settings.postgres_command_timeout_sec),
            )

        self._outbox_repository: OutboxRepository | None = None

        if (
            self.settings.outbox_enabled
            and self.settings.brain_adapter_task_repository == "postgres"
        ):
            self._outbox_repository = PostgresOutboxRepository(
                postgres_url=self.settings.postgres_url,
                pool_size=self.settings.postgres_pool_size,
                max_overflow=self.settings.postgres_max_overflow,
                pool_timeout=self.settings.postgres_pool_timeout_sec,
                command_timeout=self.settings.postgres_command_timeout_sec,
            )

        # Legacy/transient state store remains available during migration.
        if self.settings.brain_adapter_state_store == "redis":
            self._state_store: StateStore = RedisStateStore(
                redis_url=self.settings.redis_url,
                connect_timeout=self.settings.redis_connect_timeout_sec,
                command_timeout=self.settings.redis_command_timeout_sec,
            )
        else:
            self._state_store = InMemoryStateStore()

        # External event transport. Transactional publication will later
        # be performed by the outbox publisher.
        if self.settings.brain_adapter_event_bus == "nats":
            self._event_bus: EventBus = JetStreamEventBus(
                nats_url=self.settings.nats_url,
                connect_timeout=self.settings.nats_connect_timeout_sec,
                publish_timeout=self.settings.nats_publish_timeout_sec,
                max_reconnect_attempts=(self.settings.nats_max_reconnect_attempts),
                stream_name=self.settings.nats_stream_name,
                subject_prefix=self.settings.nats_subject_prefix,
                consumer_name=self.settings.nats_consumer_name,
                max_deliver=self.settings.nats_max_deliver,
                ack_wait=self.settings.nats_ack_wait_sec,
                fetch_timeout=self.settings.nats_fetch_timeout_sec,
                max_ack_pending=self.settings.nats_max_ack_pending,
                drain_timeout=self.settings.nats_drain_timeout_sec,
            )
        else:
            self._event_bus = InMemoryEventBus()

        self._outbox_publisher: OutboxPublisher | None = None

        if self._outbox_repository is not None:
            self._outbox_publisher = OutboxPublisher(
                repository=self._outbox_repository,
                event_bus=self._event_bus,
                batch_size=self.settings.outbox_batch_size,
                poll_interval_sec=self.settings.outbox_poll_interval_sec,
                lock_timeout_sec=self.settings.outbox_lock_timeout_sec,
                max_attempts=self.settings.outbox_max_attempts,
                retry_base_sec=self.settings.outbox_retry_base_sec,
            )

        self._planner = PlaceholderPlanner()
        self._plan_validator = DeterministicPlanValidator(
            allowed_operations=self.settings.allowed_operations,
            max_steps=self.settings.max_plan_steps,
            tool_registry=self._tool_registry,
        )
        self._secure_tool_runtime = SecureToolRuntime(
            registry=self._tool_registry,
            handlers=create_builtin_tool_handlers(self._filesystem_sandbox),
            invocation_repository=(self._tool_invocation_repository),
            authorization_repository=(self._authorization_repository),
            worker_id=self._worker_id,
            reservation_duration_sec=(
                self.settings.tool_invocation_reservation_duration_sec
            ),
            heartbeat_interval_sec=(
                self.settings.tool_invocation_heartbeat_interval_sec
            ),
            metrics_registry=self._metrics_registry,
        )
        self._tool_executor = PlaceholderToolExecutor(
            tool_policy=self.get_tool_policy(),
            tool_registry=self.get_tool_registry(),
            secure_runtime=self._secure_tool_runtime,
        )

        self._durable_processor: DurableTaskProcessor | None = None

        if (
            self._execution_lease_repository is not None
            and self._execution_plan_repository is not None
        ):
            checkpoint_runner = DurableCheckpointRunner(
                plan_repository=self._execution_plan_repository,
                step_executor=self._tool_executor,
                max_attempts=self.settings.execution_max_attempts,
                retry_delay_sec=(self.settings.execution_retry_delay_sec),
            )

            self._durable_processor = DurableTaskProcessor(
                task_repository=self._task_repository,
                lease_repository=(self._execution_lease_repository),
                plan_repository=(self._execution_plan_repository),
                planner=self._planner,
                plan_validator=self._plan_validator,
                checkpoint_runner=checkpoint_runner,
                worker_id=self._worker_id,
                lease_duration_sec=(self.settings.execution_lease_duration_sec),
                heartbeat_interval_sec=(self.settings.execution_heartbeat_interval_sec),
                metrics_registry=self._metrics_registry,
            )

        self._recovery_worker: RecoveryWorker | None = None

        if (
            self.settings.recovery_enabled
            and self._execution_lease_repository is not None
            and self._durable_processor is not None
        ):
            self._recovery_worker = RecoveryWorker(
                lease_repository=(self._execution_lease_repository),
                processor=self._durable_processor,
                poll_interval_sec=(self.settings.recovery_poll_interval_sec),
                batch_size=self.settings.recovery_batch_size,
                max_concurrency=(self.settings.recovery_max_concurrency),
            )

        self._orchestrator = Orchestrator(
            task_repository=self.get_task_repository(),
            planner=self.get_planner(),
            plan_validator=self.get_plan_validator(),
            tool_executor=self.get_tool_executor(),
            durable_processor=self._durable_processor,
        )

    def get_metrics_registry(
        self,
    ) -> MetricsRegistry:
        return self._metrics_registry

    def get_filesystem_sandbox(
        self,
    ) -> FilesystemSandbox | None:
        return self._filesystem_sandbox

    def get_tool_policy(self) -> ToolPolicy:
        return self._tool_policy

    def get_tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def get_task_repository(self) -> TaskRepository:
        return self._task_repository

    def get_authorization_repository(
        self,
    ) -> AuthorizationRepository | None:
        return self._authorization_repository

    def get_tool_invocation_repository(
        self,
    ) -> ToolInvocationRepository | None:
        return self._tool_invocation_repository

    def get_outbox_repository(
        self,
    ) -> OutboxRepository | None:
        return self._outbox_repository

    def get_outbox_publisher(
        self,
    ) -> OutboxPublisher | None:
        return self._outbox_publisher

    def get_execution_lease_repository(
        self,
    ) -> ExecutionLeaseRepository | None:
        return self._execution_lease_repository

    def get_execution_plan_repository(
        self,
    ) -> ExecutionPlanRepository | None:
        return self._execution_plan_repository

    def get_durable_processor(
        self,
    ) -> DurableTaskProcessor | None:
        return self._durable_processor

    def get_recovery_worker(
        self,
    ) -> RecoveryWorker | None:
        return self._recovery_worker

    def get_state_store(self) -> StateStore:
        return self._state_store

    def get_event_bus(self) -> EventBus:
        return self._event_bus

    def get_planner(self) -> PlaceholderPlanner:
        return self._planner

    def get_plan_validator(self) -> DeterministicPlanValidator:
        return self._plan_validator

    def get_tool_executor(self) -> PlaceholderToolExecutor:
        return self._tool_executor

    def get_orchestrator(self) -> Orchestrator:
        return self._orchestrator

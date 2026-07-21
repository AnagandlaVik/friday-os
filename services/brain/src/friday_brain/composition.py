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
from friday_brain.adapters.postgres_outbox_repository import (
    PostgresOutboxRepository,
)
from friday_brain.adapters.postgres_task_repository import (
    PostgresTaskRepository,
)
from friday_brain.adapters.redis_state_store import RedisStateStore
from friday_brain.application.outbox_publisher import OutboxPublisher
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.config import Settings
from friday_brain.protocols.event_bus import EventBus
from friday_brain.protocols.outbox_repository import OutboxRepository
from friday_brain.protocols.state_store import StateStore
from friday_brain.protocols.task_repository import TaskRepository
from friday_brain.security.tool_policy import ToolPolicy


class CompositionRoot:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self._tool_policy = ToolPolicy(
            allowed_operations=self.settings.allowed_operations
        )

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
        )
        self._tool_executor = PlaceholderToolExecutor(
            tool_policy=self.get_tool_policy()
        )

        self._orchestrator = Orchestrator(
            task_repository=self.get_task_repository(),
            planner=self.get_planner(),
            plan_validator=self.get_plan_validator(),
            tool_executor=self.get_tool_executor(),
        )

    def get_tool_policy(self) -> ToolPolicy:
        return self._tool_policy

    def get_task_repository(self) -> TaskRepository:
        return self._task_repository

    def get_outbox_repository(
        self,
    ) -> OutboxRepository | None:
        return self._outbox_repository

    def get_outbox_publisher(
        self,
    ) -> OutboxPublisher | None:
        return self._outbox_publisher

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

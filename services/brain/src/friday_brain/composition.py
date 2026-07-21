from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.in_memory_event_bus import InMemoryEventBus
from friday_brain.adapters.in_memory_state_store import InMemoryStateStore
from friday_brain.adapters.jetstream_event_bus import JetStreamEventBus
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
from friday_brain.adapters.placeholder_tool_executor import PlaceholderToolExecutor
from friday_brain.adapters.redis_state_store import RedisStateStore
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.config import Settings
from friday_brain.protocols.event_bus import EventBus
from friday_brain.protocols.state_store import StateStore
from friday_brain.security.tool_policy import ToolPolicy


class CompositionRoot:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self._tool_policy = ToolPolicy(
            allowed_operations=self.settings.allowed_operations
        )

        # State Store Adapter Selection
        if self.settings.brain_adapter_state_store == "redis":
            self._state_store: StateStore = RedisStateStore(
                redis_url=self.settings.redis_url,
                connect_timeout=self.settings.redis_connect_timeout_sec,
                command_timeout=self.settings.redis_command_timeout_sec,
            )
        else:  # Default to in_memory
            self._state_store = InMemoryStateStore()

        # Event Bus Adapter Selection
        if self.settings.brain_adapter_event_bus == "nats":
            self._event_bus: EventBus = JetStreamEventBus(
                nats_url=self.settings.nats_url,
                connect_timeout=self.settings.nats_connect_timeout_sec,
                publish_timeout=self.settings.nats_publish_timeout_sec,
                max_reconnect_attempts=self.settings.nats_max_reconnect_attempts,
                stream_name=self.settings.nats_stream_name,
                subject_prefix=self.settings.nats_subject_prefix,
                consumer_name=self.settings.nats_consumer_name,
                max_deliver=self.settings.nats_max_deliver,
                ack_wait=self.settings.nats_ack_wait_sec,
                fetch_timeout=self.settings.nats_fetch_timeout_sec,
                max_ack_pending=self.settings.nats_max_ack_pending,
                drain_timeout=self.settings.nats_drain_timeout_sec,
            )
        else:  # Default to in_memory
            self._event_bus = InMemoryEventBus()

        self._planner = PlaceholderPlanner()
        self._plan_validator = DeterministicPlanValidator(
            allowed_operations=self.settings.allowed_operations,
            max_steps=self.settings.max_plan_steps,
        )
        self._tool_executor = PlaceholderToolExecutor(
            tool_policy=self.get_tool_policy()
        )
        self._orchestrator = Orchestrator(
            state_store=self.get_state_store(),
            event_bus=self.get_event_bus(),
            planner=self.get_planner(),
            plan_validator=self.get_plan_validator(),
            tool_executor=self.get_tool_executor(),
        )

    def get_tool_policy(self) -> ToolPolicy:
        return self._tool_policy

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

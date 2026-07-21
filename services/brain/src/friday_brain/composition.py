from friday_brain.adapters.deterministic_plan_validator import (
    DeterministicPlanValidator,
)
from friday_brain.adapters.in_memory_event_bus import InMemoryEventBus
from friday_brain.adapters.in_memory_state_store import InMemoryStateStore
from friday_brain.adapters.placeholder_planner import PlaceholderPlanner
from friday_brain.adapters.placeholder_tool_executor import PlaceholderToolExecutor
from friday_brain.application.orchestrator import Orchestrator
from friday_brain.config import Settings
from friday_brain.security.tool_policy import ToolPolicy


class CompositionRoot:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self._tool_policy = ToolPolicy(
            allowed_operations=self.settings.allowed_operations
        )
        self._state_store = InMemoryStateStore()
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

    def get_state_store(self) -> InMemoryStateStore:
        return self._state_store

    def get_event_bus(self) -> InMemoryEventBus:
        return self._event_bus

    def get_planner(self) -> PlaceholderPlanner:
        return self._planner

    def get_plan_validator(self) -> DeterministicPlanValidator:
        return self._plan_validator

    def get_tool_executor(self) -> PlaceholderToolExecutor:
        return self._tool_executor

    def get_orchestrator(self) -> Orchestrator:
        return self._orchestrator

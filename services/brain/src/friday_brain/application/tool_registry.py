from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolPermission,
)


class ToolRegistryError(RuntimeError):
    """Base error for tool-registry operations."""


class DuplicateToolRegistrationError(ToolRegistryError):
    """Raised when a tool name is registered twice."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' is already registered.")


class ToolNotRegisteredError(ToolRegistryError):
    """Raised when a requested tool does not exist."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' is not registered.")


@dataclass(frozen=True, slots=True)
class ToolPermissionDeniedError(ToolRegistryError):
    """Raised when required permissions are missing."""

    tool_name: str
    missing_permissions: frozenset[ToolPermission]

    def __str__(self) -> str:
        missing = ", ".join(
            sorted(permission.value for permission in self.missing_permissions)
        )

        return f"Tool '{self.tool_name}' requires missing permissions: {missing}."


class ToolConfirmationRequiredError(ToolRegistryError):
    """Raised when a tool requires explicit confirmation."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' requires explicit confirmation.")


class ToolRegistry:
    """In-process registry of immutable tool definitions."""

    def __init__(
        self,
        definitions: Iterable[ToolDefinition] = (),
    ) -> None:
        self._definitions: dict[
            str,
            ToolDefinition,
        ] = {}

        for definition in definitions:
            self.register(definition)

    def register(
        self,
        definition: ToolDefinition,
    ) -> None:
        if definition.name in self._definitions:
            raise DuplicateToolRegistrationError(definition.name)

        self._definitions[definition.name] = definition

    def get(
        self,
        tool_name: str,
    ) -> ToolDefinition:
        try:
            return self._definitions[tool_name]
        except KeyError as exc:
            raise ToolNotRegisteredError(tool_name) from exc

    def list_definitions(
        self,
    ) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def validate_arguments(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> BaseModel:
        definition = self.get(tool_name)

        return definition.input_model.model_validate(arguments)

    def validate_access(
        self,
        tool_name: str,
        *,
        granted_permissions: frozenset[ToolPermission] = frozenset(),
        confirmation_granted: bool = False,
    ) -> ToolDefinition:
        definition = self.get(tool_name)

        missing_permissions = definition.permissions - granted_permissions

        if missing_permissions:
            raise ToolPermissionDeniedError(
                tool_name=tool_name,
                missing_permissions=(missing_permissions),
            )

        if definition.requires_confirmation and not confirmation_granted:
            raise ToolConfirmationRequiredError(tool_name)

        return definition

    def __contains__(
        self,
        tool_name: object,
    ) -> bool:
        return isinstance(tool_name, str) and tool_name in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)

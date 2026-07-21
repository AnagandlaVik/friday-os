from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from friday_brain.contracts.tools import ToolPermission


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Security and durability context for one tool call."""

    task_id: UUID
    checkpoint_id: UUID
    idempotency_key: str
    granted_permissions: frozenset[ToolPermission]
    confirmation_granted: bool


class ToolHandler(Protocol):
    """Implementation boundary for one registered tool."""

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any: ...

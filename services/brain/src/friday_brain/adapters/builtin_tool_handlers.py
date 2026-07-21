from typing import Any

from pydantic import BaseModel

from friday_brain.adapters.builtin_tools import EchoInput
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
    ToolHandler,
)


class EchoToolHandler:
    """Side-effect-free implementation of the built-in echo tool."""

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context

        if not isinstance(arguments, EchoInput):
            raise TypeError("Echo handler received an unexpected input schema.")

        return {
            "result": f"Echo: {arguments.message}",
        }


def create_builtin_tool_handlers() -> dict[str, ToolHandler]:
    """Create implementations for built-in registered tools."""

    return {
        "echo": EchoToolHandler(),
    }

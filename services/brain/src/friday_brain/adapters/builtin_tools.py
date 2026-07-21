from pydantic import BaseModel, ConfigDict, Field

from friday_brain.application.tool_registry import ToolRegistry
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolRetryPolicy,
    ToolRiskLevel,
)


class EchoInput(BaseModel):
    """Validated arguments for the harmless echo tool."""

    message: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class EchoOutput(BaseModel):
    """Validated result from the echo tool."""

    result: str

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )


def create_builtin_tool_registry() -> ToolRegistry:
    """Create the registry of tools built into Brain Core."""

    return ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description=(
                    "Return the supplied message without "
                    "performing external side effects."
                ),
                input_model=EchoInput,
                output_model=EchoOutput,
                risk_level=ToolRiskLevel.LOW,
                permissions=frozenset(),
                requires_confirmation=False,
                timeout_sec=5.0,
                retry_policy=ToolRetryPolicy(
                    max_attempts=1,
                    base_delay_sec=0.0,
                    max_delay_sec=0.0,
                ),
            )
        ]
    )

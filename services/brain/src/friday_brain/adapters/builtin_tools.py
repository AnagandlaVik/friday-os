from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from friday_brain.application.tool_registry import (
    ToolRegistry,
)
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolPermission,
    ToolRiskLevel,
    ToolRetryPolicy,
)


class EchoInput(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=10_000,
    )

    model_config = ConfigDict(extra="forbid")


class EchoOutput(BaseModel):
    result: str

    model_config = ConfigDict(extra="forbid")


class FilesystemReadTextInput(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=4_096,
    )

    model_config = ConfigDict(extra="forbid")


class FilesystemReadTextOutput(BaseModel):
    path: str
    content: str
    size_bytes: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class FilesystemListDirectoryInput(BaseModel):
    path: str = Field(
        default=".",
        min_length=1,
        max_length=4_096,
    )

    model_config = ConfigDict(extra="forbid")


class FilesystemDirectoryEntryOutput(BaseModel):
    name: str
    relative_path: str
    kind: Literal[
        "file",
        "directory",
        "symlink",
        "other",
    ]
    size_bytes: int | None = Field(
        default=None,
        ge=0,
    )

    model_config = ConfigDict(extra="forbid")


class FilesystemListDirectoryOutput(BaseModel):
    path: str
    entries: list[FilesystemDirectoryEntryOutput]

    model_config = ConfigDict(extra="forbid")


class FilesystemWriteTextInput(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=4_096,
    )
    content: str = Field(
        max_length=1_000_000,
    )
    overwrite: bool = False

    model_config = ConfigDict(extra="forbid")


class FilesystemWriteTextOutput(BaseModel):
    path: str
    bytes_written: int = Field(ge=0)
    overwritten: bool

    model_config = ConfigDict(extra="forbid")


def create_builtin_tool_registry() -> ToolRegistry:
    """Create the registry of trusted built-in tools."""

    retry_once = ToolRetryPolicy(
        max_attempts=1,
    )

    return ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description=(
                    "Return the supplied message without external side effects."
                ),
                input_model=EchoInput,
                output_model=EchoOutput,
                risk_level=ToolRiskLevel.LOW,
                timeout_sec=5.0,
                retry_policy=retry_once,
            ),
            ToolDefinition(
                name="filesystem.read_text",
                description=(
                    "Read one UTF-8 text file inside the configured filesystem sandbox."
                ),
                input_model=FilesystemReadTextInput,
                output_model=FilesystemReadTextOutput,
                risk_level=ToolRiskLevel.MEDIUM,
                permissions=frozenset(
                    {
                        ToolPermission.READ_DATA,
                        ToolPermission.FILESYSTEM_ACCESS,
                    }
                ),
                timeout_sec=10.0,
                retry_policy=retry_once,
            ),
            ToolDefinition(
                name="filesystem.list_directory",
                description=(
                    "List bounded metadata for one "
                    "directory inside the filesystem sandbox."
                ),
                input_model=(FilesystemListDirectoryInput),
                output_model=(FilesystemListDirectoryOutput),
                risk_level=ToolRiskLevel.MEDIUM,
                permissions=frozenset(
                    {
                        ToolPermission.READ_DATA,
                        ToolPermission.FILESYSTEM_ACCESS,
                    }
                ),
                timeout_sec=10.0,
                retry_policy=retry_once,
            ),
            ToolDefinition(
                name="filesystem.write_text",
                description=(
                    "Atomically write UTF-8 text inside "
                    "the configured filesystem sandbox."
                ),
                input_model=FilesystemWriteTextInput,
                output_model=FilesystemWriteTextOutput,
                risk_level=ToolRiskLevel.HIGH,
                permissions=frozenset(
                    {
                        ToolPermission.WRITE_DATA,
                        ToolPermission.FILESYSTEM_ACCESS,
                    }
                ),
                requires_confirmation=True,
                timeout_sec=10.0,
                retry_policy=retry_once,
            ),
        ]
    )

import asyncio
from typing import Any

from pydantic import BaseModel

from friday_brain.adapters.builtin_tools import (
    EchoInput,
    FilesystemListDirectoryInput,
    FilesystemReadTextInput,
    FilesystemWriteTextInput,
)
from friday_brain.application.secure_tool_runtime import (
    ToolHandlerError,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
    ToolHandler,
)
from friday_brain.security.filesystem_sandbox import (
    FilesystemEncodingError,
    FilesystemLimitError,
    FilesystemPathError,
    FilesystemSandbox,
    FilesystemSandboxError,
)


class EchoToolHandler:
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


class FilesystemReadTextHandler:
    def __init__(
        self,
        sandbox: FilesystemSandbox,
    ) -> None:
        self._sandbox = sandbox

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context

        if not isinstance(
            arguments,
            FilesystemReadTextInput,
        ):
            raise TypeError(
                "Filesystem read handler received an unexpected input schema."
            )

        try:
            content = await asyncio.to_thread(
                self._sandbox.read_text,
                arguments.path,
            )
        except FilesystemSandboxError as error:
            raise _tool_error(error) from error

        return {
            "path": arguments.path,
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
        }


class FilesystemListDirectoryHandler:
    def __init__(
        self,
        sandbox: FilesystemSandbox,
    ) -> None:
        self._sandbox = sandbox

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context

        if not isinstance(
            arguments,
            FilesystemListDirectoryInput,
        ):
            raise TypeError(
                "Filesystem listing handler received an unexpected input schema."
            )

        try:
            entries = await asyncio.to_thread(
                self._sandbox.list_directory,
                arguments.path,
            )
        except FilesystemSandboxError as error:
            raise _tool_error(error) from error

        return {
            "path": arguments.path,
            "entries": [
                {
                    "name": entry.name,
                    "relative_path": (entry.relative_path),
                    "kind": entry.kind,
                    "size_bytes": entry.size_bytes,
                }
                for entry in entries
            ],
        }


class FilesystemWriteTextHandler:
    def __init__(
        self,
        sandbox: FilesystemSandbox,
    ) -> None:
        self._sandbox = sandbox

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        del context

        if not isinstance(
            arguments,
            FilesystemWriteTextInput,
        ):
            raise TypeError(
                "Filesystem write handler received an unexpected input schema."
            )

        target_existed = False

        try:
            target = self._sandbox.resolve_write_target(arguments.path)
            target_existed = target.exists()

            bytes_written = await asyncio.to_thread(
                self._sandbox.write_text,
                arguments.path,
                arguments.content,
                overwrite=arguments.overwrite,
            )
        except FilesystemSandboxError as error:
            raise _tool_error(error) from error

        return {
            "path": arguments.path,
            "bytes_written": bytes_written,
            "overwritten": target_existed,
        }


def create_builtin_tool_handlers(
    filesystem_sandbox: (FilesystemSandbox | None) = None,
) -> dict[str, ToolHandler]:
    """
    Create handlers for available built-in capabilities.

    Filesystem handlers are only exposed when an explicit sandbox
    instance is supplied.
    """

    handlers: dict[str, ToolHandler] = {
        "echo": EchoToolHandler(),
    }

    if filesystem_sandbox is not None:
        handlers.update(
            {
                "filesystem.read_text": (FilesystemReadTextHandler(filesystem_sandbox)),
                "filesystem.list_directory": (
                    FilesystemListDirectoryHandler(filesystem_sandbox)
                ),
                "filesystem.write_text": (
                    FilesystemWriteTextHandler(filesystem_sandbox)
                ),
            }
        )

    return handlers


def _tool_error(
    error: FilesystemSandboxError,
) -> ToolHandlerError:
    if isinstance(error, FilesystemPathError):
        return ToolHandlerError(
            code="filesystem_path_invalid",
            message=str(error),
            retryable=False,
        )

    if isinstance(error, FilesystemLimitError):
        return ToolHandlerError(
            code="filesystem_limit_exceeded",
            message=str(error),
            retryable=False,
        )

    if isinstance(
        error,
        FilesystemEncodingError,
    ):
        return ToolHandlerError(
            code="filesystem_encoding_invalid",
            message=str(error),
            retryable=False,
        )

    return ToolHandlerError(
        code="filesystem_operation_failed",
        message="Filesystem operation failed.",
        retryable=False,
        details={
            "exception_type": type(error).__name__,
        },
    )

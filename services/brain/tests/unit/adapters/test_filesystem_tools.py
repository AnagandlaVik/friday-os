from pathlib import Path
from uuid import uuid4

import pytest

from friday_brain.adapters.builtin_tool_handlers import (
    create_builtin_tool_handlers,
)
from friday_brain.adapters.builtin_tools import (
    FilesystemListDirectoryInput,
    FilesystemReadTextInput,
    FilesystemWriteTextInput,
    create_builtin_tool_registry,
)
from friday_brain.application.secure_tool_runtime import (
    ToolHandlerError,
)
from friday_brain.contracts.tools import (
    ToolPermission,
    ToolRiskLevel,
)
from friday_brain.protocols.tool_handler import (
    ToolExecutionContext,
)
from friday_brain.security.filesystem_sandbox import (
    FilesystemSandbox,
)


def make_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=uuid4(),
        checkpoint_id=uuid4(),
        idempotency_key="filesystem-test",
        granted_permissions=frozenset(
            {
                ToolPermission.READ_DATA,
                ToolPermission.WRITE_DATA,
                ToolPermission.FILESYSTEM_ACCESS,
            }
        ),
        confirmation_granted=True,
    )


def make_handlers(
    tmp_path: Path,
):
    root = tmp_path / "sandbox"
    root.mkdir()

    sandbox = FilesystemSandbox(root)

    return (
        sandbox,
        create_builtin_tool_handlers(sandbox),
    )


def test_filesystem_definitions_have_secure_policy() -> None:
    registry = create_builtin_tool_registry()

    read_definition = registry.get("filesystem.read_text")
    list_definition = registry.get("filesystem.list_directory")
    write_definition = registry.get("filesystem.write_text")

    read_permissions = frozenset(
        {
            ToolPermission.READ_DATA,
            ToolPermission.FILESYSTEM_ACCESS,
        }
    )

    assert read_definition.permissions == read_permissions
    assert list_definition.permissions == read_permissions
    assert write_definition.permissions == frozenset(
        {
            ToolPermission.WRITE_DATA,
            ToolPermission.FILESYSTEM_ACCESS,
        }
    )
    assert write_definition.requires_confirmation is True
    assert write_definition.risk_level == ToolRiskLevel.HIGH


def test_filesystem_handlers_require_explicit_sandbox() -> None:
    handlers = create_builtin_tool_handlers()

    assert set(handlers) == {"echo"}


@pytest.mark.asyncio
async def test_read_text_handler(
    tmp_path: Path,
) -> None:
    sandbox, handlers = make_handlers(tmp_path)
    (sandbox.root / "hello.txt").write_text(
        "hello 🌎",
        encoding="utf-8",
    )

    result = await handlers["filesystem.read_text"].execute(
        FilesystemReadTextInput(path="hello.txt"),
        make_context(),
    )

    assert result == {
        "path": "hello.txt",
        "content": "hello 🌎",
        "size_bytes": len("hello 🌎".encode("utf-8")),
    }


@pytest.mark.asyncio
async def test_list_directory_handler(
    tmp_path: Path,
) -> None:
    sandbox, handlers = make_handlers(tmp_path)
    (sandbox.root / "alpha.txt").write_text(
        "alpha",
        encoding="utf-8",
    )
    (sandbox.root / "folder").mkdir()

    result = await handlers["filesystem.list_directory"].execute(
        FilesystemListDirectoryInput(path="."),
        make_context(),
    )

    entries = {entry["name"]: entry for entry in result["entries"]}

    assert entries["alpha.txt"]["kind"] == "file"
    assert entries["alpha.txt"]["size_bytes"] == 5
    assert entries["folder"]["kind"] == "directory"


@pytest.mark.asyncio
async def test_write_text_handler(
    tmp_path: Path,
) -> None:
    sandbox, handlers = make_handlers(tmp_path)

    result = await handlers["filesystem.write_text"].execute(
        FilesystemWriteTextInput(
            path="created.txt",
            content="created",
        ),
        make_context(),
    )

    assert result == {
        "path": "created.txt",
        "bytes_written": 7,
        "overwritten": False,
    }
    assert (sandbox.root / "created.txt").read_text(encoding="utf-8") == "created"


@pytest.mark.asyncio
async def test_write_reports_overwrite(
    tmp_path: Path,
) -> None:
    sandbox, handlers = make_handlers(tmp_path)
    target = sandbox.root / "existing.txt"
    target.write_text(
        "before",
        encoding="utf-8",
    )

    result = await handlers["filesystem.write_text"].execute(
        FilesystemWriteTextInput(
            path="existing.txt",
            content="after",
            overwrite=True,
        ),
        make_context(),
    )

    assert result["overwritten"] is True
    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_handler_maps_traversal_to_structured_error(
    tmp_path: Path,
) -> None:
    _, handlers = make_handlers(tmp_path)

    with pytest.raises(ToolHandlerError) as captured:
        await handlers["filesystem.read_text"].execute(
            FilesystemReadTextInput(path="../outside.txt"),
            make_context(),
        )

    assert captured.value.code == "filesystem_path_invalid"
    assert captured.value.retryable is False

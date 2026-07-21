import stat
from pathlib import Path

import pytest

from friday_brain.composition import CompositionRoot
from friday_brain.config import settings
from friday_brain.security.filesystem_sandbox import (
    FilesystemLimitError,
)


def test_filesystem_tools_are_disabled_by_default(
    tmp_path: Path,
) -> None:
    sandbox_root = tmp_path / "disabled"

    test_settings = settings.model_copy(
        update={
            "filesystem_tools_enabled": False,
            "filesystem_sandbox_root": str(sandbox_root),
        }
    )

    root = CompositionRoot(app_settings=test_settings)

    assert root.get_filesystem_sandbox() is None
    assert not sandbox_root.exists()


def test_enabled_filesystem_tools_create_private_sandbox(
    tmp_path: Path,
) -> None:
    sandbox_root = tmp_path / "enabled"

    test_settings = settings.model_copy(
        update={
            "filesystem_tools_enabled": True,
            "filesystem_sandbox_root": str(sandbox_root),
            "filesystem_max_read_bytes": 100,
            "filesystem_max_write_bytes": 100,
            "filesystem_max_directory_entries": 10,
        }
    )

    root = CompositionRoot(app_settings=test_settings)
    sandbox = root.get_filesystem_sandbox()

    assert sandbox is not None
    assert sandbox.root == sandbox_root.resolve()
    assert sandbox.root.is_dir()

    permissions = stat.S_IMODE(sandbox.root.stat().st_mode)
    assert permissions == 0o700


def test_composition_applies_filesystem_limits(
    tmp_path: Path,
) -> None:
    sandbox_root = tmp_path / "limited"

    test_settings = settings.model_copy(
        update={
            "filesystem_tools_enabled": True,
            "filesystem_sandbox_root": str(sandbox_root),
            "filesystem_max_read_bytes": 4,
            "filesystem_max_write_bytes": 4,
            "filesystem_max_directory_entries": 1,
        }
    )

    root = CompositionRoot(app_settings=test_settings)
    sandbox = root.get_filesystem_sandbox()

    assert sandbox is not None

    large_file = sandbox.root / "large.txt"
    large_file.write_text(
        "12345",
        encoding="utf-8",
    )

    with pytest.raises(FilesystemLimitError):
        sandbox.read_text("large.txt")

    with pytest.raises(FilesystemLimitError):
        sandbox.write_text(
            "write.txt",
            "12345",
        )

    (sandbox.root / "one.txt").write_text(
        "1",
        encoding="utf-8",
    )
    (sandbox.root / "two.txt").write_text(
        "2",
        encoding="utf-8",
    )

    with pytest.raises(FilesystemLimitError):
        sandbox.list_directory(".")

from pathlib import Path

import pytest

from friday_brain.security.filesystem_sandbox import (
    FilesystemEncodingError,
    FilesystemLimitError,
    FilesystemPathError,
    FilesystemSandbox,
)


def make_sandbox(
    tmp_path: Path,
    **kwargs,
) -> FilesystemSandbox:
    root = tmp_path / "sandbox"
    root.mkdir()

    return FilesystemSandbox(
        root,
        **kwargs,
    )


def test_reads_utf8_text_inside_root(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    path = sandbox.root / "hello.txt"
    path.write_text(
        "hello 🌎",
        encoding="utf-8",
    )

    assert sandbox.read_text("hello.txt") == "hello 🌎"


def test_rejects_absolute_paths(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(
        FilesystemPathError,
        match="Absolute paths",
    ):
        sandbox.read_text(str(tmp_path / "outside.txt"))


def test_rejects_parent_traversal(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(
        FilesystemPathError,
        match="traversal",
    ):
        sandbox.read_text("../outside.txt")


def test_rejects_symlink_escape_for_read(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(
        "secret",
        encoding="utf-8",
    )
    link = sandbox.root / "escape.txt"
    link.symlink_to(outside)

    with pytest.raises(
        FilesystemPathError,
        match="escapes",
    ):
        sandbox.read_text("escape.txt")


def test_rejects_symlink_in_write_path(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = sandbox.root / "linked"
    link.symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(
        FilesystemPathError,
        match="Symbolic links",
    ):
        sandbox.write_text(
            "linked/file.txt",
            "blocked",
        )

    assert not (outside / "file.txt").exists()


def test_rejects_oversized_read(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(
        tmp_path,
        max_read_bytes=4,
    )
    path = sandbox.root / "large.txt"
    path.write_text(
        "12345",
        encoding="utf-8",
    )

    with pytest.raises(
        FilesystemLimitError,
        match="read size",
    ):
        sandbox.read_text("large.txt")


def test_rejects_invalid_utf8(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    path = sandbox.root / "binary.dat"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(
        FilesystemEncodingError,
        match="UTF-8",
    ):
        sandbox.read_text("binary.dat")


def test_directory_listing_is_bounded(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(
        tmp_path,
        max_directory_entries=2,
    )

    for name in ("a.txt", "b.txt", "c.txt"):
        (sandbox.root / name).write_text(
            name,
            encoding="utf-8",
        )

    with pytest.raises(
        FilesystemLimitError,
        match="more entries",
    ):
        sandbox.list_directory(".")


def test_directory_listing_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(
        "outside",
        encoding="utf-8",
    )
    (sandbox.root / "link.txt").symlink_to(outside)
    (sandbox.root / "regular.txt").write_text(
        "inside",
        encoding="utf-8",
    )

    entries = sandbox.list_directory(".")

    by_name = {entry.name: entry for entry in entries}

    assert by_name["link.txt"].kind == "symlink"
    assert by_name["link.txt"].size_bytes is None
    assert by_name["regular.txt"].kind == "file"


def test_atomic_write_requires_explicit_overwrite(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    target = sandbox.root / "note.txt"
    target.write_text(
        "original",
        encoding="utf-8",
    )

    with pytest.raises(
        FilesystemPathError,
        match="overwrite",
    ):
        sandbox.write_text(
            "note.txt",
            "changed",
        )

    assert target.read_text(encoding="utf-8") == "original"


def test_atomic_write_replaces_content(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)
    target = sandbox.root / "note.txt"
    target.write_text(
        "original",
        encoding="utf-8",
    )

    written = sandbox.write_text(
        "note.txt",
        "replacement 🌎",
        overwrite=True,
    )

    assert written == len("replacement 🌎".encode("utf-8"))
    assert target.read_text(encoding="utf-8") == "replacement 🌎"
    assert not list(sandbox.root.glob(".friday-write-*.tmp"))


def test_write_requires_existing_parent(
    tmp_path: Path,
) -> None:
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(
        FilesystemPathError,
        match="parent directory",
    ):
        sandbox.write_text(
            "missing/file.txt",
            "hello",
        )

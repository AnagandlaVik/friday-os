import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class FilesystemSandboxError(RuntimeError):
    """Base error for sandboxed filesystem operations."""


class FilesystemPathError(FilesystemSandboxError):
    """A path is invalid or escapes the configured sandbox."""


class FilesystemLimitError(FilesystemSandboxError):
    """A configured filesystem safety limit was exceeded."""


class FilesystemEncodingError(FilesystemSandboxError):
    """A file is not valid UTF-8 text."""


FilesystemEntryKind = Literal[
    "file",
    "directory",
    "symlink",
    "other",
]


@dataclass(frozen=True, slots=True)
class FilesystemEntry:
    """Safe directory-entry metadata."""

    name: str
    relative_path: str
    kind: FilesystemEntryKind
    size_bytes: int | None


class FilesystemSandbox:
    """
    Filesystem capability restricted to one resolved root directory.

    The sandbox only handles UTF-8 text, rejects traversal and external
    symlinks, enforces size limits, and performs atomic file replacement.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 1_000_000,
        max_directory_entries: int = 1_000,
    ) -> None:
        if max_read_bytes < 1:
            raise ValueError("Maximum read size must be positive.")

        if max_write_bytes < 1:
            raise ValueError("Maximum write size must be positive.")

        if max_directory_entries < 1:
            raise ValueError("Maximum directory entries must be positive.")

        resolved_root = root.expanduser().resolve(strict=True)

        if not resolved_root.is_dir():
            raise ValueError("Filesystem sandbox root must be a directory.")

        self._root = resolved_root
        self._max_read_bytes = max_read_bytes
        self._max_write_bytes = max_write_bytes
        self._max_directory_entries = max_directory_entries

    @property
    def root(self) -> Path:
        return self._root

    def read_text(
        self,
        relative_path: str,
    ) -> str:
        path = self.resolve_existing(relative_path)

        if not path.is_file():
            raise FilesystemPathError("Requested path is not a regular file.")

        size = path.stat().st_size

        if size > self._max_read_bytes:
            raise FilesystemLimitError(
                "Requested file exceeds the maximum allowed read size."
            )

        data = path.read_bytes()

        if len(data) > self._max_read_bytes:
            raise FilesystemLimitError(
                "Requested file exceeds the maximum allowed read size."
            )

        try:
            return data.decode(
                "utf-8",
                errors="strict",
            )
        except UnicodeDecodeError as error:
            raise FilesystemEncodingError(
                "Requested file is not valid UTF-8 text."
            ) from error

    def list_directory(
        self,
        relative_path: str = ".",
    ) -> list[FilesystemEntry]:
        directory = self.resolve_existing(relative_path)

        if not directory.is_dir():
            raise FilesystemPathError("Requested path is not a directory.")

        entries: list[FilesystemEntry] = []

        with os.scandir(directory) as iterator:
            for item in iterator:
                if len(entries) >= self._max_directory_entries:
                    raise FilesystemLimitError(
                        "Directory contains more entries than the configured limit."
                    )

                item_path = Path(item.path)

                if item.is_symlink():
                    kind: FilesystemEntryKind = "symlink"
                    size_bytes = None
                elif item.is_file(follow_symlinks=False):
                    kind = "file"
                    size_bytes = item.stat(follow_symlinks=False).st_size
                elif item.is_dir(follow_symlinks=False):
                    kind = "directory"
                    size_bytes = None
                else:
                    kind = "other"
                    size_bytes = None

                entries.append(
                    FilesystemEntry(
                        name=item.name,
                        relative_path=(item_path.relative_to(self._root).as_posix()),
                        kind=kind,
                        size_bytes=size_bytes,
                    )
                )

        return sorted(
            entries,
            key=lambda entry: (
                entry.kind,
                entry.name.casefold(),
                entry.name,
            ),
        )

    def write_text(
        self,
        relative_path: str,
        content: str,
        *,
        overwrite: bool = False,
    ) -> int:
        data = content.encode(
            "utf-8",
            errors="strict",
        )

        if len(data) > self._max_write_bytes:
            raise FilesystemLimitError(
                "Content exceeds the maximum allowed write size."
            )

        target = self.resolve_write_target(relative_path)

        if target.exists() and not overwrite:
            raise FilesystemPathError(
                "Target already exists and overwrite was not explicitly enabled."
            )

        if target.exists() and not target.is_file():
            raise FilesystemPathError("Write target is not a regular file.")

        temporary_path: Path | None = None

        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".friday-write-",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary_path = Path(temporary_name)

            with os.fdopen(
                file_descriptor,
                "wb",
            ) as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())

            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            temporary_path = None

            self._fsync_directory(target.parent)

            return len(data)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def resolve_existing(
        self,
        relative_path: str,
    ) -> Path:
        candidate = self._candidate(relative_path)

        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise FilesystemPathError("Requested path does not exist.") from error

        self._ensure_inside_root(resolved)

        return resolved

    def resolve_write_target(
        self,
        relative_path: str,
    ) -> Path:
        candidate = self._candidate(relative_path)

        if candidate == self._root:
            raise FilesystemPathError(
                "The sandbox root cannot be used as a file target."
            )

        self._reject_symlink_components(candidate)

        try:
            parent = candidate.parent.resolve(strict=True)
        except FileNotFoundError as error:
            raise FilesystemPathError(
                "The target parent directory does not exist."
            ) from error

        self._ensure_inside_root(parent)

        if not parent.is_dir():
            raise FilesystemPathError("The target parent is not a directory.")

        if candidate.is_symlink():
            raise FilesystemPathError("Writing through a symbolic link is not allowed.")

        if candidate.exists():
            resolved = candidate.resolve(strict=True)
            self._ensure_inside_root(resolved)

        return candidate

    def _candidate(
        self,
        relative_path: str,
    ) -> Path:
        if "\x00" in relative_path:
            raise FilesystemPathError("Paths cannot contain null bytes.")

        supplied = Path(relative_path)

        if supplied.is_absolute():
            raise FilesystemPathError("Absolute paths are not allowed.")

        if any(part == ".." for part in supplied.parts):
            raise FilesystemPathError("Parent-directory traversal is not allowed.")

        return self._root / supplied

    def _reject_symlink_components(
        self,
        candidate: Path,
    ) -> None:
        relative = candidate.relative_to(self._root)
        current = self._root

        for part in relative.parts:
            current = current / part

            if (current.exists() or current.is_symlink()) and current.is_symlink():
                raise FilesystemPathError(
                    "Symbolic links cannot be used in write paths."
                )

    def _ensure_inside_root(
        self,
        path: Path,
    ) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as error:
            raise FilesystemPathError(
                "Requested path escapes the filesystem sandbox."
            ) from error

    @staticmethod
    def _fsync_directory(
        directory: Path,
    ) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY,
        )

        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

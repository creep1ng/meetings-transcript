"""Root-confined local source browser."""

from pathlib import Path
from typing import Any

from study.application.dto import SourceDirectoryDTO, SourceEntryDTO
from study.domain.errors import (
    InvalidLocationError,
    LibrarySubtreeError,
    LocationNotDirectoryError,
    LocationNotFoundError,
    RootEscapeError,
)
from study.domain.policies import (
    classify_asset,
    is_hidden_name,
    is_incomplete_name,
)
from study.domain.value_objects import RelativeLocation, name_collision_key


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _existing_directory(value: Any, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise LocationNotFoundError(f"{label} does not exist: {value}") from error
    if not path.is_dir():
        raise LocationNotDirectoryError(f"{label} is not a directory: {value}")
    return path


class FilesystemSourceRepository:
    """Browse relative identifiers while resolving every path for safety."""

    def __init__(
        self,
        source_root: Any,
        library_root: Any,
        *,
        start_path: Any | None = None,
        show_hidden: bool = False,
        include_incomplete: bool = False,
    ) -> None:
        self._root = _existing_directory(source_root, "source root")
        self._library_root = _existing_directory(library_root, "library root")
        if _is_within(self._root, self._library_root):
            raise LibrarySubtreeError("source root cannot be inside the library")
        self._show_hidden = show_hidden
        self._include_incomplete = include_incomplete
        start = self._root if start_path is None else self._resolve_initial(start_path)
        self._ensure_source_path(start)
        if not start.is_dir():
            raise LocationNotDirectoryError(
                f"start path is not a directory: {start_path}"
            )
        self._start_location = start.relative_to(self._root).as_posix()
        if self._start_location == ".":
            self._start_location = ""

    @property
    def start_location(self) -> str:
        return self._start_location

    def browse(self, location: str = "") -> SourceDirectoryDTO:
        relative = self._relative_location(location)
        directory = self._resolve(relative)
        if not directory.is_dir():
            raise LocationNotDirectoryError(
                f"source location is not a directory: {location}"
            )

        entries: list[SourceEntryDTO] = []
        for entry in directory.iterdir():
            if not self._show_hidden and is_hidden_name(entry.name):
                continue
            if not self._include_incomplete and is_incomplete_name(entry.name):
                continue
            try:
                resolved = entry.resolve(strict=True)
            except (FileNotFoundError, RuntimeError):
                continue
            if not _is_within(resolved, self._root):
                continue
            if _is_within(resolved, self._library_root):
                continue
            if resolved.is_dir():
                entries.append(
                    SourceEntryDTO(
                        location=relative.child(entry.name).value,
                        name=entry.name,
                        is_directory=True,
                    )
                )
            elif resolved.is_file():
                entries.append(
                    SourceEntryDTO(
                        location=relative.child(entry.name).value,
                        name=entry.name,
                        is_directory=False,
                        size_bytes=resolved.stat().st_size,
                        asset_kind=classify_asset(entry.name).value,
                    )
                )
        entries.sort(
            key=lambda item: (
                not item.is_directory,
                name_collision_key(item.name),
                item.name,
            )
        )
        return SourceDirectoryDTO(
            location=relative.value,
            parent_location=(relative.parent.value if relative.parent else None),
            entries=tuple(entries),
        )

    def _relative_location(self, location: str) -> RelativeLocation:
        try:
            return RelativeLocation(location)
        except InvalidLocationError as error:
            raise RootEscapeError(str(error)) from error

    def _resolve_initial(self, start_path: Any) -> Path:
        candidate = Path(start_path).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        try:
            return candidate.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise LocationNotFoundError(
                f"start path does not exist: {start_path}"
            ) from error

    def _resolve(self, relative: RelativeLocation) -> Path:
        try:
            resolved = (self._root / relative.value).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise LocationNotFoundError(
                f"source location does not exist: {relative.value}"
            ) from error
        self._ensure_source_path(resolved)
        return resolved

    def _ensure_source_path(self, path: Path) -> None:
        if not _is_within(path, self._root):
            raise RootEscapeError(f"source path escapes configured root: {path}")
        if _is_within(path, self._library_root):
            raise LibrarySubtreeError("managed library is not a selectable source")

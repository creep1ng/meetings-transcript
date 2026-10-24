"""Immutable domain values used by the planning and catalog slice."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath

from .errors import (
    DomainValidationError,
    InvalidClassDateError,
    InvalidLocationError,
    InvalidNameError,
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_WINDOWS_INVALID = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


def normalized_name(value: str) -> str:
    """Return the canonical Unicode form used for stored names."""
    return unicodedata.normalize("NFC", value)


def name_collision_key(value: str) -> str:
    """Return the portable key used for course and filename collisions."""
    return normalized_name(value).casefold()


def _safe_segment(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidNameError(f"{label} must be text")
    normalized = normalized_name(value)
    if not normalized or normalized != normalized.strip():
        raise InvalidNameError(f"{label} cannot be empty or have outer whitespace")
    if normalized in {".", ".."} or normalized.endswith((".", " ")):
        raise InvalidNameError(f"{label} is not a safe path segment")
    if any(character in _WINDOWS_INVALID for character in normalized):
        raise InvalidNameError(f"{label} contains a Windows-invalid character")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise InvalidNameError(f"{label} contains a control character")
    reserved_stem = normalized.split(".", 1)[0].rstrip(" .").casefold()
    if reserved_stem in _WINDOWS_RESERVED:
        raise InvalidNameError(f"{label} uses a Windows-reserved device name")
    if len(normalized.encode("utf-16-le")) // 2 > 255:
        raise InvalidNameError(f"{label} is longer than 255 UTF-16 code units")
    return normalized


@dataclass(frozen=True, slots=True)
class CourseName:
    """A normalized, portable direct-child library name."""

    value: str

    def __post_init__(self) -> None:
        normalized = _safe_segment(self.value, label="course name")
        if normalized.startswith("."):
            raise InvalidNameError("course name cannot be hidden")
        object.__setattr__(self, "value", normalized)

    @property
    def collision_key(self) -> str:
        return name_collision_key(self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AssetFilename:
    """A normalized, portable filename for a managed asset."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", _safe_segment(self.value, label="asset filename")
        )

    @property
    def collision_key(self) -> str:
        return name_collision_key(self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class ClassDate:
    """A calendar date serialized only as strict ``YYYY-MM-DD``."""

    value: date

    def __post_init__(self) -> None:
        if not isinstance(self.value, date) or isinstance(self.value, datetime):
            raise InvalidClassDateError("class date must be a calendar date")

    @classmethod
    def parse(cls, value: str) -> "ClassDate":
        if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
            raise InvalidClassDateError("class date must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise InvalidClassDateError(f"invalid class date: {value}") from error
        return cls(parsed)

    @property
    def iso(self) -> str:
        return self.value.isoformat()

    def __str__(self) -> str:
        return self.iso


@dataclass(frozen=True, slots=True)
class RelativeLocation:
    """A POSIX-style, root-relative catalog identifier."""

    value: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or "\x00" in self.value:
            raise InvalidLocationError("location must be text without NUL bytes")
        if "\\" in self.value:
            raise InvalidLocationError("location must use POSIX separators")
        path = PurePosixPath(self.value or ".")
        if path.is_absolute() or ".." in path.parts:
            raise InvalidLocationError("location must remain relative to its root")
        normalized = "" if str(path) == "." else str(path)
        object.__setattr__(self, "value", normalized)

    @property
    def parent(self) -> "RelativeLocation | None":
        if not self.value:
            return None
        parent = str(PurePosixPath(self.value).parent)
        return RelativeLocation("" if parent == "." else parent)

    def child(self, name: str) -> "RelativeLocation":
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise InvalidLocationError("child must be one relative path segment")
        return RelativeLocation(
            str(PurePosixPath(self.value) / name) if self.value else name
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Sha256:
    """A canonical lowercase SHA-256 digest."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise DomainValidationError("SHA-256 must be text")
        normalized = self.value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise DomainValidationError("SHA-256 must contain 64 hexadecimal digits")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value

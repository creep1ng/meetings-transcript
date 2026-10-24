"""Immutable, serialization-safe application boundary records."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceEntryDTO:
    location: str
    name: str
    is_directory: bool
    size_bytes: int | None = None
    asset_kind: str | None = None


@dataclass(frozen=True, slots=True)
class BrowseSourcesQuery:
    location: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDirectoryDTO:
    location: str
    parent_location: str | None
    entries: tuple[SourceEntryDTO, ...]


@dataclass(frozen=True, slots=True)
class CourseDTO:
    name: str
    package_count: int = 0


@dataclass(frozen=True, slots=True)
class CreateCourseCommand:
    name: str


@dataclass(frozen=True, slots=True)
class PackageAssetDTO:
    name: str
    kind: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PackageDTO:
    course_name: str
    class_date: str
    assets: tuple[PackageAssetDTO, ...] = ()
    has_manifest: bool = False
    adopted: bool = True


@dataclass(frozen=True, slots=True)
class ListPackagesQuery:
    course_name: str


@dataclass(frozen=True, slots=True)
class EnsurePackageCommand:
    course_name: str
    class_date: str


@dataclass(frozen=True, slots=True)
class PlanAssetCommand:
    course_name: str
    class_date: str
    source_location: str | None = None
    original_name: str | None = None
    url: str | None = None
    label: str | None = None
    kind_override: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedAssetDTO:
    course_name: str
    class_date: str
    kind: str
    source_location: str | None
    source_url: str | None
    original_name: str | None
    destination_name: str | None
    destination_location: str | None
    label: str | None

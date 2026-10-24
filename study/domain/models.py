"""Immutable entities for courses, packages, and planned assets."""

from dataclasses import dataclass
from enum import StrEnum

from .errors import DomainValidationError
from .value_objects import AssetFilename, ClassDate, CourseName, RelativeLocation


class AssetKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    NOTEBOOK = "notebook"
    IMAGE = "image"
    ARCHIVE = "archive"
    LINK = "link"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Course:
    name: CourseName


@dataclass(frozen=True, slots=True)
class PackageAsset:
    filename: str
    kind: AssetKind
    size_bytes: int

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise DomainValidationError("asset size cannot be negative")


@dataclass(frozen=True, slots=True)
class ClassPackage:
    course: CourseName
    class_date: ClassDate
    assets: tuple[PackageAsset, ...] = ()
    has_manifest: bool = False


@dataclass(frozen=True, slots=True)
class AssetPlan:
    course: CourseName
    class_date: ClassDate
    kind: AssetKind
    source_location: RelativeLocation | None = None
    source_url: str | None = None
    original_filename: AssetFilename | None = None
    destination_filename: AssetFilename | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        is_link = self.kind is AssetKind.LINK
        if is_link:
            if not self.source_url or self.source_location is not None:
                raise DomainValidationError("link plans require only a source URL")
            if self.destination_filename is not None:
                raise DomainValidationError(
                    "link plans do not create a destination file"
                )
        elif (
            self.source_location is None
            or self.source_url is not None
            or self.original_filename is None
            or self.destination_filename is None
        ):
            raise DomainValidationError("file plans require source and filename values")

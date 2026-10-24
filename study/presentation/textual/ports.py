"""Injected presentation ports; concrete adapters are composed elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from .state import CourseItem, JobItem, PackageItem, SourceCatalog


@dataclass(frozen=True, slots=True)
class MoveConflict:
    resolution_key: str
    filename: str
    existing_sha256: str
    incoming_sha256: str


class MoveConflictError(RuntimeError):
    def __init__(self, conflict: MoveConflict) -> None:
        super().__init__(f"Different content already exists for {conflict.filename}")
        self.conflict = conflict


@dataclass(frozen=True, slots=True)
class MoveResult:
    moved_locations: tuple[str, ...]
    retained_locations: tuple[str, ...] = ()
    skipped_locations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OrganizerConfig:
    source_root: str
    library_root: str
    start_path: str
    language: str = "es"
    today: str = ""
    detected_date: str | None = None
    os_open_available: bool = True
    environment_label: str = "LOCAL"


class StudyFacade(Protocol):
    """Asynchronous boundary consumed by the Textual presentation."""

    async def browse_source(self, location: str | None) -> SourceCatalog: ...

    async def list_courses(self) -> tuple[CourseItem, ...]: ...

    async def list_packages(self, course_name: str) -> tuple[PackageItem, ...]: ...

    async def list_jobs(self) -> tuple[JobItem, ...]: ...

    async def create_course(self, name: str) -> CourseItem: ...

    async def ensure_package(
        self, course_name: str, class_date: str
    ) -> PackageItem: ...

    async def move_sources(
        self,
        locations: tuple[str, ...],
        course_name: str,
        class_date: str,
        resolutions: Mapping[str, str] | None = None,
        expected_generated_assets: Mapping[str, tuple[str, ...]] | None = None,
    ) -> MoveResult: ...

    async def dispatch_transcriptions(
        self, locations: tuple[str, ...], language: str
    ) -> tuple[JobItem, ...]: ...

    async def assign_sources(
        self, locations: tuple[str, ...], course_name: str, class_date: str
    ) -> None: ...

    async def rename_speakers(self, job_id: str, names: Mapping[str, str]) -> None: ...

    async def open_path(self, location: str) -> None: ...

    async def retry_job(self, job_id: str) -> None: ...

    async def discard_job(self, job_id: str) -> None: ...

    async def add_link(
        self, url: str, label: str, course_name: str, class_date: str
    ) -> None: ...

    async def close(self, *, cancel_active: bool) -> None: ...

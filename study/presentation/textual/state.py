"""Immutable state rendered by the academic organizer."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum


class Pane(StrEnum):
    SOURCES = "sources"
    COURSES = "courses"
    PACKAGES = "packages"


PANES = (Pane.SOURCES, Pane.COURSES, Pane.PACKAGES)


@dataclass(frozen=True, slots=True)
class SourceItem:
    location: str
    name: str
    is_directory: bool = False
    kind: str | None = None
    size_bytes: int | None = None
    is_parent: bool = False

    @property
    def is_media(self) -> bool:
        return self.kind in {"audio", "video"}


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    location: str
    parent_location: str | None
    entries: tuple[SourceItem, ...]


@dataclass(frozen=True, slots=True)
class CourseItem:
    name: str
    package_count: int = 0
    location: str | None = None


@dataclass(frozen=True, slots=True)
class PackageItem:
    course_name: str
    class_date: str
    location: str | None = None
    asset_names: tuple[str, ...] = ()
    has_manifest: bool = False
    adopted: bool = True
    planned_changes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpeakerItem:
    anonymous_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class JobItem:
    job_id: str
    file_name: str
    phase: str
    position: int
    total: int
    status: str
    active: bool = False
    recoverable: bool = False
    speakers: tuple[SpeakerItem, ...] = ()
    source_location: str = ""
    generated_assets: tuple[str, ...] = ()

    @property
    def progress_text(self) -> str:
        return f"{self.file_name} // {self.phase} // {self.position}/{self.total}"


@dataclass(frozen=True, slots=True)
class UIState:
    viewport_width: int = 120
    active_pane: Pane = Pane.SOURCES
    source_location: str = ""
    source_parent: str | None = None
    sources: tuple[SourceItem, ...] = ()
    source_index: int | None = None
    selected_locations: frozenset[str] = frozenset()
    selected_sources: tuple[SourceItem, ...] = ()
    transcription_locations: frozenset[str] = frozenset()
    courses: tuple[CourseItem, ...] = ()
    course_filter: str = ""
    course_index: int | None = None
    confirmed_course: str | None = None
    packages: tuple[PackageItem, ...] = ()
    package_index: int | None = None
    confirmed_date: str | None = None
    suggested_date: str | None = None
    today: str = ""
    language: str = "es"
    jobs: tuple[JobItem, ...] = ()
    assignment_running: frozenset[str] = frozenset()
    move_running: bool = False
    os_open_available: bool = True
    status: str = "BOOT // waiting for facade"
    error: str | None = None

    @property
    def compact(self) -> bool:
        return self.viewport_width < 120

    @property
    def current_source(self) -> SourceItem | None:
        if self.source_index is None or not self.sources:
            return None
        return self.sources[min(self.source_index, len(self.sources) - 1)]

    @property
    def current_course(self) -> CourseItem | None:
        courses = self.visible_courses
        if self.course_index is None or not courses:
            return None
        return courses[min(self.course_index, len(courses) - 1)]

    @property
    def visible_courses(self) -> tuple[CourseItem, ...]:
        query = self.course_filter.strip().casefold()
        if not query:
            return self.courses
        scored = (
            (
                max(
                    SequenceMatcher(None, query, course.name.casefold()).ratio(),
                    1.0 if query in course.name.casefold() else 0.0,
                ),
                course.name.casefold(),
                course,
            )
            for course in self.courses
        )
        return tuple(
            item[2] for item in sorted(scored, key=lambda item: (-item[0], item[1]))
        )

    @property
    def current_package(self) -> PackageItem | None:
        if self.package_index is None or not self.packages:
            return None
        return self.packages[min(self.package_index, len(self.packages) - 1)]

    @property
    def has_exit_jobs(self) -> bool:
        return bool(self.assignment_running) or any(
            job.active or job.recoverable for job in self.jobs
        )

    @property
    def renameable_job(self) -> JobItem | None:
        return next((job for job in self.jobs if job.speakers), None)

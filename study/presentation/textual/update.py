"""Pure Model-View-Update transitions for the organizer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, TypeAlias

from .state import (
    PANES,
    CourseItem,
    JobItem,
    PackageItem,
    Pane,
    SourceCatalog,
    SourceItem,
    UIState,
)


# Intents translated from keys and widget messages.
@dataclass(frozen=True, slots=True)
class Mounted:
    pass


@dataclass(frozen=True, slots=True)
class ViewportChanged:
    width: int


@dataclass(frozen=True, slots=True)
class NextPane:
    pass


@dataclass(frozen=True, slots=True)
class PaneFocused:
    pane: Pane


@dataclass(frozen=True, slots=True)
class SourceCursorChanged:
    index: int | None


@dataclass(frozen=True, slots=True)
class CourseCursorChanged:
    index: int | None


@dataclass(frozen=True, slots=True)
class PackageCursorChanged:
    index: int | None


@dataclass(frozen=True, slots=True)
class CourseFilterRequested:
    pass


@dataclass(frozen=True, slots=True)
class CourseFilterChanged:
    query: str


@dataclass(frozen=True, slots=True)
class ActivateCurrent:
    pass


@dataclass(frozen=True, slots=True)
class ToggleSelection:
    pass


@dataclass(frozen=True, slots=True)
class ToggleTranscription:
    pass


@dataclass(frozen=True, slots=True)
class NewCourseRequested:
    pass


@dataclass(frozen=True, slots=True)
class CourseNameEntered:
    name: str


@dataclass(frozen=True, slots=True)
class CreateCourseConfirmed:
    name: str


@dataclass(frozen=True, slots=True)
class CourseConfirmed:
    name: str


@dataclass(frozen=True, slots=True)
class DateRequested:
    pass


@dataclass(frozen=True, slots=True)
class DateConfirmed:
    class_date: str


@dataclass(frozen=True, slots=True)
class MoveRequested:
    pass


@dataclass(frozen=True, slots=True)
class MoveConfirmed:
    locations: tuple[str, ...]
    course_name: str
    class_date: str
    generated_assets: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class AddLinkRequested:
    pass


@dataclass(frozen=True, slots=True)
class LinkEntered:
    url: str
    label: str


@dataclass(frozen=True, slots=True)
class DispatchRequested:
    pass


@dataclass(frozen=True, slots=True)
class OpenRequested:
    pass


@dataclass(frozen=True, slots=True)
class RenameRequested:
    pass


@dataclass(frozen=True, slots=True)
class RetryRequested:
    job_id: str


@dataclass(frozen=True, slots=True)
class DiscardRequested:
    job_id: str


@dataclass(frozen=True, slots=True)
class SpeakerNamesConfirmed:
    job_id: str
    names: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class HelpRequested:
    pass


@dataclass(frozen=True, slots=True)
class BackRequested:
    pass


@dataclass(frozen=True, slots=True)
class ExitRequested:
    pass


@dataclass(frozen=True, slots=True)
class CloseDecision:
    cancel_active: bool | None


# Events returned by the facade.
@dataclass(frozen=True, slots=True)
class SourceLoaded:
    catalog: SourceCatalog


@dataclass(frozen=True, slots=True)
class CoursesLoaded:
    courses: tuple[CourseItem, ...]


@dataclass(frozen=True, slots=True)
class PackagesLoaded:
    course_name: str
    packages: tuple[PackageItem, ...]


@dataclass(frozen=True, slots=True)
class JobsLoaded:
    jobs: tuple[JobItem, ...]


@dataclass(frozen=True, slots=True)
class JobChanged:
    job: JobItem


@dataclass(frozen=True, slots=True)
class CourseCreated:
    course: CourseItem


@dataclass(frozen=True, slots=True)
class PackageEnsured:
    package: PackageItem


@dataclass(frozen=True, slots=True)
class MoveCompleted:
    moved_locations: tuple[str, ...]
    retained_locations: tuple[str, ...]
    skipped_locations: tuple[str, ...]
    course_name: str


@dataclass(frozen=True, slots=True)
class MoveCancelled:
    pass


@dataclass(frozen=True, slots=True)
class AssignmentStarted:
    locations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssignmentCompleted:
    locations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssignmentFailed:
    locations: tuple[str, ...]
    error: str


@dataclass(frozen=True, slots=True)
class OperationFailed:
    operation: str
    error: str


@dataclass(frozen=True, slots=True)
class Notice:
    text: str


Message: TypeAlias = (
    Mounted
    | ViewportChanged
    | NextPane
    | PaneFocused
    | SourceCursorChanged
    | CourseCursorChanged
    | PackageCursorChanged
    | CourseFilterRequested
    | CourseFilterChanged
    | ActivateCurrent
    | ToggleSelection
    | ToggleTranscription
    | NewCourseRequested
    | CourseNameEntered
    | CreateCourseConfirmed
    | CourseConfirmed
    | DateRequested
    | DateConfirmed
    | MoveRequested
    | MoveConfirmed
    | AddLinkRequested
    | LinkEntered
    | DispatchRequested
    | OpenRequested
    | RenameRequested
    | RetryRequested
    | DiscardRequested
    | SpeakerNamesConfirmed
    | HelpRequested
    | BackRequested
    | ExitRequested
    | CloseDecision
    | SourceLoaded
    | CoursesLoaded
    | PackagesLoaded
    | JobsLoaded
    | JobChanged
    | CourseCreated
    | PackageEnsured
    | MoveCompleted
    | MoveCancelled
    | AssignmentStarted
    | AssignmentCompleted
    | AssignmentFailed
    | OperationFailed
    | Notice
)


# Effects interpreted by OrganizerApp.
@dataclass(frozen=True, slots=True)
class LoadSource:
    location: str | None


@dataclass(frozen=True, slots=True)
class LoadCourses:
    pass


@dataclass(frozen=True, slots=True)
class LoadPackages:
    course_name: str


@dataclass(frozen=True, slots=True)
class LoadJobs:
    pass


@dataclass(frozen=True, slots=True)
class CreateCourse:
    name: str


@dataclass(frozen=True, slots=True)
class EnsurePackage:
    course_name: str
    class_date: str


@dataclass(frozen=True, slots=True)
class MoveSources:
    locations: tuple[str, ...]
    course_name: str
    class_date: str
    generated_assets: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class AddLink:
    url: str
    label: str
    course_name: str
    class_date: str


@dataclass(frozen=True, slots=True)
class DispatchTranscriptions:
    locations: tuple[str, ...]
    language: str


@dataclass(frozen=True, slots=True)
class BeginAssignment:
    locations: tuple[str, ...]
    course_name: str
    class_date: str


@dataclass(frozen=True, slots=True)
class RenameSpeakers:
    job_id: str
    names: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RetryJob:
    job_id: str


@dataclass(frozen=True, slots=True)
class DiscardJob:
    job_id: str


@dataclass(frozen=True, slots=True)
class OpenLocation:
    location: str


@dataclass(frozen=True, slots=True)
class ShowCourseConfirmation:
    course_name: str


@dataclass(frozen=True, slots=True)
class ShowMoveReview:
    locations: tuple[str, ...]
    course_name: str
    class_date: str
    assets: tuple[tuple[str, str, tuple[str, ...]], ...]
    generated_assets: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class ShowLink:
    pass


@dataclass(frozen=True, slots=True)
class FocusCourseFilter:
    pass


@dataclass(frozen=True, slots=True)
class FocusPane:
    pane: Pane


@dataclass(frozen=True, slots=True)
class ShowNewCourse:
    pass


@dataclass(frozen=True, slots=True)
class ShowCourseCreationConfirmation:
    course_name: str


@dataclass(frozen=True, slots=True)
class ShowCalendar:
    prefill: str


@dataclass(frozen=True, slots=True)
class ShowSpeakerRename:
    job: JobItem


@dataclass(frozen=True, slots=True)
class ShowHelp:
    pass


@dataclass(frozen=True, slots=True)
class ShowClosePrompt:
    active_jobs: int
    recoverable_jobs: int


@dataclass(frozen=True, slots=True)
class Shutdown:
    cancel_active: bool


Effect: TypeAlias = (
    LoadSource
    | LoadCourses
    | LoadPackages
    | LoadJobs
    | CreateCourse
    | EnsurePackage
    | MoveSources
    | AddLink
    | DispatchTranscriptions
    | BeginAssignment
    | RenameSpeakers
    | RetryJob
    | DiscardJob
    | OpenLocation
    | ShowCourseConfirmation
    | ShowMoveReview
    | ShowLink
    | FocusCourseFilter
    | FocusPane
    | ShowNewCourse
    | ShowCourseCreationConfirmation
    | ShowCalendar
    | ShowSpeakerRename
    | ShowHelp
    | ShowClosePrompt
    | Shutdown
)


@dataclass(frozen=True, slots=True)
class Transition:
    state: UIState
    effects: tuple[Effect, ...] = ()


def _assignment_effect(state: UIState) -> tuple[Effect, ...]:
    if not state.confirmed_course or not state.confirmed_date:
        return ()
    candidates = (
        state.selected_locations | state.transcription_locations
    ) - state.assignment_running
    if not candidates:
        return ()
    return (
        BeginAssignment(
            tuple(sorted(candidates)), state.confirmed_course, state.confirmed_date
        ),
    )


def _with_assignment(state: UIState, *effects: Effect) -> Transition:
    assignment_effects = _assignment_effect(state)
    if assignment_effects:
        assignment = assignment_effects[0]
        assert isinstance(assignment, BeginAssignment)
        state = replace(
            state,
            assignment_running=state.assignment_running
            | frozenset(assignment.locations),
        )
    return Transition(state, tuple(effects) + assignment_effects)


def _merge_job(jobs: tuple[JobItem, ...], changed: JobItem) -> tuple[JobItem, ...]:
    merged = [job for job in jobs if job.job_id != changed.job_id]
    merged.append(changed)
    return tuple(
        sorted(merged, key=lambda job: (job.position, job.file_name.casefold()))
    )


def update(state: UIState, message: Message) -> Transition:
    """Return the next immutable state and declarative effects."""

    if isinstance(message, Mounted):
        return Transition(
            replace(state, status="INDEX // loading workspace", error=None),
            (
                LoadSource(state.source_location or None),
                LoadCourses(),
                LoadJobs(),
                FocusPane(state.active_pane),
            ),
        )
    if isinstance(message, ViewportChanged):
        return Transition(replace(state, viewport_width=message.width))
    if isinstance(message, NextPane):
        index = (PANES.index(state.active_pane) + 1) % len(PANES)
        pane = PANES[index]
        return Transition(replace(state, active_pane=pane), (FocusPane(pane),))
    if isinstance(message, PaneFocused):
        return Transition(replace(state, active_pane=message.pane))
    if isinstance(message, SourceCursorChanged):
        return Transition(replace(state, source_index=message.index))
    if isinstance(message, CourseCursorChanged):
        return Transition(replace(state, course_index=message.index))
    if isinstance(message, PackageCursorChanged):
        return Transition(replace(state, package_index=message.index))
    if isinstance(message, CourseFilterRequested):
        return Transition(
            replace(state, active_pane=Pane.COURSES), (FocusCourseFilter(),)
        )
    if isinstance(message, CourseFilterChanged):
        next_state = replace(
            state, course_filter=message.query, active_pane=Pane.COURSES
        )
        return Transition(
            replace(
                next_state,
                course_index=0 if next_state.visible_courses else None,
                status=f"FILTER // {len(next_state.visible_courses)} course match(es)",
            )
        )
    if isinstance(message, ActivateCurrent):
        if state.active_pane is Pane.SOURCES:
            source = state.current_source
            if source and source.is_directory:
                return Transition(state, (LoadSource(source.location),))
        elif state.active_pane is Pane.COURSES:
            course = state.current_course
            if course:
                return Transition(state, (ShowCourseConfirmation(course.name),))
        elif state.current_package:
            return Transition(
                replace(
                    state,
                    status=f"PACKAGE // {state.current_package.class_date} selected",
                )
            )
        return Transition(replace(state, status="ACTION // nothing available here"))
    if isinstance(message, ToggleSelection):
        source = state.current_source
        if not source or source.is_directory:
            return Transition(replace(state, status="SELECT // choose a file"))
        selected = set(state.selected_locations)
        selected.symmetric_difference_update({source.location})
        selected_sources = {item.location: item for item in state.selected_sources}
        if source.location in selected:
            selected_sources[source.location] = source
        else:
            selected_sources.pop(source.location, None)
        next_state = replace(
            state,
            selected_locations=frozenset(selected),
            selected_sources=tuple(
                selected_sources[location] for location in sorted(selected_sources)
            ),
            status=f"SELECT // {len(selected)} material(s)",
        )
        return _with_assignment(next_state)
    if isinstance(message, ToggleTranscription):
        source = state.current_source
        if not source or not source.is_media:
            return Transition(
                replace(state, status="ASR // only audio or video can be marked")
            )
        marked = set(state.transcription_locations)
        marked.symmetric_difference_update({source.location})
        next_state = replace(
            state,
            transcription_locations=frozenset(marked),
            status=f"ASR // {len(marked)} recording(s) marked",
        )
        return _with_assignment(next_state)
    if isinstance(message, NewCourseRequested):
        return Transition(state, (ShowNewCourse(),))
    if isinstance(message, CourseNameEntered):
        normalized = " ".join(message.name.split())
        if not normalized:
            return Transition(replace(state, status="COURSE // name cannot be empty"))
        return Transition(state, (ShowCourseCreationConfirmation(normalized),))
    if isinstance(message, CreateCourseConfirmed):
        return Transition(
            replace(state, status=f"COURSE // creating {message.name}"),
            (CreateCourse(message.name),),
        )
    if isinstance(message, CourseConfirmed):
        next_state = replace(
            state,
            confirmed_course=message.name,
            packages=(),
            package_index=None,
            status=f"COURSE // confirmed {message.name}",
        )
        effects: list[Effect] = [LoadPackages(message.name)]
        if next_state.confirmed_date:
            effects.append(EnsurePackage(message.name, next_state.confirmed_date))
        return _with_assignment(next_state, *effects)
    if isinstance(message, DateRequested):
        prefill = state.confirmed_date or state.suggested_date or state.today
        return Transition(state, (ShowCalendar(prefill),))
    if isinstance(message, DateConfirmed):
        next_state = replace(
            state,
            confirmed_date=message.class_date,
            status=f"DATE // confirmed {message.class_date}",
        )
        effects: list[Effect] = []
        if next_state.confirmed_course:
            effects.append(
                EnsurePackage(next_state.confirmed_course, message.class_date)
            )
        return _with_assignment(next_state, *effects)
    if isinstance(message, MoveRequested):
        if state.move_running:
            return Transition(replace(state, status="MOVE // commit already running"))
        if not state.selected_locations:
            return Transition(
                replace(state, status="MOVE // select materials with Space")
            )
        if not state.confirmed_course or not state.confirmed_date:
            return Transition(
                replace(state, status="MOVE // confirm course and class date first")
            )
        locations = tuple(sorted(state.selected_locations))
        indexed = {source.location: source for source in state.selected_sources}
        indexed.update({source.location: source for source in state.sources})
        missing = tuple(location for location in locations if location not in indexed)
        if missing:
            return Transition(
                replace(
                    state,
                    status="MOVE // revisit or reselect materials missing from review",
                )
            )
        generated = {
            job.source_location: job.generated_assets
            for job in state.jobs
            if job.source_location and job.generated_assets
        }
        assets = tuple(
            (
                indexed[location].name,
                indexed[location].kind or "other",
                generated.get(location, ()),
            )
            for location in locations
        )
        reviewed_generated = tuple(
            (location, generated.get(location, ())) for location in locations
        )
        return Transition(
            replace(
                state,
                move_running=True,
                status=f"MOVE // reviewing {len(locations)} material(s)",
            ),
            (
                ShowMoveReview(
                    locations,
                    state.confirmed_course,
                    state.confirmed_date,
                    assets,
                    reviewed_generated,
                ),
            ),
        )
    if isinstance(message, MoveConfirmed):
        return Transition(
            replace(
                state,
                move_running=True,
                status=f"MOVE // committing {len(message.locations)} material(s)",
            ),
            (
                MoveSources(
                    message.locations,
                    message.course_name,
                    message.class_date,
                    message.generated_assets,
                ),
            ),
        )
    if isinstance(message, AddLinkRequested):
        if not state.confirmed_course or not state.confirmed_date:
            return Transition(
                replace(state, status="LINK // confirm course and class date first")
            )
        return Transition(state, (ShowLink(),))
    if isinstance(message, LinkEntered):
        if not state.confirmed_course or not state.confirmed_date:
            return Transition(
                replace(state, status="LINK // target is no longer confirmed")
            )
        return Transition(
            replace(state, status="LINK // adding reference"),
            (
                AddLink(
                    message.url,
                    message.label,
                    state.confirmed_course,
                    state.confirmed_date,
                ),
            ),
        )
    if isinstance(message, DispatchRequested):
        if not state.transcription_locations:
            return Transition(
                replace(state, status="ASR // mark recordings with Shift+T")
            )
        locations = tuple(sorted(state.transcription_locations))
        return Transition(
            replace(state, status=f"ASR // dispatching {len(locations)} recording(s)"),
            (DispatchTranscriptions(locations, state.language),),
        )
    if isinstance(message, OpenRequested):
        if not state.os_open_available:
            return Transition(
                replace(state, status="OPEN // unavailable in this terminal session")
            )
        location: str | None = None
        if state.active_pane is Pane.SOURCES and state.current_source:
            location = state.current_source.location
        elif state.active_pane is Pane.COURSES and state.current_course:
            location = state.current_course.location
        elif state.active_pane is Pane.PACKAGES and state.current_package:
            location = state.current_package.location
        if not location:
            return Transition(replace(state, status="OPEN // no location selected"))
        return Transition(state, (OpenLocation(location),))
    if isinstance(message, RenameRequested):
        job = state.renameable_job
        if not job:
            return Transition(
                replace(state, status="SPEAKERS // no transcript speakers available")
            )
        return Transition(state, (ShowSpeakerRename(job),))
    if isinstance(message, RetryRequested):
        return Transition(
            replace(state, status="RECOVERY // validating retained work"),
            (RetryJob(message.job_id),),
        )
    if isinstance(message, DiscardRequested):
        return Transition(
            replace(state, status="RECOVERY // discarding retained work"),
            (DiscardJob(message.job_id),),
        )
    if isinstance(message, SpeakerNamesConfirmed):
        return Transition(
            replace(state, status="SPEAKERS // updating names"),
            (RenameSpeakers(message.job_id, message.names),),
        )
    if isinstance(message, HelpRequested):
        return Transition(state, (ShowHelp(),))
    if isinstance(message, BackRequested):
        if state.course_filter:
            return Transition(
                replace(
                    state, course_filter="", course_index=0 if state.courses else None
                )
            )
        if state.active_pane is Pane.SOURCES and state.source_parent is not None:
            return Transition(state, (LoadSource(state.source_parent),))
        if state.compact and state.active_pane is not Pane.SOURCES:
            return Transition(replace(state, active_pane=Pane.SOURCES))
        return Transition(state, (ShowHelp(),))
    if isinstance(message, ExitRequested):
        if state.move_running:
            return Transition(
                replace(state, status="EXIT // wait for the asset commit to finish")
            )
        if state.has_exit_jobs:
            active = sum(job.active for job in state.jobs) + bool(
                state.assignment_running
            )
            recoverable = sum(job.recoverable for job in state.jobs)
            return Transition(state, (ShowClosePrompt(active, recoverable),))
        return Transition(state, (Shutdown(False),))
    if isinstance(message, CloseDecision):
        if message.cancel_active is None:
            return Transition(replace(state, status="EXIT // cancelled"))
        return Transition(state, (Shutdown(message.cancel_active),))
    if isinstance(message, SourceLoaded):
        parent = (
            (
                SourceItem(
                    message.catalog.parent_location,
                    "..",
                    is_directory=True,
                    is_parent=True,
                ),
            )
            if message.catalog.parent_location is not None
            else ()
        )
        sources = parent + message.catalog.entries
        refreshed = {
            source.location: source
            for source in state.selected_sources
            if source.location in state.selected_locations
        }
        for source in message.catalog.entries:
            if source.location in state.selected_locations:
                refreshed[source.location] = source
        return Transition(
            replace(
                state,
                source_location=message.catalog.location,
                source_parent=message.catalog.parent_location,
                sources=sources,
                selected_sources=tuple(
                    refreshed[location] for location in sorted(refreshed)
                ),
                source_index=0 if sources else None,
                status=f"SOURCE // {message.catalog.location}",
                error=None,
            )
        )
    if isinstance(message, CoursesLoaded):
        return Transition(
            replace(
                state,
                courses=message.courses,
                course_index=0 if message.courses else None,
                status=f"COURSES // {len(message.courses)} indexed",
                error=None,
            )
        )
    if isinstance(message, PackagesLoaded):
        if message.course_name != state.confirmed_course:
            return Transition(state)
        return Transition(
            replace(
                state,
                packages=message.packages,
                package_index=0 if message.packages else None,
                status=f"PACKAGES // {len(message.packages)} for {message.course_name}",
                error=None,
            )
        )
    if isinstance(message, JobsLoaded):
        return Transition(replace(state, jobs=message.jobs, error=None))
    if isinstance(message, JobChanged):
        return Transition(replace(state, jobs=_merge_job(state.jobs, message.job)))
    if isinstance(message, CourseCreated):
        courses = tuple(
            sorted(
                (
                    *[c for c in state.courses if c.name != message.course.name],
                    message.course,
                ),
                key=lambda course: course.name.casefold(),
            )
        )
        index = next(
            i for i, course in enumerate(courses) if course.name == message.course.name
        )
        next_state = replace(
            state,
            courses=courses,
            course_index=index,
            confirmed_course=message.course.name,
            status=f"COURSE // confirmed {message.course.name}",
        )
        effects: list[Effect] = [LoadPackages(message.course.name)]
        if next_state.confirmed_date:
            effects.append(
                EnsurePackage(message.course.name, next_state.confirmed_date)
            )
        return _with_assignment(next_state, *effects)
    if isinstance(message, PackageEnsured):
        packages = tuple(
            sorted(
                (
                    *[
                        package
                        for package in state.packages
                        if package.class_date != message.package.class_date
                    ],
                    message.package,
                ),
                key=lambda package: package.class_date,
                reverse=True,
            )
        )
        return Transition(
            replace(
                state,
                packages=packages,
                package_index=next(
                    i
                    for i, package in enumerate(packages)
                    if package.class_date == message.package.class_date
                ),
            )
        )
    if isinstance(message, MoveCompleted):
        moved = frozenset(message.moved_locations)
        skipped = frozenset(message.skipped_locations)
        cleared = moved | skipped
        details = [f"committed {len(moved)}"]
        if skipped:
            details.append(f"skipped {len(skipped)}")
        if message.retained_locations:
            details.append(f"retained {len(message.retained_locations)} changed")
        return Transition(
            replace(
                state,
                selected_locations=state.selected_locations - cleared,
                selected_sources=tuple(
                    source
                    for source in state.selected_sources
                    if source.location not in cleared
                ),
                transcription_locations=state.transcription_locations - moved,
                move_running=False,
                status="MOVE // " + "; ".join(details),
            ),
            (LoadSource(state.source_location), LoadPackages(message.course_name)),
        )
    if isinstance(message, MoveCancelled):
        return Transition(
            replace(
                state,
                move_running=False,
                status="MOVE // cancelled; sources unchanged",
            )
        )
    if isinstance(message, AssignmentStarted):
        locations = frozenset(message.locations)
        return Transition(
            replace(
                state,
                assignment_running=state.assignment_running | locations,
                status=f"ASSIGN // {len(locations)} file(s) running independently",
            )
        )
    if isinstance(message, AssignmentCompleted):
        locations = frozenset(message.locations)
        return Transition(
            replace(
                state,
                assignment_running=state.assignment_running - locations,
                status=f"ASSIGN // confirmed {len(locations)} file(s)",
            )
        )
    if isinstance(message, AssignmentFailed):
        locations = frozenset(message.locations)
        return Transition(
            replace(
                state,
                assignment_running=state.assignment_running - locations,
                status=f"ASSIGN // failed: {message.error}",
                error=message.error,
            )
        )
    if isinstance(message, OperationFailed):
        return Transition(
            replace(
                state,
                move_running=(
                    False if message.operation == "MoveSources" else state.move_running
                ),
                status=f"{message.operation.upper()} // failed: {message.error}",
                error=message.error,
            )
        )
    if isinstance(message, Notice):
        return Transition(replace(state, status=message.text, error=None))
    raise TypeError(f"unsupported update message: {type(message).__name__}")

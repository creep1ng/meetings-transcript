"""Textual navigation and lifecycle for the academic organizer."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Mapping

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, ListView, Static

from . import update as mvu
from .ports import MoveConflictError, OrganizerConfig, StudyFacade
from .screens import (
    CalendarScreen,
    CloseJobsScreen,
    ConfirmationScreen,
    CourseNameScreen,
    HelpScreen,
    LinkScreen,
    MoveConflictScreen,
    MoveReviewScreen,
    RecoveryScreen,
    SpeakerRenameScreen,
)
from .state import Pane, UIState
from .widgets import CoursePane, JobStrip, PackagePane, SourcePane, StatusStrip


class OrganizerApp(App[None]):
    """Render immutable organizer state and interpret its declared effects."""

    CSS_PATH = "organizer.tcss"
    BINDINGS = [
        Binding("tab", "next_pane", "Pane", priority=True),
        Binding("space", "toggle_selection", "Select", priority=True),
        Binding("shift+t", "toggle_transcription", "Mark ASR", priority=True),
        Binding("m", "move", "Move", priority=True),
        Binding("t", "dispatch_transcriptions", "Dispatch", priority=True),
        Binding("n", "new_course", "New course", priority=True),
        Binding("l", "add_link", "Add link", priority=True),
        Binding("c", "calendar", "Class date", priority=True),
        Binding("r", "rename_speakers", "Speakers", priority=True),
        Binding("u", "recovery", "Recovery", priority=True),
        Binding("o", "open_path", "Open", priority=True),
        Binding("slash", "course_filter", "Filter", priority=True),
        Binding("h", "help", "Help", priority=True),
        Binding("escape", "back", "Back / menu", priority=True),
        Binding("enter", "activate", "Open / confirm", show=False),
        Binding("q", "request_exit", "Exit", show=False, priority=True),
    ]

    def __init__(
        self,
        facade: StudyFacade,
        config: OrganizerConfig,
        *,
        initial_state: UIState | None = None,
    ) -> None:
        super().__init__()
        self.facade = facade
        self.config = config
        today = config.today or date.today().isoformat()
        self.ui_state = initial_state or UIState(
            source_location=config.start_path,
            suggested_date=config.detected_date,
            today=today,
            language=config.language,
            os_open_available=config.os_open_available,
        )
        self._ui_ready = False
        self._facade_closed = False
        self._recovery_prompted: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(
            f"ACADEMIC ORGANIZER // {self.config.environment_label} // LOCAL-FIRST",
            id="masthead",
        )
        with Horizontal(id="workspace"):
            yield SourcePane(id="sources-pane", classes="workspace-pane")
            yield CoursePane(id="courses-pane", classes="workspace-pane")
            yield PackagePane(id="packages-pane", classes="workspace-pane")
        yield JobStrip(id="job-strip")
        yield StatusStrip(id="status-strip")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Study Control Deck"
        self.sub_title = self.config.library_root
        self._ui_ready = True
        self.dispatch_update(mvu.ViewportChanged(self.size.width))
        self.dispatch_update(mvu.Mounted())
        self.set_interval(1.0, self._refresh_jobs)

    def on_resize(self, event: events.Resize) -> None:
        self.dispatch_update(mvu.ViewportChanged(event.size.width))

    async def on_unmount(self) -> None:
        if not self._facade_closed:
            self._facade_closed = True
            await self.facade.close(cancel_active=False)

    def dispatch_update(self, message: mvu.Message) -> None:
        """Apply one pure transition; useful as an event sink for facade bridges."""

        transition = mvu.update(self.ui_state, message)
        self.ui_state = transition.state
        if self._ui_ready:
            self._render_state()
        for effect in transition.effects:
            self._interpret(effect)

    def _render_state(self) -> None:
        pane_widgets = {
            Pane.SOURCES: self.query_one(SourcePane),
            Pane.COURSES: self.query_one(CoursePane),
            Pane.PACKAGES: self.query_one(PackagePane),
        }
        for pane, widget in pane_widgets.items():
            widget.styles.display = (
                "block"
                if not self.ui_state.compact or pane is self.ui_state.active_pane
                else "none"
            )
            widget.show_state(self.ui_state)
        self.query_one(JobStrip).show_state(self.ui_state)
        self.query_one(StatusStrip).show_state(self.ui_state)

    def _interpret(self, effect: mvu.Effect) -> None:
        if isinstance(effect, mvu.FocusPane):
            self.call_later(self._focus_pane, effect.pane)
        elif isinstance(effect, mvu.FocusCourseFilter):
            self.call_later(self.query_one("#course-filter", Input).focus)
        elif isinstance(effect, mvu.ShowCourseConfirmation):
            self.push_screen(
                ConfirmationScreen(
                    "CONFIRM COURSE",
                    f"Assign selected work to {effect.course_name}?",
                    "CONFIRM COURSE",
                ),
                lambda confirmed: (
                    self.dispatch_update(mvu.CourseConfirmed(effect.course_name))
                    if confirmed
                    else None
                ),
            )
        elif isinstance(effect, mvu.ShowMoveReview):
            self.push_screen(
                MoveReviewScreen(
                    effect.course_name,
                    effect.class_date,
                    effect.assets,
                ),
                lambda confirmed: (
                    self.dispatch_update(
                        mvu.MoveConfirmed(
                            effect.locations,
                            effect.course_name,
                            effect.class_date,
                            effect.generated_assets,
                        )
                    )
                    if confirmed
                    else self.dispatch_update(mvu.MoveCancelled())
                ),
            )
        elif isinstance(effect, mvu.ShowLink):
            self.push_screen(
                LinkScreen(),
                lambda result: (
                    self.dispatch_update(mvu.LinkEntered(*result)) if result else None
                ),
            )
        elif isinstance(effect, mvu.ShowNewCourse):
            self.push_screen(
                CourseNameScreen(),
                lambda name: (
                    self.dispatch_update(mvu.CourseNameEntered(name)) if name else None
                ),
            )
        elif isinstance(effect, mvu.ShowCourseCreationConfirmation):
            self.push_screen(
                ConfirmationScreen(
                    "CREATE COURSE",
                    f"Create direct-child course {effect.course_name} and confirm it?",
                    "CREATE + CONFIRM",
                ),
                lambda confirmed: (
                    self.dispatch_update(mvu.CreateCourseConfirmed(effect.course_name))
                    if confirmed
                    else None
                ),
            )
        elif isinstance(effect, mvu.ShowCalendar):
            prefill = self._date_or_today(effect.prefill)
            self.push_screen(
                CalendarScreen(
                    prefill=prefill,
                    today=self._date_or_today(self.ui_state.today),
                ),
                lambda selected: (
                    self.dispatch_update(mvu.DateConfirmed(selected.isoformat()))
                    if selected
                    else None
                ),
            )
        elif isinstance(effect, mvu.ShowSpeakerRename):
            self.push_screen(
                SpeakerRenameScreen(effect.job),
                lambda names: (
                    self.dispatch_update(
                        mvu.SpeakerNamesConfirmed(effect.job.job_id, names)
                    )
                    if names
                    else None
                ),
            )
        elif isinstance(effect, mvu.ShowHelp):
            self.push_screen(HelpScreen())
        elif isinstance(effect, mvu.ShowClosePrompt):
            self.push_screen(
                CloseJobsScreen(effect.active_jobs, effect.recoverable_jobs),
                self._close_prompt_result,
            )
        else:
            self.run_worker(
                self._run_facade_effect(effect),
                name=type(effect).__name__,
                group=type(effect).__name__,
                exit_on_error=False,
                exclusive=isinstance(effect, (mvu.LoadJobs, mvu.LoadPackages)),
            )

    @staticmethod
    def _date_or_today(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return date.today()

    def _close_prompt_result(self, decision: str | None) -> None:
        cancel_active = None
        if decision == "cancel":
            cancel_active = True
        elif decision == "retain":
            cancel_active = False
        self.dispatch_update(mvu.CloseDecision(cancel_active))

    def _refresh_jobs(self) -> None:
        if not self._facade_closed:
            self._interpret(mvu.LoadJobs())

    def _maybe_show_recovery(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        job = next(
            (
                item
                for item in self.ui_state.jobs
                if item.recoverable
                and not item.active
                and item.job_id not in self._recovery_prompted
            ),
            None,
        )
        if job is None:
            return
        self._recovery_prompted.add(job.job_id)
        self.push_screen(
            RecoveryScreen(job),
            lambda decision: self._recovery_result(job.job_id, decision),
        )

    def _recovery_result(self, job_id: str, decision: str | None) -> None:
        if decision == "retry":
            self.dispatch_update(mvu.RetryRequested(job_id))
        elif decision == "discard":
            self.dispatch_update(mvu.DiscardRequested(job_id))
        else:
            self.dispatch_update(mvu.Notice("RECOVERY // retained for later"))

    def _focus_pane(self, pane: Pane) -> None:
        selector = {
            Pane.SOURCES: "#source-list",
            Pane.COURSES: "#course-list",
            Pane.PACKAGES: "#package-list",
        }[pane]
        self.query_one(selector, ListView).focus()

    async def _run_facade_effect(self, effect: mvu.Effect) -> None:
        operation = type(effect).__name__
        try:
            if isinstance(effect, mvu.LoadSource):
                catalog = await self.facade.browse_source(effect.location)
                self.dispatch_update(mvu.SourceLoaded(catalog))
            elif isinstance(effect, mvu.LoadCourses):
                self.dispatch_update(
                    mvu.CoursesLoaded(await self.facade.list_courses())
                )
            elif isinstance(effect, mvu.LoadPackages):
                packages = await self.facade.list_packages(effect.course_name)
                self.dispatch_update(mvu.PackagesLoaded(effect.course_name, packages))
            elif isinstance(effect, mvu.LoadJobs):
                self.dispatch_update(mvu.JobsLoaded(await self.facade.list_jobs()))
            elif isinstance(effect, mvu.CreateCourse):
                course = await self.facade.create_course(effect.name)
                self.dispatch_update(mvu.CourseCreated(course))
            elif isinstance(effect, mvu.EnsurePackage):
                package = await self.facade.ensure_package(
                    effect.course_name, effect.class_date
                )
                self.dispatch_update(mvu.PackageEnsured(package))
            elif isinstance(effect, mvu.MoveSources):
                await self._move_with_conflicts(effect, {})
            elif isinstance(effect, mvu.DispatchTranscriptions):
                jobs = await self.facade.dispatch_transcriptions(
                    effect.locations, effect.language
                )
                self.dispatch_update(mvu.JobsLoaded(jobs))
                self.dispatch_update(mvu.Notice("ASR // recordings dispatched"))
            elif isinstance(effect, mvu.BeginAssignment):
                self.dispatch_update(mvu.AssignmentStarted(effect.locations))
                try:
                    await self.facade.assign_sources(
                        effect.locations, effect.course_name, effect.class_date
                    )
                except Exception as error:
                    self.dispatch_update(
                        mvu.AssignmentFailed(effect.locations, str(error))
                    )
                else:
                    self.dispatch_update(mvu.AssignmentCompleted(effect.locations))
            elif isinstance(effect, mvu.RenameSpeakers):
                await self.facade.rename_speakers(effect.job_id, effect.names)
                self.dispatch_update(mvu.Notice("SPEAKERS // display names updated"))
            elif isinstance(effect, mvu.AddLink):
                await self.facade.add_link(
                    effect.url,
                    effect.label,
                    effect.course_name,
                    effect.class_date,
                )
                self.dispatch_update(mvu.Notice("LINK // reference added"))
                self._interpret(mvu.LoadPackages(effect.course_name))
            elif isinstance(effect, mvu.RetryJob):
                await self.facade.retry_job(effect.job_id)
                self.dispatch_update(mvu.Notice("RECOVERY // job queued"))
                self._interpret(mvu.LoadJobs())
            elif isinstance(effect, mvu.DiscardJob):
                await self.facade.discard_job(effect.job_id)
                self.dispatch_update(mvu.Notice("RECOVERY // staging discarded"))
                self._interpret(mvu.LoadJobs())
            elif isinstance(effect, mvu.OpenLocation):
                await self.facade.open_path(effect.location)
                self.dispatch_update(mvu.Notice("OPEN // request sent to OS"))
            elif isinstance(effect, mvu.Shutdown):
                await self._close_and_exit(effect.cancel_active)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.dispatch_update(mvu.OperationFailed(operation, str(error)))

    async def _move_with_conflicts(
        self, effect: mvu.MoveSources, resolutions: dict[str, str]
    ) -> None:
        try:
            result = await self.facade.move_sources(
                effect.locations,
                effect.course_name,
                effect.class_date,
                resolutions,
                dict(effect.generated_assets),
            )
        except MoveConflictError as error:
            conflict = error.conflict
            self.push_screen(
                MoveConflictScreen(conflict),
                lambda decision, key=conflict.resolution_key: self._continue_move(
                    effect, resolutions, key, decision
                ),
            )
            return
        self.dispatch_update(
            mvu.MoveCompleted(
                result.moved_locations,
                result.retained_locations,
                result.skipped_locations,
                effect.course_name,
            )
        )

    def _continue_move(
        self,
        effect: mvu.MoveSources,
        resolutions: dict[str, str],
        resolution_key: str,
        decision: str | None,
    ) -> None:
        if decision is None or decision == "cancel":
            self.dispatch_update(mvu.MoveCancelled())
            return
        next_resolutions = {**resolutions, resolution_key: decision}
        self.run_worker(
            self._move_with_conflicts(effect, next_resolutions),
            name="MoveSources",
            group="MoveSources",
            exit_on_error=False,
        )

    async def _close_and_exit(self, cancel_active: bool) -> None:
        if not self._facade_closed:
            self._facade_closed = True
            await self.facade.close(cancel_active=cancel_active)
        self.exit()

    @on(ListView.Highlighted, "#source-list")
    def source_highlighted(self, event: ListView.Highlighted) -> None:
        self.dispatch_update(mvu.SourceCursorChanged(event.list_view.index))
        if event.list_view.has_focus:
            self.dispatch_update(mvu.PaneFocused(Pane.SOURCES))

    @on(ListView.Highlighted, "#course-list")
    def course_highlighted(self, event: ListView.Highlighted) -> None:
        self.dispatch_update(mvu.CourseCursorChanged(event.list_view.index))
        if event.list_view.has_focus:
            self.dispatch_update(mvu.PaneFocused(Pane.COURSES))

    @on(ListView.Highlighted, "#package-list")
    def package_highlighted(self, event: ListView.Highlighted) -> None:
        self.dispatch_update(mvu.PackageCursorChanged(event.list_view.index))
        if event.list_view.has_focus:
            self.dispatch_update(mvu.PaneFocused(Pane.PACKAGES))

    @on(ListView.Selected, "#source-list, #course-list, #package-list")
    def row_selected(self) -> None:
        self.dispatch_update(mvu.ActivateCurrent())

    @on(Input.Changed, "#course-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.dispatch_update(mvu.CourseFilterChanged(event.value))

    def action_next_pane(self) -> None:
        self.dispatch_update(mvu.NextPane())

    def action_toggle_selection(self) -> None:
        self.dispatch_update(mvu.ToggleSelection())

    def action_toggle_transcription(self) -> None:
        self.dispatch_update(mvu.ToggleTranscription())

    def action_move(self) -> None:
        self.dispatch_update(mvu.MoveRequested())

    def action_dispatch_transcriptions(self) -> None:
        self.dispatch_update(mvu.DispatchRequested())

    def action_new_course(self) -> None:
        self.dispatch_update(mvu.NewCourseRequested())

    def action_add_link(self) -> None:
        self.dispatch_update(mvu.AddLinkRequested())

    def action_calendar(self) -> None:
        self.dispatch_update(mvu.DateRequested())

    def action_rename_speakers(self) -> None:
        self.dispatch_update(mvu.RenameRequested())

    def action_recovery(self) -> None:
        self._maybe_show_recovery()

    def action_open_path(self) -> None:
        self.dispatch_update(mvu.OpenRequested())

    def action_course_filter(self) -> None:
        self.dispatch_update(mvu.CourseFilterRequested())

    def action_help(self) -> None:
        self.dispatch_update(mvu.HelpRequested())

    def action_back(self) -> None:
        self.dispatch_update(mvu.BackRequested())

    def action_activate(self) -> None:
        self.dispatch_update(mvu.ActivateCurrent())

    def action_request_exit(self) -> None:
        self.dispatch_update(mvu.ExitRequested())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Let the course filter receive printable global-binding keys."""

        if isinstance(self.focused, Input) and self.focused.id == "course-filter":
            if action not in {"next_pane", "back", "activate"}:
                return False
        return True

    def update_speaker_names(self, job_id: str, names: Mapping[str, str]) -> None:
        """Public helper for non-widget integrations that confirm speaker labels."""

        self.dispatch_update(mvu.SpeakerNamesConfirmed(job_id, names))

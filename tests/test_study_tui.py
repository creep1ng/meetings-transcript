from __future__ import annotations

from datetime import date
from typing import Mapping

import pytest
from textual.widgets import Input, ListView, Static

from study.presentation.textual import OrganizerApp, OrganizerConfig
from study.presentation.textual import update as mvu
from study.presentation.textual.ports import MoveResult
from study.presentation.textual.screens import (
    CalendarScreen,
    CloseJobsScreen,
    LinkScreen,
    MoveReviewScreen,
)
from study.presentation.textual.state import (
    CourseItem,
    JobItem,
    PackageItem,
    Pane,
    SourceCatalog,
    SourceItem,
    UIState,
)
from study.presentation.textual.widgets import CoursePane, PackagePane, SourcePane


class FakeFacade:
    def __init__(self, *, jobs: tuple[JobItem, ...] = ()) -> None:
        self.jobs = jobs
        self.closed: list[bool] = []
        self.dispatched: list[tuple[tuple[str, ...], str]] = []
        self.assigned: list[tuple[tuple[str, ...], str, str]] = []
        self.moved: list[tuple[tuple[str, ...], str, str]] = []
        self.created: list[str] = []
        self.opened: list[str] = []
        self.renamed: list[tuple[str, dict[str, str]]] = []
        self.links: list[tuple[str, str, str, str]] = []
        self.courses = (
            CourseItem("Algorithms", 2, "/library/Algorithms"),
            CourseItem("Distributed Systems", 1, "/library/Distributed Systems"),
        )
        self.packages = {
            "Algorithms": (
                PackageItem(
                    "Algorithms",
                    "2026-08-10",
                    "/library/Algorithms/2026-08-10",
                    ("lecture.mp3", "notes.pdf"),
                    has_manifest=True,
                ),
                PackageItem(
                    "Algorithms",
                    "2026-08-03",
                    "/library/Algorithms/2026-08-03",
                    ("week-1.pdf",),
                    has_manifest=True,
                ),
            ),
            "Distributed Systems": (),
        }

    async def browse_source(self, location: str | None) -> SourceCatalog:
        return SourceCatalog(
            location or "/source/inbox",
            None,
            (
                SourceItem(
                    "/source/inbox/class.mp3",
                    "class.mp3",
                    kind="audio",
                    size_bytes=4096,
                ),
                SourceItem(
                    "/source/inbox/slides.pdf",
                    "slides.pdf",
                    kind="document",
                    size_bytes=2048,
                ),
                SourceItem("/source/inbox/week-2", "week-2", is_directory=True),
            ),
        )

    async def list_courses(self) -> tuple[CourseItem, ...]:
        return self.courses

    async def list_packages(self, course_name: str) -> tuple[PackageItem, ...]:
        return self.packages.get(course_name, ())

    async def list_jobs(self) -> tuple[JobItem, ...]:
        return self.jobs

    async def create_course(self, name: str) -> CourseItem:
        self.created.append(name)
        return CourseItem(name, location=f"/library/{name}")

    async def ensure_package(self, course_name: str, class_date: str) -> PackageItem:
        return PackageItem(
            course_name,
            class_date,
            f"/library/{course_name}/{class_date}",
            has_manifest=True,
            adopted=False,
        )

    async def move_sources(
        self,
        locations: tuple[str, ...],
        course_name: str,
        class_date: str,
        resolutions: Mapping[str, str] | None = None,
        expected_generated_assets: Mapping[str, tuple[str, ...]] | None = None,
    ) -> MoveResult:
        self.moved.append((locations, course_name, class_date))
        return MoveResult(locations)

    async def dispatch_transcriptions(
        self, locations: tuple[str, ...], language: str
    ) -> tuple[JobItem, ...]:
        self.dispatched.append((locations, language))
        self.jobs = (
            JobItem("job-1", "class.mp3", "asr", 1, 1, "running", active=True),
        )
        return self.jobs

    async def assign_sources(
        self, locations: tuple[str, ...], course_name: str, class_date: str
    ) -> None:
        self.assigned.append((locations, course_name, class_date))

    async def rename_speakers(self, job_id: str, names: dict[str, str]) -> None:
        self.renamed.append((job_id, names))

    async def open_path(self, location: str) -> None:
        self.opened.append(location)

    async def retry_job(self, job_id: str) -> None:
        return None

    async def discard_job(self, job_id: str) -> None:
        return None

    async def add_link(
        self, url: str, label: str, course_name: str, class_date: str
    ) -> None:
        self.links.append((url, label, course_name, class_date))

    async def close(self, *, cancel_active: bool) -> None:
        self.closed.append(cancel_active)


def config() -> OrganizerConfig:
    return OrganizerConfig(
        source_root="/source",
        library_root="/library",
        start_path="/source/inbox",
        today="2026-08-20",
        detected_date="2026-08-17",
    )


def test_update_is_immutable_and_models_assignment_as_an_independent_effect() -> None:
    source = SourceItem("/source/class.mp3", "class.mp3", kind="audio")
    original = UIState(
        sources=(source,),
        source_index=0,
        selected_locations=frozenset({source.location}),
        confirmed_course="Algorithms",
    )

    transition = mvu.update(original, mvu.DateConfirmed("2026-08-17"))

    assert original.confirmed_date is None
    assert transition.state.confirmed_date == "2026-08-17"
    assert transition.state.assignment_running == frozenset({source.location})
    assert any(isinstance(effect, mvu.EnsurePackage) for effect in transition.effects)
    assignment = next(
        effect
        for effect in transition.effects
        if isinstance(effect, mvu.BeginAssignment)
    )
    assert assignment.locations == ("/source/class.mp3",)


def test_move_review_lists_only_generated_transcript_assets() -> None:
    audio = SourceItem("Downloads/class.mp3", "class.mp3", kind="audio")
    notes = SourceItem("Downloads/notes.pdf", "notes.pdf", kind="document")
    state = UIState(
        sources=(notes,),
        selected_locations=frozenset({audio.location, notes.location}),
        selected_sources=(audio, notes),
        confirmed_course="Algorithms",
        confirmed_date="2026-08-17",
        jobs=(
            JobItem(
                "job-1",
                "class.mp3",
                "ready_to_commit",
                1,
                1,
                "ready_to_commit",
                source_location=audio.location,
                generated_assets=("transcript JSON", "transcript Markdown"),
            ),
        ),
    )

    transition = mvu.update(state, mvu.MoveRequested())

    review = transition.effects[0]
    assert isinstance(review, mvu.ShowMoveReview)
    assert review.assets == (
        (
            "class.mp3",
            "audio",
            ("transcript JSON", "transcript Markdown"),
        ),
        ("notes.pdf", "document", ()),
    )
    assert review.generated_assets == (
        (audio.location, ("transcript JSON", "transcript Markdown")),
        (notes.location, ()),
    )


def test_move_completion_keeps_only_retained_sources_selected_and_refreshes() -> None:
    moved = SourceItem("Downloads/moved.pdf", "moved.pdf", kind="document")
    skipped = SourceItem("Downloads/skipped.pdf", "skipped.pdf", kind="document")
    retained = SourceItem("Downloads/changed.pdf", "changed.pdf", kind="document")
    state = UIState(
        selected_locations=frozenset(
            {moved.location, skipped.location, retained.location}
        ),
        selected_sources=(moved, skipped, retained),
        move_running=True,
        source_location="Downloads",
    )

    transition = mvu.update(
        state,
        mvu.MoveCompleted(
            (moved.location,),
            (retained.location,),
            (skipped.location,),
            "Algorithms",
        ),
    )

    assert transition.state.selected_locations == frozenset({retained.location})
    assert transition.state.selected_sources == (retained,)
    assert not transition.state.move_running
    assert (
        transition.state.status == "MOVE // committed 1; skipped 1; retained 1 changed"
    )
    assert transition.effects == (
        mvu.LoadSource("Downloads"),
        mvu.LoadPackages("Algorithms"),
    )

    blocked_exit = mvu.update(
        UIState(move_running=True, status="MOVE // committing"), mvu.ExitRequested()
    )
    assert blocked_exit.effects == ()
    assert blocked_exit.state.status == "EXIT // wait for the asset commit to finish"


@pytest.mark.asyncio
async def test_wide_layout_keeps_three_panes_visible_and_tab_moves_focus() -> None:
    app = OrganizerApp(FakeFacade(), config())

    async with app.run_test(size=(140, 36)) as pilot:
        await pilot.pause()
        assert app.query_one(SourcePane).display
        assert app.query_one(CoursePane).display
        assert app.query_one(PackagePane).display
        assert app.ui_state.active_pane is Pane.SOURCES
        assert app.query_one("#source-list").has_focus

        await pilot.press("tab")
        await pilot.pause()
        assert app.ui_state.active_pane is Pane.COURSES
        assert app.query_one("#course-list").has_focus

        await pilot.press("slash", "d", "i", "s", "t")
        await pilot.pause()
        assert app.ui_state.course_filter == "dist"
        assert app.ui_state.current_course.name == "Distributed Systems"

        await pilot.press("tab")
        await pilot.pause()
        assert app.ui_state.active_pane is Pane.PACKAGES
        assert app.query_one("#package-list").has_focus


@pytest.mark.asyncio
async def test_arrow_keys_move_the_cursor_in_every_workspace_panel() -> None:
    app = OrganizerApp(
        FakeFacade(),
        config(),
        initial_state=UIState(
            source_location="/source/inbox",
            confirmed_course="Algorithms",
        ),
    )

    async with app.run_test(size=(140, 36)) as pilot:
        await pilot.pause()
        source_list = app.query_one("#source-list", ListView)
        assert source_list.has_focus and source_list.index == 0
        await pilot.press("down")
        await pilot.pause()
        assert (
            source_list.index,
            app.ui_state.source_index,
            len(source_list.children),
            app.focused.id if app.focused else None,
        ) == (1, 1, 3, "source-list")
        await pilot.press("up")
        await pilot.pause()
        assert (source_list.index, app.ui_state.source_index) == (0, 0)
        app.dispatch_update(mvu.CourseConfirmed("Algorithms"))
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()
        course_list = app.query_one("#course-list", ListView)
        assert course_list.has_focus and course_list.index == 0
        await pilot.press("down")
        await pilot.pause()
        assert course_list.index == 1
        assert app.ui_state.course_index == 1

        await pilot.press("tab")
        await pilot.pause()
        package_list = app.query_one("#package-list", ListView)
        assert package_list.has_focus and package_list.index == 0
        await pilot.press("down")
        await pilot.pause()
        assert package_list.index == 1
        assert app.ui_state.package_index == 1


@pytest.mark.asyncio
async def test_narrow_layout_shows_one_active_pane_and_keeps_bindings() -> None:
    facade = FakeFacade()
    app = OrganizerApp(facade, config())

    async with app.run_test(size=(90, 32)) as pilot:
        await pilot.pause()
        assert app.query_one(SourcePane).display
        assert not app.query_one(CoursePane).display
        assert not app.query_one(PackagePane).display

        await pilot.press("space", "shift+t", "t")
        await pilot.pause()
        source = "/source/inbox/class.mp3"
        assert app.ui_state.selected_locations == frozenset({source})
        assert app.ui_state.transcription_locations == frozenset({source})
        assert facade.dispatched == [((source,), "es")]

        await pilot.press("tab")
        await pilot.pause()
        assert not app.query_one(SourcePane).display
        assert app.query_one(CoursePane).display
        assert not app.query_one(PackagePane).display


@pytest.mark.asyncio
async def test_course_and_calendar_require_confirmation_with_iso_navigation() -> None:
    facade = FakeFacade()
    app = OrganizerApp(facade, config())

    async with app.run_test(size=(140, 36)) as pilot:
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert app.ui_state.confirmed_course is None
        await pilot.click("#dialog-confirm")
        await pilot.pause()
        assert app.ui_state.confirmed_course == "Algorithms"

        await pilot.press("c")
        await pilot.pause()
        calendar = app.screen
        assert isinstance(calendar, CalendarScreen)
        assert calendar.selected_date == date(2026, 8, 17)
        assert str(calendar.query_one("#calendar-week-0").render()) == "31"
        assert app.ui_state.confirmed_date is None

        await pilot.click("#next-month")
        assert calendar.selected_date == date(2026, 9, 17)
        await pilot.click("#calendar-today")
        assert calendar.selected_date == date(2026, 8, 20)
        await pilot.click("#calendar-confirm")
        await pilot.pause()
        assert app.ui_state.confirmed_date == "2026-08-20"


@pytest.mark.asyncio
async def test_link_and_move_review_modals_complete_facade_actions() -> None:
    facade = FakeFacade()
    selected = "/source/inbox/class.mp3"
    initial_state = UIState(
        source_location="/source/inbox",
        selected_locations=frozenset({selected}),
        confirmed_course="Algorithms",
        confirmed_date="2026-08-17",
    )
    app = OrganizerApp(facade, config(), initial_state=initial_state)

    async with app.run_test(size=(140, 36)) as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LinkScreen)
        app.screen.query_one(
            "#link-url", Input
        ).value = "https://github.com/example/repo"
        app.screen.query_one("#link-label", Input).value = "Course repository"
        await pilot.click("#link-confirm")
        await pilot.pause()
        assert facade.links == [
            (
                "https://github.com/example/repo",
                "Course repository",
                "Algorithms",
                "2026-08-17",
            )
        ]

        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, MoveReviewScreen)
        await pilot.press("m")
        await pilot.pause()
        assert (
            sum(isinstance(screen, MoveReviewScreen) for screen in app.screen_stack)
            == 1
        )
        detail = str(app.screen.query_one(".dialog-detail", Static).render())
        assert "Algorithms / 2026-08-17" in detail
        assert "class.mp3 [audio]" in detail
        await pilot.click("#move-review-confirm")
        await pilot.pause()
        assert facade.moved == [((selected,), "Algorithms", "2026-08-17")]


@pytest.mark.asyncio
async def test_exit_with_active_or_recoverable_jobs_prompts_and_closes_facade() -> None:
    jobs = (
        JobItem("active", "class.mp3", "align", 1, 2, "running", active=True),
        JobItem("partial", "seminar.mp4", "diarize", 2, 2, "partial", recoverable=True),
    )
    facade = FakeFacade(jobs=jobs)
    app = OrganizerApp(facade, config())

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, CloseJobsScreen)
        assert app.screen.query_one("#close-cancel")
        assert app.screen.query_one("#close-retain")
        await pilot.click("#close-retain")
        await pilot.pause()
        assert facade.closed == [False]


def test_job_progress_is_textual_without_an_invented_percentage() -> None:
    job = JobItem("job-1", "lecture.mp3", "diarize", 2, 5, "running", active=True)

    assert job.progress_text == "lecture.mp3 // diarize // 2/5"
    assert "%" not in job.progress_text

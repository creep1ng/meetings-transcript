"""Workspace panes that render immutable UI state without accessing adapters."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Label, ListItem, ListView, Static

from ..state import Pane, UIState


def _size(value: int | None) -> str:
    if value is None:
        return ""
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


class SourcePane(Vertical):
    _list_key: object = None

    def compose(self) -> ComposeResult:
        yield Label("01 // INTAKE", classes="pane-heading")
        yield Static("-", id="source-path", classes="pane-context", markup=False)
        yield ListView(id="source-list", classes="pane-list")

    def show_state(self, state: UIState) -> None:
        self.set_class(state.active_pane is Pane.SOURCES, "active-pane")
        self.query_one("#source-path", Static).update(state.source_location or "-")
        source_list = self.query_one("#source-list", ListView)
        key = (state.sources, state.selected_locations, state.transcription_locations)
        if key != self._list_key:
            self._list_key = key
            source_list.clear()
            rows: list[ListItem] = []
            for source in state.sources:
                if source.is_parent:
                    label = "<-  .."
                elif source.is_directory:
                    label = f"DIR  {source.name}/"
                else:
                    selected = (
                        "X" if source.location in state.selected_locations else " "
                    )
                    transcribe = (
                        "T" if source.location in state.transcription_locations else " "
                    )
                    details = " ".join(
                        part for part in (source.kind, _size(source.size_bytes)) if part
                    )
                    label = (
                        f"[{selected}][{transcribe}] {source.name}  {details}".rstrip()
                    )
                rows.append(ListItem(Label(label, markup=False)))
            source_list.extend(rows)
        if state.source_index is not None and state.sources:
            target = min(state.source_index, len(state.sources) - 1)
            if source_list.index != target:
                source_list.index = target


class CoursePane(Vertical):
    _list_key: object = None

    def compose(self) -> ComposeResult:
        yield Label("02 // COURSE RACK", classes="pane-heading")
        yield Input(placeholder="/ fuzzy course filter", id="course-filter")
        yield ListView(id="course-list", classes="pane-list")

    def show_state(self, state: UIState) -> None:
        self.set_class(state.active_pane is Pane.COURSES, "active-pane")
        course_filter = self.query_one("#course-filter", Input)
        if course_filter.value != state.course_filter:
            course_filter.value = state.course_filter
        course_list = self.query_one("#course-list", ListView)
        key = (state.visible_courses, state.confirmed_course)
        if key != self._list_key:
            self._list_key = key
            course_list.clear()
            rows = []
            for course in state.visible_courses:
                confirmed = "LOCK" if course.name == state.confirmed_course else "    "
                rows.append(
                    ListItem(
                        Label(
                            f"{confirmed}  {course.name}  [{course.package_count}]",
                            markup=False,
                        )
                    )
                )
            course_list.extend(rows)
        if state.course_index is not None and state.visible_courses:
            target = min(state.course_index, len(state.visible_courses) - 1)
            if course_list.index != target:
                course_list.index = target


class PackagePane(Vertical):
    _list_key: object = None

    def compose(self) -> ComposeResult:
        yield Label("03 // CLASS PACKAGES", classes="pane-heading")
        yield Static(
            "Confirm a course",
            id="package-context",
            classes="pane-context",
            markup=False,
        )
        yield ListView(id="package-list", classes="package-list")
        yield Static("No package selected", id="package-detail", markup=False)

    def show_state(self, state: UIState) -> None:
        self.set_class(state.active_pane is Pane.PACKAGES, "active-pane")
        context = state.confirmed_course or "Confirm a course"
        if state.confirmed_date:
            context = f"{context} // target {state.confirmed_date}"
        self.query_one("#package-context", Static).update(context)
        package_list = self.query_one("#package-list", ListView)
        key = (state.packages, state.confirmed_date)
        if key != self._list_key:
            self._list_key = key
            package_list.clear()
            rows = []
            for package in state.packages:
                marker = "*" if package.class_date == state.confirmed_date else " "
                mode = "INDEX" if package.has_manifest else "ADOPT"
                rows.append(
                    ListItem(
                        Label(
                            f"{marker} {package.class_date}  {mode}  {len(package.asset_names)} asset(s)",
                            markup=False,
                        )
                    )
                )
            package_list.extend(rows)
        if state.package_index is not None and state.packages:
            target = min(state.package_index, len(state.packages) - 1)
            if package_list.index != target:
                package_list.index = target
        package = state.current_package
        if not package:
            detail = "No packages indexed. C assigns a class date."
        else:
            assets = (
                "\n".join(f"+ {name}" for name in package.asset_names)
                or "+ empty package"
            )
            changes = "\n".join(f"> {change}" for change in package.planned_changes)
            detail = assets if not changes else f"{assets}\n\nPLANNED\n{changes}"
        self.query_one("#package-detail", Static).update(detail)


class JobStrip(Static):
    def show_state(self, state: UIState) -> None:
        active = next((job for job in state.jobs if job.active), None)
        if active:
            text = f"WORKER // {active.progress_text}"
        elif state.jobs:
            job = state.jobs[0]
            text = f"WORKER // {job.file_name} // {job.status} // {job.position}/{job.total}"
        else:
            text = "WORKER // idle"
        if state.assignment_running:
            text += f"    ASSIGN // {len(state.assignment_running)} running"
        self.update(text)


class StatusStrip(Static):
    def show_state(self, state: UIState) -> None:
        course = state.confirmed_course or "unconfirmed"
        class_date = state.confirmed_date or "unconfirmed"
        mode = "SINGLE" if state.compact else "TRIPLE"
        self.update(
            f"{state.status}    TARGET // {course} / {class_date}    VIEW // {mode}"
        )

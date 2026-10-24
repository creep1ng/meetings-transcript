"""Input, confirmation, help, recovery, and speaker modal screens."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..ports import MoveConflict
from ..state import JobItem


class ConfirmationScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Back")]

    def __init__(self, title: str, detail: str, confirm_label: str = "CONFIRM") -> None:
        super().__init__()
        self.prompt_title = title
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation-dialog", classes="dialog-shell small-dialog"):
            yield Label(self.prompt_title, classes="dialog-title")
            yield Static(self.detail, classes="dialog-detail")
            with Horizontal(classes="dialog-actions"):
                yield Button(self.confirm_label, id="dialog-confirm", variant="primary")
                yield Button("BACK", id="dialog-back")

    def on_mount(self) -> None:
        self.query_one("#dialog-confirm", Button).focus()

    @on(Button.Pressed, "#dialog-confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#dialog-back")
    def back(self) -> None:
        self.dismiss(False)


class CourseNameScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Back")]

    def compose(self) -> ComposeResult:
        with Vertical(id="course-dialog", classes="dialog-shell small-dialog"):
            yield Label("NEW COURSE // DIRECT CHILD", classes="dialog-title")
            yield Static("Enter a course name. Creation requires another confirmation.")
            yield Input(placeholder="Course name", id="course-name", max_length=80)
            with Horizontal(classes="dialog-actions"):
                yield Button("REVIEW", id="course-review", variant="primary")
                yield Button("BACK", id="course-back")

    def on_mount(self) -> None:
        self.query_one("#course-name", Input).focus()

    @on(Input.Submitted, "#course-name")
    def submitted(self) -> None:
        self.action_submit()

    @on(Button.Pressed, "#course-review")
    def review(self) -> None:
        self.action_submit()

    @on(Button.Pressed, "#course-back")
    def back(self) -> None:
        self.action_cancel()

    def action_submit(self) -> None:
        value = self.query_one("#course-name", Input).value
        if value.strip():
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("h", "dismiss", "Back", show=False),
    ]

    HELP = """TAB       cycle workspace panes
SPACE     select material for move
SHIFT+T   mark audio/video for transcription
T         dispatch marked recordings immediately
M         commit selected materials after assignment
ENTER     open directory or confirm highlighted course
N / C     new course / class date calendar
L         add a URL reference without fetching it
R / O     rename speakers / open in OS
U         retry, retain, or discard recoverable work
/         fuzzy course navigation
ESC       back, then this menu
Q         close organizer"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog", classes="dialog-shell"):
            yield Label("OPERATOR MANUAL // STUDY WORKSPACE", classes="dialog-title")
            yield Static(self.HELP, id="help-copy")
            yield Static(
                "Course and date are never accepted implicitly. Worker progress reports phase and queue position only.",
                classes="dialog-detail",
            )
            yield Button("RETURN", id="help-return", variant="primary")

    @on(Button.Pressed, "#help-return")
    def close_help(self) -> None:
        self.dismiss(None)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class CloseJobsScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, active_jobs: int, recoverable_jobs: int) -> None:
        super().__init__()
        self.active_jobs = active_jobs
        self.recoverable_jobs = recoverable_jobs

    def compose(self) -> ComposeResult:
        with Vertical(id="close-dialog", classes="dialog-shell"):
            yield Label("WORK REMAINS // CLOSE DECISION", classes="dialog-title")
            yield Static(
                f"Active or assigning: {self.active_jobs}\nRecoverable: {self.recoverable_jobs}\n\nThe worker process always terminates. Retain keeps recoverable checkpoints; cancel marks active jobs cancelled.",
                classes="dialog-detail",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("CANCEL JOBS + EXIT", id="close-cancel", variant="error")
                yield Button("RETAIN + EXIT", id="close-retain", variant="primary")
                yield Button("BACK", id="close-back")

    @on(Button.Pressed, "#close-cancel")
    def cancel_jobs(self) -> None:
        self.dismiss("cancel")

    @on(Button.Pressed, "#close-retain")
    def retain_jobs(self) -> None:
        self.dismiss("retain")

    @on(Button.Pressed, "#close-back")
    def back(self) -> None:
        self.dismiss(None)


class SpeakerRenameScreen(ModalScreen[dict[str, str] | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, job: JobItem) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="speaker-dialog", classes="dialog-shell"):
            yield Label(
                f"SPEAKER LABELS // {self.job.file_name}", classes="dialog-title"
            )
            yield Static(
                "Anonymous IDs remain canonical. These values are display names only."
            )
            for index, speaker in enumerate(self.job.speakers):
                yield Label(speaker.anonymous_id, classes="speaker-id")
                yield Input(
                    value=speaker.display_name,
                    id=f"speaker-name-{index}",
                    max_length=80,
                )
            with Horizontal(classes="dialog-actions"):
                yield Button("APPLY NAMES", id="speaker-confirm", variant="primary")
                yield Button("BACK", id="speaker-back")

    @on(Button.Pressed, "#speaker-confirm")
    def confirm(self) -> None:
        names = {
            speaker.anonymous_id: self.query_one(f"#speaker-name-{index}", Input).value
            for index, speaker in enumerate(self.job.speakers)
        }
        if all(name.strip() for name in names.values()):
            self.dismiss(names)

    @on(Button.Pressed, "#speaker-back")
    def back(self) -> None:
        self.dismiss(None)


class MoveConflictScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, conflict: MoveConflict) -> None:
        super().__init__()
        self.conflict = conflict

    def compose(self) -> ComposeResult:
        with Vertical(id="conflict-dialog", classes="dialog-shell"):
            yield Label("CONTENT CONFLICT // DECISION REQUIRED", classes="dialog-title")
            yield Static(
                f"{self.conflict.filename}\n\nExisting: {self.conflict.existing_sha256[:16]}...\nIncoming: {self.conflict.incoming_sha256[:16]}...\n\nNo overwrite occurs without this decision.",
                classes="dialog-detail",
                markup=False,
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("REPLACE", id="conflict-replace", variant="error")
                yield Button("KEEP BOTH", id="conflict-keep", variant="primary")
                yield Button("SKIP", id="conflict-skip")
                yield Button("CANCEL MOVE", id="conflict-cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        decisions = {
            "conflict-replace": "replace",
            "conflict-keep": "keep-both",
            "conflict-skip": "skip",
            "conflict-cancel": "cancel",
        }
        if event.button.id in decisions:
            self.dismiss(decisions[event.button.id])


class RecoveryScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss('retain')", "Retain")]

    def __init__(self, job: JobItem) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="recovery-dialog", classes="dialog-shell"):
            yield Label("RECOVERABLE WORK // DECISION", classes="dialog-title")
            yield Static(
                f"{self.job.file_name}\nState: {self.job.status}\nResume phase: {self.job.phase}\n\nRetry validates the source and every retained artifact first.",
                classes="dialog-detail",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("RETRY", id="recovery-retry", variant="primary")
                yield Button("DISCARD", id="recovery-discard", variant="error")
                yield Button("RETAIN", id="recovery-retain")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        decisions = {
            "recovery-retry": "retry",
            "recovery-discard": "discard",
            "recovery-retain": "retain",
        }
        if event.button.id in decisions:
            self.dismiss(decisions[event.button.id])


class MoveReviewScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Back")]

    def __init__(
        self,
        course_name: str,
        class_date: str,
        assets: tuple[tuple[str, str, tuple[str, ...]], ...],
    ) -> None:
        super().__init__()
        self.course_name = course_name
        self.class_date = class_date
        self.assets = assets

    def compose(self) -> ComposeResult:
        lines = []
        for name, kind, generated_assets in self.assets:
            suffix = " + " + ", ".join(generated_assets) if generated_assets else ""
            lines.append(f"{name} [{kind}]{suffix}")
        with Vertical(id="move-review-dialog", classes="dialog-shell"):
            yield Label("COMMIT PLAN // FINAL REVIEW", classes="dialog-title")
            yield Static(
                f"Target: {self.course_name} / {self.class_date}\n\n"
                + "\n".join(lines)
                + "\n\nSources are deleted only after verified publication and manifest commit.",
                classes="dialog-detail",
                markup=False,
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("COMMIT PLAN", id="move-review-confirm", variant="primary")
                yield Button("BACK", id="move-review-back")

    @on(Button.Pressed, "#move-review-confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#move-review-back")
    def back(self) -> None:
        self.dismiss(False)


class LinkScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def compose(self) -> ComposeResult:
        with Vertical(id="link-dialog", classes="dialog-shell small-dialog"):
            yield Label("LINK ASSET // RECORD ONLY", classes="dialog-title")
            yield Static("The URL is normalized and indexed; no content is fetched.")
            yield Input(placeholder="https://github.com/...", id="link-url")
            yield Input(placeholder="Label", id="link-label", max_length=200)
            with Horizontal(classes="dialog-actions"):
                yield Button("REVIEW + ADD", id="link-confirm", variant="primary")
                yield Button("BACK", id="link-back")

    def on_mount(self) -> None:
        self.query_one("#link-url", Input).focus()

    @on(Button.Pressed, "#link-confirm")
    def confirm(self) -> None:
        url = self.query_one("#link-url", Input).value.strip()
        label = self.query_one("#link-label", Input).value.strip()
        if url and label:
            self.dismiss((url, label))

    @on(Button.Pressed, "#link-back")
    def back(self) -> None:
        self.dismiss(None)

"""ISO-week, Monday-first calendar with explicit date confirmation."""

from __future__ import annotations

from calendar import Calendar, month_name
from datetime import date, timedelta

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class CalendarDay(Button):
    def __init__(self, row: int, column: int) -> None:
        super().__init__("", id=f"calendar-day-{row}-{column}")
        self.date_value = date.min


def _month_weeks(year: int, month: int) -> tuple[tuple[date, ...], ...]:
    weeks = [
        tuple(week) for week in Calendar(firstweekday=0).monthdatescalendar(year, month)
    ]
    while len(weeks) < 6:
        start = weeks[-1][-1] + timedelta(days=1)
        weeks.append(tuple(start + timedelta(days=offset) for offset in range(7)))
    return tuple(weeks[:6])


def _shift_month(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


class CalendarScreen(ModalScreen[date | None]):
    """Select a class date without accepting an implicit default."""

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
        Binding("enter", "confirm", "Confirm"),
        Binding("left", "previous_month", "Previous month", show=False),
        Binding("right", "next_month", "Next month", show=False),
        Binding("pageup", "previous_year", "Previous year", show=False),
        Binding("pagedown", "next_year", "Next year", show=False),
        Binding("t", "today", "Today", show=False),
    ]

    def __init__(self, *, prefill: date, today: date) -> None:
        super().__init__()
        self.today = today
        self.selected_date = prefill
        self.displayed_month = prefill.replace(day=1)

    def compose(self) -> ComposeResult:
        with Vertical(id="calendar-dialog", classes="dialog-shell"):
            yield Label("CLASS DATE // ISO CALENDAR", classes="dialog-title")
            with Horizontal(id="calendar-navigation"):
                yield Button("<< YEAR", id="previous-year")
                yield Button("< MONTH", id="previous-month")
                yield Static("", id="calendar-month")
                yield Button("MONTH >", id="next-month")
                yield Button("YEAR >>", id="next-year")
            with Grid(id="calendar-grid"):
                yield Static("WK", classes="calendar-heading")
                for weekday in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"):
                    yield Static(weekday, classes="calendar-heading")
                for row in range(6):
                    yield Static("", id=f"calendar-week-{row}", classes="calendar-week")
                    for column in range(7):
                        yield CalendarDay(row, column)
            yield Static("", id="calendar-selection")
            with Horizontal(classes="dialog-actions"):
                yield Button("TODAY", id="calendar-today")
                yield Button("CONFIRM DATE", id="calendar-confirm", variant="primary")
                yield Button("BACK", id="calendar-back")

    def on_mount(self) -> None:
        self._refresh_calendar()
        self.query_one("#calendar-confirm", Button).focus()

    def _refresh_calendar(self) -> None:
        year = self.displayed_month.year
        month = self.displayed_month.month
        self.query_one("#calendar-month", Static).update(
            f"{month_name[month].upper()} {year}"
        )
        for row, week in enumerate(_month_weeks(year, month)):
            self.query_one(f"#calendar-week-{row}", Static).update(
                f"{week[0].isocalendar().week:02d}"
            )
            for column, value in enumerate(week):
                button = self.query_one(f"#calendar-day-{row}-{column}", CalendarDay)
                button.date_value = value
                button.label = str(value.day)
                button.set_class(value.month != month, "outside-month")
                button.set_class(value == self.today, "today")
                button.set_class(value == self.selected_date, "selected-date")
        self.query_one("#calendar-selection", Static).update(
            f"SELECTED // {self.selected_date.isoformat()} // confirmation required"
        )

    def _navigate(self, months: int) -> None:
        self.selected_date = _shift_month(self.selected_date, months)
        self.displayed_month = self.selected_date.replace(day=1)
        self._refresh_calendar()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if isinstance(event.button, CalendarDay):
            self.selected_date = event.button.date_value
            self.displayed_month = self.selected_date.replace(day=1)
            self._refresh_calendar()
        elif button_id == "previous-year":
            self.action_previous_year()
        elif button_id == "previous-month":
            self.action_previous_month()
        elif button_id == "next-month":
            self.action_next_month()
        elif button_id == "next-year":
            self.action_next_year()
        elif button_id == "calendar-today":
            self.action_today()
        elif button_id == "calendar-confirm":
            self.action_confirm()
        elif button_id == "calendar-back":
            self.action_cancel()

    def action_previous_month(self) -> None:
        self._navigate(-1)

    def action_next_month(self) -> None:
        self._navigate(1)

    def action_previous_year(self) -> None:
        self._navigate(-12)

    def action_next_year(self) -> None:
        self._navigate(12)

    def action_today(self) -> None:
        self.selected_date = self.today
        self.displayed_month = self.today.replace(day=1)
        self._refresh_calendar()

    def action_confirm(self) -> None:
        self.dismiss(self.selected_date)

    def action_cancel(self) -> None:
        self.dismiss(None)

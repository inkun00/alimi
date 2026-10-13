"""Date-based reminder colors, without inventing deadlines for monthly events."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Event


@dataclass(frozen=True)
class UrgencyState:
    """One reminder's color palette and human-readable deadline status."""

    code: str
    background: str
    foreground: str
    border: str
    hover: str
    label: str
    days_remaining: int | None = None


URGENCY_PALETTES = (
    UrgencyState("normal", "#fff0ad", "#493e1e", "#ddc872", "#f3df8d", ""),
    UrgencyState("three_days", "#ffe08a", "#493e1e", "#d5af56", "#f4d278", "D-3"),
    UrgencyState("two_days", "#ffc75f", "#493e1e", "#ce982f", "#f4b94f", "D-2"),
    UrgencyState("one_day", "#ffad3d", "#493e1e", "#c88123", "#f49f2d", "D-1"),
    UrgencyState("due", "#ff8d3a", "#493e1e", "#bd6728", "#f47e2b", "오늘 마감"),
)
_PALETTES = {palette.code: palette for palette in URGENCY_PALETTES}


def event_urgency(event: Event, today: date | None = None) -> UrgencyState:
    """Return a palette for the actual day deadline, relative to ``today``.

    A dated range uses its end date, and a single-day event uses its start date.
    Monthly schedules remain neutral: their internally stored first/last days
    are scheduling boundaries, not deadlines supplied by the user. Overdue
    reminders keep the orange deadline color until they are completed.
    """
    neutral = _PALETTES["normal"]
    if event.date_precision == "month":
        return replace(neutral, code="month", label="월 일정")
    deadline = None
    for value in (event.end_date, event.start_date):
        if not value:
            continue
        try:
            deadline = date.fromisoformat(value)
            break
        except (TypeError, ValueError):
            continue
    if deadline is None:
        return replace(neutral, code="undated", label="날짜 미정")
    remaining = (deadline - (today if today is not None else date.today())).days
    if remaining <= 0:
        return replace(_PALETTES["due"], days_remaining=remaining,
                       label="오늘 마감" if remaining == 0 else "기한 지남")
    code = {1: "one_day", 2: "two_days", 3: "three_days"}.get(remaining, "normal")
    return replace(_PALETTES[code], days_remaining=remaining, label=f"D-{remaining}")

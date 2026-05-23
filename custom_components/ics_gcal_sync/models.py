"""Data models for the ICS to Google Calendar Sync integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class CalendarSource:
    """One ICS feed → target Google Calendar mapping."""

    id: str
    ics_url: str
    target_calendar: str
    team_name: str = ""
    color_id: str = ""
    enabled: bool = True


@dataclass
class ParsedEvent:
    """An event parsed from an ICS feed, ready for Google Calendar comparison."""

    uid: str
    composite_id: str       # uid + '_' + team_name, or plain uid if no team
    team_name: str
    summary: str
    start: datetime | date
    end: datetime | date
    is_all_day: bool
    location: str | None
    description: str | None
    recurrence: list[str]   # RRULE/EXRULE/EXDATE/RDATE lines for Google Calendar
    color_id: str | None
    status: str | None      # "confirmed" | "tentative" | "cancelled"
    url: str | None
    md5: str                # hash of normalized ICS text (DTSTAMP stripped)


@dataclass
class SyncResult:
    """Result of a single sync pass for one target calendar."""

    calendar_name: str
    added: int = 0
    modified: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.calendar_name}: "
            f"+{self.added} ~{self.modified} -{self.removed}"
            + (f" ({len(self.errors)} errors)" if self.errors else "")
        )

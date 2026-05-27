"""Data models for the ICS to Google Calendar Sync integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class CalendarSource:
    """One calendar feed → target Google Calendar mapping."""

    id: str
    ics_urls: list[str]
    target_calendar: str
    prefix: str = ""
    color_id: str = ""
    enabled: bool = True
    use_se_enricher: bool = False
    se_account_id: str = ""
    # Source type: "ics" (default) or "se_tourney"
    source_type: str = "ics"
    # Display name prepended to event titles only on shareable calendar copies.
    # Never affects composite_id or iCalUID, so deduplication stays intact.
    shared_display_name: str = ""
    # SportsEngine Tourney fields (populated when source_type == "se_tourney")
    se_tourney_tournament_id: str = ""
    se_tourney_division_id: str = ""
    se_tourney_team_id: str = ""
    se_tourney_tournament_name: str = ""
    se_tourney_division_name: str = ""
    se_tourney_team_name: str = ""
    se_tourney_game_duration: int = 90


@dataclass
class ParsedEvent:
    """An event parsed from an ICS feed, ready for Google Calendar comparison."""

    uid: str
    composite_id: str       # uid + '_' + prefix, or plain uid if no prefix
    prefix: str
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
    md5: str                # hash of normalized ICS text (DTSTAMP stripped) + enrichment suffix
    skip_title_case: bool = False  # set True for sources whose names should not be title-cased
    shared_display_name: str = ""  # set on shareable-calendar copies; prepended as "(Name) " in summary

    # Enrichment support: raw ical text (DTSTAMP stripped) is stored so enrichers
    # can append their output and trigger a MD5 recomputation that includes
    # enriched fields (matching the GAS script behaviour of embedding seLocation
    # in the event before hashing).
    raw_ical_no_dtstamp: str = ""
    enrichment_suffix: str = ""


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

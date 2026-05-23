"""Fetch and parse ICS/iCal feeds into ParsedEvent objects."""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone

import aiohttp
from icalendar import Calendar

from .models import ParsedEvent

_LOGGER = logging.getLogger(__name__)

# Properties whose recurrence lines are forwarded to Google Calendar
_RECURRENCE_PROPS = ("RRULE", "EXRULE", "EXDATE", "RDATE")


async def async_fetch_and_parse(
    session: aiohttp.ClientSession,
    url: str,
    team_name: str = "",
    color_id: str = "",
) -> list[ParsedEvent]:
    """Fetch one ICS URL and return a list of ParsedEvents."""
    content = await _fetch(session, url)
    if content is None:
        return []
    return _parse(content, team_name, color_id)


async def _fetch(session: aiohttp.ClientSession, url: str) -> str | None:
    url = url.replace("webcal://", "https://")
    try:
        async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                return await resp.text()
            _LOGGER.warning("HTTP %d fetching %s", resp.status, url)
    except Exception as err:
        _LOGGER.error("Error fetching %s: %s", url, err)
    return None


def _parse(content: str, team_name: str, color_id: str) -> list[ParsedEvent]:
    try:
        cal = Calendar.from_ical(content)
    except Exception as err:
        _LOGGER.error("Failed to parse ICS content: %s", err)
        return []

    seen_uids: set[str] = set()
    events: list[ParsedEvent] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        # Skip cancelled events
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue

        # Skip exception instances (RECURRENCE-ID events); handled in a later phase
        if component.get("RECURRENCE-ID") is not None:
            continue

        uid = str(component.get("UID", "")).strip()
        if not uid:
            uid = hashlib.md5(component.to_ical()).hexdigest()

        # Deduplicate: first occurrence of uid+team_name wins
        dedup_key = f"{uid}|{team_name}"
        if dedup_key in seen_uids:
            continue
        seen_uids.add(dedup_key)

        composite_id = f"{uid}_{team_name}" if team_name else uid

        dtstart = component.decoded("DTSTART", None)
        if dtstart is None:
            continue
        dtend = component.decoded("DTEND", None)

        is_all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)

        if dtend is None:
            dtend = dtstart + timedelta(days=1) if is_all_day else dtstart + timedelta(hours=1)

        # Normalize: all-day end must be after start for Google Calendar
        if is_all_day and dtend == dtstart:
            dtend = dtstart + timedelta(days=1)

        recurrence = _extract_recurrence(component)
        md5 = _compute_md5(component)

        location = str(component.get("LOCATION", "")).strip() or None
        description = str(component.get("DESCRIPTION", "")).strip() or None
        status = str(component.get("STATUS", "")).lower().strip() or None
        url_val = str(component.get("URL", "")).strip() or None

        events.append(
            ParsedEvent(
                uid=uid,
                composite_id=composite_id,
                team_name=team_name,
                summary=str(component.get("SUMMARY", "")).strip(),
                start=dtstart,
                end=dtend,
                is_all_day=is_all_day,
                location=location,
                description=description,
                recurrence=recurrence,
                color_id=color_id or None,
                status=status,
                url=url_val if url_val and url_val.startswith("http") else None,
                md5=md5,
            )
        )

    return events


def _extract_recurrence(component) -> list[str]:
    """Extract RRULE/EXRULE/EXDATE/RDATE lines as GCal-compatible strings.

    Serializes the full event, unfolds wrapped lines, then picks the
    recurrence-related ones so that TZID parameters are included correctly.
    """
    raw = component.to_ical().decode("utf-8", errors="replace")
    # Unfold: continuation lines start with a single space or tab
    unfolded = raw.replace("\r\n ", "").replace("\r\n\t", "").replace("\n ", "").replace("\n\t", "")

    result = []
    for line in unfolded.splitlines():
        prop_name = line.split(";", 1)[0].split(":", 1)[0].upper()
        if prop_name in _RECURRENCE_PROPS:
            result.append(line.strip())
    return result


def _compute_md5(component) -> str:
    """Compute a stable MD5 of the event, excluding DTSTAMP.

    DTSTAMP changes on every feed fetch; stripping it means the hash only
    changes when meaningful event data changes.
    """
    raw = component.to_ical().decode("utf-8", errors="replace")
    lines = [
        line for line in raw.splitlines()
        if not line.startswith("DTSTAMP")
    ]
    return hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()

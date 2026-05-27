"""Core sync logic: ICS feeds → Google Calendar for one target calendar group."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from itertools import groupby

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADD_EVENTS,
    CONF_LOCATION_ABBREVIATIONS,
    CONF_MODIFY_EVENTS,
    CONF_REMOVE_EVENTS,
    CONF_REMOVE_PAST_EVENTS,
    CONF_SE_TITLE_REMOVALS,
    CONF_TITLE_CASE,
    DEFAULT_ADD_EVENTS,
    DEFAULT_MODIFY_EVENTS,
    DEFAULT_REMOVE_EVENTS,
    DEFAULT_REMOVE_PAST_EVENTS,
    DEFAULT_TITLE_CASE,
    SOURCE_TYPE_SE_TOURNEY,
)
from .enrichers import BaseEnricher
from .google_calendar_client import GoogleCalendarClient, GoogleCalendarError
from .ics_parser import async_fetch_and_parse, recompute_md5
from .models import CalendarSource, ParsedEvent, SyncResult

_LOGGER = logging.getLogger(__name__)


async def async_sync_all(
    hass: HomeAssistant,
    client: GoogleCalendarClient,
    sources: list[CalendarSource],
    options: dict,
    enrichers: list[BaseEnricher] | None = None,
) -> list[SyncResult]:
    """Sync all enabled sources grouped by target calendar."""
    enabled = [s for s in sources if s.enabled]
    if not enabled:
        return []

    active_enrichers = enrichers or []
    calendar_tz = await client.get_timezone()

    # Prime enrichers once before any calendar is processed
    for enricher in active_enrichers:
        await enricher.async_prepare(hass, enabled, options)

    enabled.sort(key=lambda s: s.target_calendar)
    results: list[SyncResult] = []

    for calendar_name, group_iter in groupby(enabled, key=lambda s: s.target_calendar):
        group = list(group_iter)
        result = await _sync_calendar_group(
            hass, client, calendar_name, group, calendar_tz, options, active_enrichers
        )
        results.append(result)
        _LOGGER.info("Sync complete: %s", result)

    return results


async def _sync_calendar_group(
    hass: HomeAssistant,
    client: GoogleCalendarClient,
    calendar_name: str,
    sources: list[CalendarSource],
    calendar_tz: str,
    options: dict,
    enrichers: list[BaseEnricher],
) -> SyncResult:
    result = SyncResult(calendar_name=calendar_name)
    add_events = options.get(CONF_ADD_EVENTS, DEFAULT_ADD_EVENTS)
    modify_events = options.get(CONF_MODIFY_EVENTS, DEFAULT_MODIFY_EVENTS)
    remove_events = options.get(CONF_REMOVE_EVENTS, DEFAULT_REMOVE_EVENTS)
    remove_past = options.get(CONF_REMOVE_PAST_EVENTS, DEFAULT_REMOVE_PAST_EVENTS)
    title_case = options.get(CONF_TITLE_CASE, DEFAULT_TITLE_CASE)

    # Determine which enrichers are active for this calendar group
    group_enrichers = _select_enrichers(sources, enrichers)

    try:
        calendar = await client.get_or_create_calendar(calendar_name, calendar_tz)
        calendar_id = calendar["id"]

        # Load existing GAS-managed events
        existing_gcal = await client.list_managed_events(calendar_id)
        _LOGGER.debug("%s: %d existing managed events", calendar_name, len(existing_gcal))

        existing_by_id: dict[str, dict] = {}
        existing_md5s: set[str] = set()
        for gcal_event in existing_gcal:
            props = gcal_event.get("extendedProperties", {}).get("private", {})
            event_id = props.get("rec-id") or props.get("id")
            md5 = props.get("MD5")
            if event_id:
                existing_by_id[event_id] = gcal_event
            if md5:
                existing_md5s.add(md5)

        # Fetch and parse all sources (ICS or TourneyMachine)
        http_session = async_get_clientsession(hass)
        parsed_events: list[ParsedEvent] = []
        seen_composite_ids: set[str] = set()

        for source in sources:
            if source.source_type == SOURCE_TYPE_SE_TOURNEY:
                from .se_tourney_parser import async_fetch_games
                fetched = await async_fetch_games(
                    hass,
                    source.se_tourney_tournament_id,
                    source.se_tourney_division_id,
                    source.se_tourney_team_id,
                    source.prefix,
                    source.color_id,
                    source.se_tourney_game_duration,
                )
                for event in fetched:
                    if event.composite_id not in seen_composite_ids:
                        seen_composite_ids.add(event.composite_id)
                        if source.shared_display_name:
                            event.shared_display_name = source.shared_display_name
                        parsed_events.append(event)
            else:
                for url in source.ics_urls:
                    fetched = await async_fetch_and_parse(
                        http_session, url, source.prefix, source.color_id
                    )
                    for event in fetched:
                        if event.composite_id not in seen_composite_ids:
                            seen_composite_ids.add(event.composite_id)
                            if source.shared_display_name:
                                event.shared_display_name = source.shared_display_name
                            parsed_events.append(event)

        _LOGGER.debug("%s: parsed %d events", calendar_name, len(parsed_events))

        # Apply enrichers and recompute MD5
        for enricher in group_enrichers:
            enriched: list[ParsedEvent] = []
            for event in parsed_events:
                enriched.append(await enricher.async_enrich(event, options))
            parsed_events = enriched

        title_removals: list[str] = options.get(CONF_SE_TITLE_REMOVALS, [])
        location_abbreviations: dict[str, str] = options.get(CONF_LOCATION_ABBREVIATIONS, {})

        for event in parsed_events:
            # Apply location abbreviations universally. The SE enricher already handles
            # SE-API-resolved locations with a prefix match; this covers raw ICS locations
            # for all sources (including non-SE calendars with no enricher active).
            if location_abbreviations and event.location:
                event.location = _apply_location_abbreviations(event.location, location_abbreviations)

            # Strip "[N] - " seed/bracket number prefixes from SE ICS and SE Tourney titles
            # (e.g. "[3] - Bears vs [2] - Lions" → "Bears vs Lions")
            if event.summary:
                event.summary = re.sub(r"\[\d+\]\s*-\s*", "", event.summary).strip()

            # Apply title token removals to all events (not just SE).
            if title_removals and event.summary:
                summary = event.summary
                for token in title_removals:
                    summary = re.sub(rf"\b{re.escape(token)}\b\s*", "", summary, flags=re.IGNORECASE)
                event.summary = summary.strip()

            # Apply prefix universally (all sources).
            # Smart separator: respect what the user already put at the end of the prefix.
            #   ends with space        → append directly ("Jax - " + "Title")
            #   ends with - : | / etc  → add one space  ("Jax -"  + " Title")
            #   has ASCII letters       → add " - "      ("Jules"  + " - Title")
            #   emoji-only             → add " "         ("⚽"      + " Title")
            if event.prefix and event.summary:
                if event.shared_display_name:
                    event.summary = f"{event.prefix}({event.shared_display_name}) {event.summary}"
                else:
                    event.summary = f"{event.prefix}{event.summary}"
            elif event.shared_display_name and event.summary:
                event.summary = f"({event.shared_display_name}) {event.summary}"

            # Normalize title capitalization (skip for SE Tourney — team names are proper nouns)
            if title_case and event.summary and not event.skip_title_case:
                event.summary = _to_title_case(event.summary)

            # Rebuild enrichment_suffix so recompute_md5 captures all post-parse
            # transformations. Keep seLocation= (set by SE enricher); add location=
            # for other events so abbreviation changes trigger GCal updates.
            existing_parts = [p for p in event.enrichment_suffix.split("|") if p]
            has_se_location = any(p.startswith("seLocation=") for p in existing_parts)
            other_parts = [
                p for p in existing_parts
                if not p.startswith("summary=") and not p.startswith("location=")
            ]
            if event.location and not has_se_location:
                other_parts.append(f"location={event.location}")
            if event.summary:
                other_parts.append(f"summary={event.summary}")
            event.enrichment_suffix = "|".join(other_parts)

            recompute_md5(event)

        # Add / update
        ics_composite_ids: set[str] = {e.composite_id for e in parsed_events}

        for parsed in parsed_events:
            gcal_event = GoogleCalendarClient.build_event(parsed, calendar_tz)

            existing = existing_by_id.get(parsed.composite_id)
            if existing is None:
                # Prefix changed (e.g. added/changed emoji): find same uid stored under
                # a different key and re-index it so it gets updated rather than duplicated.
                uid_prefix = parsed.uid + "_"
                for old_key, evt in list(existing_by_id.items()):
                    if old_key == parsed.uid or old_key.startswith(uid_prefix):
                        existing = evt
                        del existing_by_id[old_key]
                        existing_by_id[parsed.composite_id] = existing
                        break

            if existing is not None:
                if parsed.md5 not in existing_md5s and modify_events:
                    try:
                        await client.update_event(calendar_id, existing["id"], gcal_event)
                        result.modified += 1
                    except GoogleCalendarError as err:
                        result.errors.append(f"Update {parsed.composite_id}: {err}")
                        _LOGGER.warning("Failed to update %s: %s", parsed.composite_id, err)
            elif add_events:
                try:
                    await client.insert_event(calendar_id, gcal_event)
                    result.added += 1
                except GoogleCalendarError as err:
                    result.errors.append(f"Insert {parsed.composite_id}: {err}")
                    _LOGGER.warning("Failed to add %s: %s", parsed.composite_id, err)

        # Remove stale events
        if remove_events:
            now = datetime.now(timezone.utc)
            for composite_id, gcal_event in existing_by_id.items():
                if composite_id in ics_composite_ids:
                    continue
                if gcal_event.get("recurringEventId"):
                    continue
                if not remove_past:
                    event_start = _parse_gcal_start(gcal_event)
                    if event_start and event_start < now:
                        continue
                try:
                    await client.delete_event(calendar_id, gcal_event["id"])
                    result.removed += 1
                except GoogleCalendarError as err:
                    result.errors.append(f"Delete {composite_id}: {err}")
                    _LOGGER.warning("Failed to remove %s: %s", composite_id, err)

    except Exception as err:
        result.errors.append(str(err))
        _LOGGER.error("Sync failed for %s: %s", calendar_name, err)

    return result


def _select_enrichers(
    sources: list[CalendarSource], enrichers: list[BaseEnricher]
) -> list[BaseEnricher]:
    """Return enrichers relevant for this calendar group.

    SE enrichers are matched by account_id. Sources with no account_id set
    (legacy config) accept any SE enricher that also has no account_id.
    """
    from .enrichers.sportsengine import SportsEngineEnricher

    se_account_ids = {
        s.se_account_id for s in sources
        if s.use_se_enricher and s.se_account_id
    }
    uses_se_legacy = any(s.use_se_enricher and not s.se_account_id for s in sources)

    result: list[BaseEnricher] = []
    for enricher in enrichers:
        if isinstance(enricher, SportsEngineEnricher):
            if enricher.account_id in se_account_ids:
                result.append(enricher)
            elif uses_se_legacy and not enricher.account_id:
                result.append(enricher)
        else:
            result.append(enricher)
    return result


_LOWERCASE_WORDS = frozenset({
    "a", "an", "and", "at", "but", "by", "for", "in", "nor",
    "of", "on", "or", "so", "the", "to", "up", "vs", "yet",
})


def _to_title_case(text: str) -> str:
    """Title-case a string, keeping common short words lowercase mid-title.

    Preserves existing mixed-case words (U12, McGregor, etc.).
    Always capitalizes the first word.
    """
    words = text.split(" ")
    result = []
    for i, word in enumerate(words):
        if not word:
            result.append(word)
            continue
        lower = word.lower()
        if i == 0:
            if word.isupper() and len(word) <= 2:
                result.append(word)
            elif word.isupper() or word.islower():
                result.append(word.capitalize())
            else:
                result.append(word[0].upper() + word[1:])
        elif lower in _LOWERCASE_WORDS:
            result.append(lower)
        elif word.isupper() and len(word) <= 2:
            result.append(word)
        elif word.isupper() or word.islower():
            result.append(word.capitalize())
        else:
            result.append(word)
    return " ".join(result)


def _apply_location_abbreviations(location: str, abbreviations: dict[str, str]) -> str:
    """Replace venue names/addresses with short names via case-insensitive substring match."""
    loc_lower = location.lower()
    for key, abbrev in abbreviations.items():
        if key.lower() in loc_lower:
            return abbrev
    return location


def _parse_gcal_start(gcal_event: dict) -> datetime | None:
    start = gcal_event.get("start", {})
    dt_str = start.get("dateTime") or start.get("date")
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

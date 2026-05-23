"""Core sync logic: ICS feeds → Google Calendar for one target calendar group."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from itertools import groupby

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADD_EVENTS,
    CONF_MODIFY_EVENTS,
    CONF_REMOVE_EVENTS,
    CONF_REMOVE_PAST_EVENTS,
    DEFAULT_ADD_EVENTS,
    DEFAULT_MODIFY_EVENTS,
    DEFAULT_REMOVE_EVENTS,
    DEFAULT_REMOVE_PAST_EVENTS,
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

        # Fetch and parse ICS sources
        http_session = async_get_clientsession(hass)
        parsed_events: list[ParsedEvent] = []
        seen_composite_ids: set[str] = set()

        for source in sources:
            for url in source.ics_urls:
                fetched = await async_fetch_and_parse(
                    http_session, url, source.prefix, source.color_id
                )
                for event in fetched:
                    if event.composite_id not in seen_composite_ids:
                        seen_composite_ids.add(event.composite_id)
                        parsed_events.append(event)

        _LOGGER.debug("%s: parsed %d events", calendar_name, len(parsed_events))

        # Apply enrichers and recompute MD5
        for enricher in group_enrichers:
            enriched: list[ParsedEvent] = []
            for event in parsed_events:
                enriched.append(await enricher.async_enrich(event, options))
            parsed_events = enriched

        for event in parsed_events:
            # Apply prefix universally (all sources)
            if event.prefix and event.summary:
                event.summary = f"{event.prefix} - {event.summary}"

            # Rebuild enrichment_suffix with the final summary so recompute_md5
            # captures team prefix and title cleanup even for non-SE events.
            # Keep any seLocation= part, replace any existing summary= part.
            other_parts = [
                p for p in event.enrichment_suffix.split("|")
                if p and not p.startswith("summary=")
            ]
            if event.summary:
                other_parts.append(f"summary={event.summary}")
            event.enrichment_suffix = "|".join(other_parts)

            recompute_md5(event)

        # Add / update
        ics_composite_ids: set[str] = {e.composite_id for e in parsed_events}

        for parsed in parsed_events:
            gcal_event = GoogleCalendarClient.build_event(parsed, calendar_tz)

            if parsed.composite_id in existing_by_id:
                if parsed.md5 not in existing_md5s and modify_events:
                    existing = existing_by_id[parsed.composite_id]
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
    """Return enrichers that are relevant for this calendar group."""
    from .enrichers.sportsengine import SportsEngineEnricher

    result: list[BaseEnricher] = []
    uses_se = any(s.use_se_enricher for s in sources)
    for enricher in enrichers:
        if isinstance(enricher, SportsEngineEnricher) and not uses_se:
            continue
        result.append(enricher)
    return result


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

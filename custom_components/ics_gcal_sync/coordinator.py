"""DataUpdateCoordinator that drives the ICS → Google Calendar sync."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .calendar_sync import async_sync_all
from .const import (
    CONF_GCAL_TARGETS,
    CONF_GCAL_TARGET_NAME,
    CONF_GCAL_TARGET_SOURCE_IDS,
    CONF_GCAL_TARGET_SOURCE_PREFIXES,
    CONF_SE_ACCOUNT_ID,
    CONF_SE_ACCOUNTS,
    CONF_SE_PASSWORD,
    CONF_SE_USERNAME,
    CONF_SOURCES,
    CONF_SOURCE_CALENDAR,
    CONF_SOURCE_COLOR,
    CONF_SOURCE_ENABLED,
    CONF_SOURCE_ID,
    CONF_SOURCE_PREFIX,
    CONF_SOURCE_TYPE,
    CONF_SOURCE_URL,
    CONF_SOURCE_URLS,
    CONF_SOURCE_USE_SE,
    CONF_SYNC_INTERVAL,
    CONF_SE_TOURNEY_DIVISION_ID,
    CONF_SE_TOURNEY_DIVISION_NAME,
    CONF_SE_TOURNEY_GAME_DURATION,
    CONF_SE_TOURNEY_TEAM_ID,
    CONF_SE_TOURNEY_TEAM_NAME,
    CONF_SE_TOURNEY_TOURNAMENT_ID,
    CONF_SE_TOURNEY_TOURNAMENT_NAME,
    DEFAULT_SYNC_INTERVAL,
    DEFAULT_SE_TOURNEY_GAME_DURATION,
    DOMAIN,
    SOURCE_TYPE_ICS,
    SOURCE_TYPE_SE_TOURNEY,
)
from .enrichers import BaseEnricher
from .enrichers.sportsengine import SportsEngineEnricher
from .google_calendar_client import GoogleCalendarClient
from .models import CalendarSource, SyncResult

_LOGGER = logging.getLogger(__name__)


class ICSGCalSyncCoordinator(DataUpdateCoordinator[list[SyncResult]]):
    """Coordinate periodic ICS → Google Calendar sync."""

    last_sync_time: datetime | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        oauth_session: OAuth2Session,
    ) -> None:
        interval_minutes = entry.options.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval_minutes),
        )
        self._entry = entry
        self._client = GoogleCalendarClient(hass, oauth_session)
        self._enrichers: list[BaseEnricher] = self._build_enrichers()

    def _build_enrichers(self) -> list[BaseEnricher]:
        """Instantiate enrichers based on the current options."""
        enrichers: list[BaseEnricher] = []
        options = self._entry.options

        se_accounts = options.get(CONF_SE_ACCOUNTS, [])
        if se_accounts:
            for account in se_accounts:
                username = account.get(CONF_SE_USERNAME, "")
                password = account.get(CONF_SE_PASSWORD, "")
                account_id = account.get(CONF_SE_ACCOUNT_ID, "")
                if username and password:
                    enrichers.append(SportsEngineEnricher(username, password, account_id=account_id))
        else:
            # Backwards compat: single global SE credential
            username = options.get(CONF_SE_USERNAME, "")
            password = options.get(CONF_SE_PASSWORD, "")
            sources = options.get(CONF_SOURCES, [])
            any_se = any(s.get(CONF_SOURCE_USE_SE, False) for s in sources)
            if any_se and username and password:
                enrichers.append(SportsEngineEnricher(username, password))

        return enrichers

    def _build_sources(self) -> list[CalendarSource]:
        """Build CalendarSource objects from config entry options."""
        raw_sources = self._entry.options.get(CONF_SOURCES, [])
        sources = []
        for raw in raw_sources:
            source_type = raw.get(CONF_SOURCE_TYPE, SOURCE_TYPE_ICS)
            target_calendar = raw.get(CONF_SOURCE_CALENDAR, "")
            if not target_calendar:
                continue

            if source_type == SOURCE_TYPE_SE_TOURNEY:
                tournament_id = raw.get(CONF_SE_TOURNEY_TOURNAMENT_ID, "")
                if not tournament_id:
                    continue
                sources.append(
                    CalendarSource(
                        id=raw.get(CONF_SOURCE_ID, ""),
                        ics_urls=[],
                        target_calendar=target_calendar,
                        prefix=raw.get(CONF_SOURCE_PREFIX, ""),
                        color_id=raw.get(CONF_SOURCE_COLOR, ""),
                        enabled=raw.get(CONF_SOURCE_ENABLED, True),
                        source_type=SOURCE_TYPE_SE_TOURNEY,
                        se_tourney_tournament_id=tournament_id,
                        se_tourney_division_id=raw.get(CONF_SE_TOURNEY_DIVISION_ID, ""),
                        se_tourney_team_id=raw.get(CONF_SE_TOURNEY_TEAM_ID, ""),
                        se_tourney_tournament_name=raw.get(CONF_SE_TOURNEY_TOURNAMENT_NAME, ""),
                        se_tourney_division_name=raw.get(CONF_SE_TOURNEY_DIVISION_NAME, ""),
                        se_tourney_team_name=raw.get(CONF_SE_TOURNEY_TEAM_NAME, ""),
                        se_tourney_game_duration=raw.get(CONF_SE_TOURNEY_GAME_DURATION, DEFAULT_SE_TOURNEY_GAME_DURATION),
                    )
                )
            else:
                # ICS source — migration: old configs store a single "ics_url" string
                ics_urls = raw.get(CONF_SOURCE_URLS) or []
                if not ics_urls and raw.get(CONF_SOURCE_URL):
                    ics_urls = [raw[CONF_SOURCE_URL]]
                if not ics_urls:
                    continue
                sources.append(
                    CalendarSource(
                        id=raw.get(CONF_SOURCE_ID, ""),
                        ics_urls=ics_urls,
                        target_calendar=target_calendar,
                        prefix=raw.get(CONF_SOURCE_PREFIX, ""),
                        color_id=raw.get(CONF_SOURCE_COLOR, ""),
                        enabled=raw.get(CONF_SOURCE_ENABLED, True),
                        use_se_enricher=raw.get(CONF_SOURCE_USE_SE, False),
                        se_account_id=raw.get(CONF_SE_ACCOUNT_ID, ""),
                    )
                )
        # For each shareable Google Calendar target, create virtual source copies
        # that point to the shareable calendar instead of their primary calendar.
        # The original source config (URLs, prefix, color, SE settings) is reused
        # as-is — no duplication in the user-facing configuration.
        source_by_id = {s.id: s for s in sources}
        gcal_targets = self._entry.options.get(CONF_GCAL_TARGETS, [])
        for target in gcal_targets:
            target_name = target.get(CONF_GCAL_TARGET_NAME, "")
            source_ids = target.get(CONF_GCAL_TARGET_SOURCE_IDS, [])
            if not target_name or not source_ids:
                continue
            source_prefixes: dict[str, str] = target.get(CONF_GCAL_TARGET_SOURCE_PREFIXES, {})
            for sid in source_ids:
                orig = source_by_id.get(sid)
                if orig is None:
                    continue
                shared_prefix = source_prefixes.get(sid, "")
                sources.append(replace(
                    orig,
                    target_calendar=target_name,
                    shared_display_name=shared_prefix,
                ))

        return sources

    async def _async_update_data(self) -> list[SyncResult]:
        """Run one full sync pass. Called by HA on update_interval."""
        sources = self._build_sources()
        if not sources:
            _LOGGER.debug("No calendar sources configured; skipping sync")
            return []

        # Rebuild enrichers in case options changed since last run
        self._enrichers = self._build_enrichers()

        try:
            results = await async_sync_all(
                self.hass,
                self._client,
                sources,
                self._entry.options,
                self._enrichers,
            )
            self.last_sync_time = datetime.now(timezone.utc)

            # If any changes were made, refresh the HA Google Calendar integration
            # so its entities reflect the new events without waiting for its own poll cycle.
            if any(r.added or r.modified or r.removed for r in results):
                await _async_refresh_google_calendar(self.hass)

            return results
        except Exception as err:
            raise UpdateFailed(f"Sync error: {err}") from err


async def _async_refresh_google_calendar(hass: HomeAssistant) -> None:
    """Refresh HA's Google Calendar integration after our sync pushes changes.

    First tries a light coordinator refresh.  If no coordinator is found
    (attribute names vary across HA versions), falls back to reloading the
    config entry so the entities always pick up fresh data.
    """
    for entry in hass.config_entries.async_entries("google"):
        refreshed = False
        try:
            # HA 2024.6+: runtime_data is a dataclass on the entry itself
            runtime = getattr(entry, "runtime_data", None)
            if runtime is not None:
                # CalendarListCoordinator (lists all calendars)
                for attr in ("calendar_list", "coordinator"):
                    coord = getattr(runtime, attr, None)
                    if hasattr(coord, "async_request_refresh"):
                        await coord.async_request_refresh()
                        refreshed = True
                        _LOGGER.debug("Refreshed Google Calendar coordinator (%s)", attr)
                        break
                # Per-calendar event coordinators
                sync_map = getattr(runtime, "calendar_sync", {})
                if isinstance(sync_map, dict):
                    for coord in sync_map.values():
                        if hasattr(coord, "async_request_refresh"):
                            await coord.async_request_refresh()
                            refreshed = True
            else:
                # Older HA: data stored in hass.data["google"][entry_id]
                entry_data = hass.data.get("google", {}).get(entry.entry_id)
                if entry_data is not None:
                    for attr in ("coordinator", "calendar_list", "calendars_coordinator"):
                        coord = (
                            entry_data.get(attr)
                            if isinstance(entry_data, dict)
                            else getattr(entry_data, attr, None)
                        )
                        if hasattr(coord, "async_request_refresh"):
                            await coord.async_request_refresh()
                            refreshed = True
                            _LOGGER.debug("Refreshed Google Calendar coordinator (%s)", attr)
                            break
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Coordinator refresh failed for Google Calendar entry %s", entry.entry_id)

        if not refreshed:
            # Coordinator not found — schedule a full reload as reliable fallback.
            # Using async_create_task so it runs after our own update completes.
            _LOGGER.debug("No coordinator found; scheduling reload of Google Calendar entry %s", entry.entry_id)
            hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id),
                eager_start=False,
            )

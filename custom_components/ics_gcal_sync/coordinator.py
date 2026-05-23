"""DataUpdateCoordinator that drives the ICS → Google Calendar sync."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .calendar_sync import async_sync_all
from .const import (
    CONF_SE_PASSWORD,
    CONF_SE_USERNAME,
    CONF_SOURCES,
    CONF_SOURCE_CALENDAR,
    CONF_SOURCE_COLOR,
    CONF_SOURCE_ENABLED,
    CONF_SOURCE_ID,
    CONF_SOURCE_TEAM,
    CONF_SOURCE_URL,
    CONF_SOURCE_USE_SE,
    CONF_SYNC_INTERVAL,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
)
from .enrichers import BaseEnricher
from .enrichers.sportsengine import SportsEngineEnricher
from .google_calendar_client import GoogleCalendarClient
from .models import CalendarSource, SyncResult

_LOGGER = logging.getLogger(__name__)


class ICSGCalSyncCoordinator(DataUpdateCoordinator[list[SyncResult]]):
    """Coordinate periodic ICS → Google Calendar sync."""

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

        # SE enricher: include if any source uses it AND credentials are present
        sources = options.get(CONF_SOURCES, [])
        any_se = any(s.get(CONF_SOURCE_USE_SE, False) for s in sources)
        has_se_creds = bool(options.get(CONF_SE_USERNAME) and options.get(CONF_SE_PASSWORD))
        if any_se and has_se_creds:
            enrichers.append(SportsEngineEnricher())

        return enrichers

    def _build_sources(self) -> list[CalendarSource]:
        """Build CalendarSource objects from config entry options."""
        raw_sources = self._entry.options.get(CONF_SOURCES, [])
        sources = []
        for raw in raw_sources:
            sources.append(
                CalendarSource(
                    id=raw.get(CONF_SOURCE_ID, ""),
                    ics_url=raw.get(CONF_SOURCE_URL, ""),
                    target_calendar=raw.get(CONF_SOURCE_CALENDAR, ""),
                    team_name=raw.get(CONF_SOURCE_TEAM, ""),
                    color_id=raw.get(CONF_SOURCE_COLOR, ""),
                    enabled=raw.get(CONF_SOURCE_ENABLED, True),
                    use_se_enricher=raw.get(CONF_SOURCE_USE_SE, False),
                )
            )
        return [s for s in sources if s.ics_url and s.target_calendar]

    async def _async_update_data(self) -> list[SyncResult]:
        """Run one full sync pass. Called by HA on update_interval."""
        sources = self._build_sources()
        if not sources:
            _LOGGER.debug("No calendar sources configured; skipping sync")
            return []

        # Rebuild enrichers in case options changed since last run
        self._enrichers = self._build_enrichers()

        try:
            return await async_sync_all(
                self.hass,
                self._client,
                sources,
                self._entry.options,
                self._enrichers,
            )
        except Exception as err:
            raise UpdateFailed(f"Sync error: {err}") from err

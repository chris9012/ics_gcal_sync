"""DataUpdateCoordinator that drives the ICS → Google Calendar sync."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .calendar_sync import async_sync_all
from .const import (
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
    CONF_SOURCE_URL,
    CONF_SOURCE_URLS,
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
            # One enricher per named SE account
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
            # Migration: old configs store a single "ics_url" string
            ics_urls = raw.get(CONF_SOURCE_URLS) or []
            if not ics_urls and raw.get(CONF_SOURCE_URL):
                ics_urls = [raw[CONF_SOURCE_URL]]
            sources.append(
                CalendarSource(
                    id=raw.get(CONF_SOURCE_ID, ""),
                    ics_urls=ics_urls,
                    target_calendar=raw.get(CONF_SOURCE_CALENDAR, ""),
                    prefix=raw.get(CONF_SOURCE_PREFIX, ""),
                    color_id=raw.get(CONF_SOURCE_COLOR, ""),
                    enabled=raw.get(CONF_SOURCE_ENABLED, True),
                    use_se_enricher=raw.get(CONF_SOURCE_USE_SE, False),
                    se_account_id=raw.get(CONF_SE_ACCOUNT_ID, ""),
                )
            )
        return [s for s in sources if s.ics_urls and s.target_calendar]

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
            return results
        except Exception as err:
            raise UpdateFailed(f"Sync error: {err}") from err

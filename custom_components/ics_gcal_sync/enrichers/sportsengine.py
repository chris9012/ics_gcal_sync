"""SportsEngine enricher: field/location lookup + title cleanup + auto re-login."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.issue_registry import async_delete_issue

from ..const import (
    CONF_LOCATION_ABBREVIATIONS,
    CONF_SE_PASSWORD,
    CONF_SE_TITLE_REMOVALS,
    CONF_SE_USERNAME,
    DOMAIN,
    ISSUE_SE_LOGIN_FAILED,
    SE_API_CALENDAR_URL,
    SE_LOGIN_URL,
)
from ..models import CalendarSource, ParsedEvent
from . import BaseEnricher

_LOGGER = logging.getLogger(__name__)

# SportsEngine event UIDs contain this domain
_SE_UID_MARKER = "@sportsengine.com"


class SportsEngineLoginError(Exception):
    """Raised when SE login fails."""


class SportsEngineEnricher(BaseEnricher):
    """Enriches events from SportsEngine ICS feeds.

    - Fetches field/location details from the SE API (once per sync).
    - Applies location abbreviations.
    - Strips configured tokens from SE event titles.
    - Re-logs in automatically when the session expires.
    """

    def __init__(self) -> None:
        self._cookie: str | None = None
        self._location_map: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # BaseEnricher interface
    # ------------------------------------------------------------------ #

    async def async_prepare(
        self,
        hass: HomeAssistant,
        sources: list[CalendarSource],
        options: dict,
    ) -> None:
        """Fetch SE location data once before processing any events."""
        username = options.get(CONF_SE_USERNAME, "")
        password = options.get(CONF_SE_PASSWORD, "")
        if not username or not password:
            _LOGGER.debug("SE credentials not configured; skipping SE location fetch")
            self._location_map = {}
            return

        session = async_get_clientsession(hass)
        self._location_map = await self._async_fetch_locations(hass, session, username, password)

    async def async_enrich(self, event: ParsedEvent, options: dict) -> ParsedEvent:
        """Apply SE enrichment to a single event."""
        abbreviations: dict[str, str] = options.get(CONF_LOCATION_ABBREVIATIONS, {})
        title_removals: list[str] = options.get(CONF_SE_TITLE_REMOVALS, [])

        is_se_event = _SE_UID_MARKER in event.uid

        # ---- Location -------------------------------------------------- #
        se_location = self._lookup_location(event)
        if se_location:
            se_location = _apply_abbreviations(se_location, abbreviations, prefix_match=True)
            event.location = se_location
        elif event.location:
            event.location = _apply_abbreviations(event.location, abbreviations, prefix_match=False)

        # ---- Title cleanup (SE events only) ---------------------------- #
        if is_se_event and event.summary:
            summary = re.sub(r"\s*\([^)]*\)", "", event.summary)
            for token in title_removals:
                summary = re.sub(rf"\b{re.escape(token)}\b\s*", "", summary)
            event.summary = summary.strip()

        # ---- Update enrichment suffix for MD5 -------------------------- #
        suffix_parts = []
        if se_location:
            suffix_parts.append(f"seLocation={se_location}")
        if event.summary:
            suffix_parts.append(f"summary={event.summary}")
        event.enrichment_suffix = "|".join(suffix_parts)

        return event

    # ------------------------------------------------------------------ #
    # SE session management
    # ------------------------------------------------------------------ #

    async def _async_fetch_locations(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> dict[str, str]:
        """Fetch upcoming SE events and build a UTC-start-time → location map."""
        for attempt in range(2):
            if not self._cookie:
                try:
                    self._cookie = await self._async_login(session, username, password)
                    _LOGGER.debug("SE login successful")
                    async_delete_issue(hass, DOMAIN, ISSUE_SE_LOGIN_FAILED)
                except SportsEngineLoginError as err:
                    _LOGGER.error("SE login failed: %s", err)
                    async_create_issue(
                        hass,
                        DOMAIN,
                        ISSUE_SE_LOGIN_FAILED,
                        is_fixable=True,
                        severity=IssueSeverity.ERROR,
                        translation_key=ISSUE_SE_LOGIN_FAILED,
                    )
                    return {}

            locations: dict[str, str] = {}
            page = 1
            while True:
                try:
                    async with session.get(
                        SE_API_CALENDAR_URL,
                        params={"include_favorites": "1", "page": page, "per_page": 30, "past": "false"},
                        headers={"Cookie": f"sportngin_session={self._cookie}"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status in (401, 403):
                            # Cookie expired; force re-login on next attempt
                            _LOGGER.debug("SE cookie expired; will re-login")
                            self._cookie = None
                            break
                        if resp.status != 200:
                            _LOGGER.warning("SE API returned %d", resp.status)
                            return locations
                        data = await resp.json()
                except Exception as err:
                    _LOGGER.error("SE API request failed: %s", err)
                    return locations

                for evt in data.get("result", []):
                    if evt.get("location_name") or evt.get("location_description"):
                        key = evt["start_date_time"].replace(".000Z", "Z")
                        parts = [evt.get("location_name") or "", evt.get("location_description") or ""]
                        locations[key] = " - ".join(p for p in parts if p)

                if data.get("metadata", {}).get("pagination", {}).get("last_page", True):
                    break
                page += 1

            if self._cookie:
                _LOGGER.debug("Fetched %d SE location entries", len(locations))
                return locations
            # Cookie was invalid; loop to retry with fresh login

        return {}

    async def _async_login(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> str:
        """Log into SportsEngine and return the session cookie value."""
        # 1. GET login page to extract CSRF token
        try:
            async with session.get(SE_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
        except Exception as err:
            raise SportsEngineLoginError(f"Could not reach SE login page: {err}") from err

        csrf_match = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', html)
        if not csrf_match:
            raise SportsEngineLoginError("CSRF token not found on SE login page")
        csrf = csrf_match.group(1)

        # 2. POST credentials
        try:
            async with session.post(
                SE_LOGIN_URL,
                data={
                    "user[email]": username,
                    "user[password]": password,
                    "authenticity_token": csrf,
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                cookie = resp.cookies.get("sportngin_session")
                if cookie:
                    return cookie.value
                if resp.status not in (200, 302):
                    raise SportsEngineLoginError(f"Login returned HTTP {resp.status}")
        except SportsEngineLoginError:
            raise
        except Exception as err:
            raise SportsEngineLoginError(f"Login POST failed: {err}") from err

        raise SportsEngineLoginError("sportngin_session cookie not found after login")

    # ------------------------------------------------------------------ #
    # Location lookup
    # ------------------------------------------------------------------ #

    def _lookup_location(self, event: ParsedEvent) -> str | None:
        """Look up the SE field/location for this event by UTC start time."""
        if not self._location_map:
            return None
        dt = event.start
        if not isinstance(dt, datetime):
            return None
        if dt.tzinfo is None:
            utc_dt = dt.replace(tzinfo=timezone.utc)
        else:
            utc_dt = dt.astimezone(timezone.utc)
        key = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._location_map.get(key)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _apply_abbreviations(
    location: str,
    abbreviations: dict[str, str],
    prefix_match: bool,
) -> str:
    """Replace venue names/addresses with friendly short names.

    prefix_match=True  → replace when the location *starts with* the key
                         (SE API location name); keeps any trailing field suffix.
    prefix_match=False → replace when the location *contains* the key
                         (raw ICS address); full replacement.
    """
    loc_lower = location.lower()
    for key, abbrev in abbreviations.items():
        key_lower = key.lower()
        if prefix_match:
            if loc_lower.startswith(key_lower):
                remainder = location[len(key):].strip()
                if remainder.startswith("- "):
                    field_part = remainder[2:].strip()
                    # Drop field_part if it is itself a known venue (redundant)
                    if not any(field_part.lower().startswith(k.lower()) for k in abbreviations):
                        return f"{abbrev} {remainder}"
                return abbrev
        else:
            if key_lower in loc_lower:
                return abbrev
    return location

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
    CONF_SE_TITLE_REMOVALS,
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

    def __init__(self, username: str = "", password: str = "", account_id: str = "") -> None:
        self._username = username
        self._password = password
        self.account_id = account_id
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
        if not self._username or not self._password:
            _LOGGER.debug("SE credentials not configured; skipping SE location fetch")
            self._location_map = {}
            return

        session = async_get_clientsession(hass)
        self._location_map = await self._async_fetch_locations(hass, session, self._username, self._password)

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
                summary = re.sub(rf"\b{re.escape(token)}\b\s*", "", summary, flags=re.IGNORECASE)
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
                        headers={
                            "Cookie": f"sportngin_session={self._cookie}",
                            "Accept": "application/json",
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36"
                            ),
                        },
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status in (400, 401, 403):
                            # 400/401/403 all indicate an invalid or expired cookie
                            body = (await resp.text())[:200]
                            _LOGGER.debug("SE cookie invalid/expired (HTTP %d): %s", resp.status, body)
                            self._cookie = None
                            break
                        if resp.status != 200:
                            body = (await resp.text())[:200]
                            _LOGGER.warning("SE API returned %d: %s", resp.status, body)
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
        """Log into SportsEngine and return the session cookie value.

        SE uses a two-step login flow (step 1: email, step 2: password).
        Uses a fresh ClientSession with its own cookie jar so Rails session
        cookies are reliably carried between the three requests.
        """
        browser_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        base_headers = {
            "User-Agent": browser_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with aiohttp.ClientSession(headers=base_headers) as s:
            # ---- Step 1: GET login page → extract CSRF token ----------- #
            try:
                async with s.get(SE_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    html1 = await resp.text()
            except Exception as err:
                raise SportsEngineLoginError(f"Could not reach SE login page: {err}") from err

            csrf1 = _extract_csrf(html1)
            if not csrf1:
                raise SportsEngineLoginError("CSRF token not found on SE login page (step 1)")
            _LOGGER.debug("SE step 1 CSRF found (length %d)", len(csrf1))

            # ---- Step 2: POST email → get password page + new CSRF ----- #
            try:
                async with s.post(
                    SE_LOGIN_URL,
                    data={"authenticity_token": csrf1, "user[login]": username, "commit": "Continue"},
                    headers={"Referer": SE_LOGIN_URL},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:500]
                        _LOGGER.warning("SE step 1 POST returned HTTP %d: %s", resp.status, body)
                        raise SportsEngineLoginError(f"Step 1 POST returned HTTP {resp.status}")
                    html2 = await resp.text()
            except SportsEngineLoginError:
                raise
            except Exception as err:
                raise SportsEngineLoginError(f"Step 1 POST failed: {err}") from err

            csrf2 = _extract_csrf(html2)
            if not csrf2:
                raise SportsEngineLoginError("CSRF token not found on SE login page (step 2)")
            _LOGGER.debug("SE step 2 CSRF found (length %d)", len(csrf2))

            # ---- Step 3: POST password → follow redirects, grab cookie - #
            try:
                async with s.post(
                    SE_LOGIN_URL,
                    data={
                        "authenticity_token": csrf2,
                        "user[login]": username,
                        "user[password]": password,
                        "commit": "Sign in",
                    },
                    headers={"Referer": SE_LOGIN_URL},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    _LOGGER.debug(
                        "SE step 3 final status=%d url=%s", resp.status, resp.url
                    )
                    if resp.status not in (200, 302):
                        body = (await resp.text())[:500]
                        _LOGGER.warning("SE step 3 returned HTTP %d: %s", resp.status, body)
                        raise SportsEngineLoginError(f"Step 3 returned HTTP {resp.status}")
            except SportsEngineLoginError:
                raise
            except Exception as err:
                raise SportsEngineLoginError(f"Step 3 POST failed: {err}") from err

            # Search the entire cookie jar — the cookie domain may differ from
            # the login URL (e.g. .sportngin.com vs user.sportngin.com)
            for morsel in s.cookie_jar:
                if morsel.key == "sportngin_session":
                    _LOGGER.debug("SE sportngin_session cookie found in jar")
                    return morsel.value

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

def _extract_csrf(html: str) -> str | None:
    """Extract the CSRF token from a page's meta tag (either attribute order)."""
    m = re.search(
        r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"'
        r'|<meta[^>]+content="([^"]+)"[^>]+name="csrf-token"',
        html,
    )
    if not m:
        return None
    return m.group(1) or m.group(2)

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

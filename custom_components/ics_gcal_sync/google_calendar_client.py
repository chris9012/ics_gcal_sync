"""Async Google Calendar REST API client using HA's OAuth2 session."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import GCAL_API_BASE, GCAL_MARKER_KEY, GCAL_MARKER_VALUE
from .models import ParsedEvent

_LOGGER = logging.getLogger(__name__)


class GoogleCalendarError(Exception):
    """Raised when a Google Calendar API call fails."""


class GoogleCalendarClient:
    """Thin async wrapper around the Google Calendar v3 REST API."""

    def __init__(self, hass: HomeAssistant, oauth_session: OAuth2Session) -> None:
        self._hass = hass
        self._oauth = oauth_session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated request, refreshing the token if needed."""
        await self._oauth.async_ensure_token_valid()
        token = self._oauth.token["access_token"]
        session = async_get_clientsession(self._hass)
        url = f"{GCAL_API_BASE}{path}"
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}

        async with session.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status == 204:
                return None
            data = await resp.json()
            if resp.status >= 400:
                raise GoogleCalendarError(
                    f"Google Calendar API {method} {path} returned {resp.status}: "
                    f"{data.get('error', {}).get('message', data)}"
                )
            return data

    # ------------------------------------------------------------------ #
    # Calendar management
    # ------------------------------------------------------------------ #

    async def get_timezone(self) -> str:
        """Return the authenticated user's primary calendar timezone."""
        data = await self._request("GET", "/users/me/settings/timezone")
        return data["value"]

    async def list_writable_calendars(self) -> list[str]:
        """Return sorted names of calendars the user owns or can write to."""
        data = await self._request(
            "GET",
            "/users/me/calendarList",
            params={"showHidden": "true", "maxResults": 250},
        )
        names = []
        for cal in data.get("items", []):
            if cal.get("accessRole") in ("owner", "writer"):
                name = cal.get("summaryOverride") or cal.get("summary", "")
                if name:
                    names.append(name)
        return sorted(names)

    async def get_or_create_calendar(self, name: str, timezone: str) -> dict:
        """Return the calendar with the given name, creating it if absent."""
        data = await self._request(
            "GET",
            "/users/me/calendarList",
            params={"showHidden": "true", "maxResults": 250},
        )
        for cal in data.get("items", []):
            display_name = cal.get("summaryOverride") or cal.get("summary", "")
            if display_name == name and cal.get("accessRole") in ("owner", "writer"):
                return cal

        _LOGGER.info("Creating Google Calendar: %s", name)
        return await self._request(
            "POST",
            "/calendars",
            json={"summary": name, "timeZone": timezone},
        )

    # ------------------------------------------------------------------ #
    # Event listing
    # ------------------------------------------------------------------ #

    async def list_managed_events(self, calendar_id: str) -> list[dict]:
        """Return all events created by this integration (paginated)."""
        events: list[dict] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "showDeleted": "false",
                "privateExtendedProperty": f"{GCAL_MARKER_KEY}={GCAL_MARKER_VALUE}",
                "maxResults": 2500,
            }
            if page_token:
                params["pageToken"] = page_token

            data = await self._request("GET", f"/calendars/{calendar_id}/events", params=params)
            events.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return events

    # ------------------------------------------------------------------ #
    # Event mutations
    # ------------------------------------------------------------------ #

    async def insert_event(self, calendar_id: str, event: dict) -> dict:
        return await self._request("POST", f"/calendars/{calendar_id}/events", json=event)

    async def update_event(self, calendar_id: str, event_id: str, event: dict) -> dict:
        return await self._request(
            "PUT", f"/calendars/{calendar_id}/events/{event_id}", json=event
        )

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await self._request("DELETE", f"/calendars/{calendar_id}/events/{event_id}")

    # ------------------------------------------------------------------ #
    # Event building
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_event(parsed: ParsedEvent, calendar_tz: str) -> dict:
        """Convert a ParsedEvent into a Google Calendar event dict."""
        gcal: dict[str, Any] = {}

        if parsed.is_all_day:
            start = parsed.start if isinstance(parsed.start, date) else parsed.start.date()
            end = parsed.end if isinstance(parsed.end, date) else parsed.end.date()
            gcal["start"] = {"date": start.isoformat()}
            gcal["end"] = {"date": end.isoformat()}
        else:
            start_dt = _ensure_datetime(parsed.start)
            end_dt = _ensure_datetime(parsed.end)
            start_tz = _tz_name(start_dt) or calendar_tz
            end_tz = _tz_name(end_dt) or calendar_tz
            gcal["start"] = {"dateTime": start_dt.isoformat(), "timeZone": start_tz}
            gcal["end"] = {"dateTime": end_dt.isoformat(), "timeZone": end_tz}

        if parsed.summary:
            gcal["summary"] = parsed.summary
        if parsed.description:
            gcal["description"] = parsed.description
        if parsed.location:
            gcal["location"] = parsed.location
        if parsed.status in ("confirmed", "tentative", "cancelled"):
            gcal["status"] = parsed.status
        if parsed.url:
            gcal["source"] = {"url": parsed.url, "title": "link"}
        if parsed.recurrence:
            gcal["recurrence"] = parsed.recurrence
        if parsed.color_id:
            gcal["colorId"] = parsed.color_id

        gcal["reminders"] = {"useDefault": True, "overrides": []}

        gcal["extendedProperties"] = {
            "private": {
                GCAL_MARKER_KEY: GCAL_MARKER_VALUE,
                "id": parsed.composite_id,
                "MD5": parsed.md5,
            }
        }

        return gcal


def _ensure_datetime(dt: datetime | date) -> datetime:
    if isinstance(dt, datetime):
        return dt
    return datetime(dt.year, dt.month, dt.day)


def _tz_name(dt: datetime) -> str | None:
    """Return the IANA timezone name from a timezone-aware datetime, or None."""
    if dt.tzinfo is None:
        return None
    name = str(dt.tzinfo)
    return name if name not in ("UTC", "") else "UTC"

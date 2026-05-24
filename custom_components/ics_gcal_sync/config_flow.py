"""Config flow and options flow for ICS to Google Calendar Sync."""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .const import (
    CONF_ADD_EVENTS,
    CONF_LOCATION_ABBREVIATIONS,
    CONF_MODIFY_EVENTS,
    CONF_REMOVE_EVENTS,
    CONF_REMOVE_PAST_EVENTS,
    CONF_TITLE_CASE,
    CONF_SE_PASSWORD,
    CONF_SE_TITLE_REMOVALS,
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
    DEFAULT_ADD_EVENTS,
    DEFAULT_MODIFY_EVENTS,
    DEFAULT_REMOVE_EVENTS,
    DEFAULT_REMOVE_PAST_EVENTS,
    DEFAULT_SYNC_INTERVAL,
    DEFAULT_TITLE_CASE,
    DOMAIN,
    OAUTH2_SCOPES,
)

_LOGGER = logging.getLogger(__name__)

_ABBREV_HELP = (
    "One entry per line: Venue Name or Street Address = Short Name\n"
    "Matching is case-insensitive. Example:\n"
    "200 Rex Place = MB ROC\n"
    "Treasure Island - Rosselli Park = TI Rosselli"
)


class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the OAuth2 config flow."""

    DOMAIN = DOMAIN

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(__name__)

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {
            "scope": " ".join(OAUTH2_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)


# ======================================================================
# Options flow
# ======================================================================

class OptionsFlowHandler(OptionsFlow):
    """Multi-step options flow for managing calendar sources and sync settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._options: dict[str, Any] = dict(config_entry.options)
        self._sources: list[dict] = list(self._options.get(CONF_SOURCES, []))
        self._editing_idx: int | None = None

    # ------------------------------------------------------------------ #
    # Main menu
    # ------------------------------------------------------------------ #

    async def async_step_init(self, user_input: dict | None = None):
        menu_options = ["sync_settings", "add_source"]
        if self._sources:
            menu_options.append("manage_sources")
        menu_options += ["se_settings", "done"]
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    # ------------------------------------------------------------------ #
    # Sync settings
    # ------------------------------------------------------------------ #

    async def async_step_sync_settings(self, user_input: dict | None = None):
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SYNC_INTERVAL,
                    default=self._options.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=1440, mode="box", unit_of_measurement="min")
                ),
                vol.Optional(
                    CONF_ADD_EVENTS,
                    default=self._options.get(CONF_ADD_EVENTS, DEFAULT_ADD_EVENTS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_MODIFY_EVENTS,
                    default=self._options.get(CONF_MODIFY_EVENTS, DEFAULT_MODIFY_EVENTS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_REMOVE_EVENTS,
                    default=self._options.get(CONF_REMOVE_EVENTS, DEFAULT_REMOVE_EVENTS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_REMOVE_PAST_EVENTS,
                    default=self._options.get(CONF_REMOVE_PAST_EVENTS, DEFAULT_REMOVE_PAST_EVENTS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_TITLE_CASE,
                    default=self._options.get(CONF_TITLE_CASE, DEFAULT_TITLE_CASE),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="sync_settings", data_schema=schema)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _calendar_selector(self, default: str = "") -> selector.SelectSelector:
        """Return a SelectSelector pre-populated with the user's Google Calendars.

        custom_value=True lets the user type a new calendar name that doesn't
        exist yet — it will be created automatically on first sync.
        Falls back to an empty list if the API call fails.
        """
        cal_names: list[str] = []
        try:
            from .coordinator import ICSGCalSyncCoordinator
            coordinator: ICSGCalSyncCoordinator = self.hass.data[DOMAIN].get(
                self._config_entry.entry_id
            )
            if coordinator:
                cal_names = await coordinator._client.list_writable_calendars()
        except Exception:
            pass

        options = [selector.SelectOptionDict(value=n, label=n) for n in cal_names]
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

    # ------------------------------------------------------------------ #
    # Add source
    # ------------------------------------------------------------------ #

    async def async_step_add_source(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            ics_urls = _parse_urls(user_input.get("ics_urls_raw", ""))
            if not ics_urls:
                errors["ics_urls_raw"] = "invalid_url"
            if not user_input.get(CONF_SOURCE_CALENDAR, "").strip():
                errors[CONF_SOURCE_CALENDAR] = "required"
            if not errors:
                self._sources.append(
                    {
                        CONF_SOURCE_ID: str(uuid.uuid4()),
                        CONF_SOURCE_URLS: ics_urls,
                        CONF_SOURCE_CALENDAR: user_input[CONF_SOURCE_CALENDAR].strip(),
                        CONF_SOURCE_PREFIX: user_input.get(CONF_SOURCE_PREFIX, "").strip(),
                        CONF_SOURCE_COLOR: user_input.get(CONF_SOURCE_COLOR, "").strip(),
                        CONF_SOURCE_USE_SE: user_input.get(CONF_SOURCE_USE_SE, False),
                        CONF_SOURCE_ENABLED: True,
                    }
                )
                self._options[CONF_SOURCES] = self._sources
                return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required("ics_urls_raw"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                vol.Required(CONF_SOURCE_CALENDAR): await self._calendar_selector(),
                vol.Optional(CONF_SOURCE_PREFIX, default=""): selector.TextSelector(),
                vol.Optional(CONF_SOURCE_COLOR, default=""): selector.TextSelector(),
                vol.Optional(CONF_SOURCE_USE_SE, default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="add_source", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------ #
    # Manage existing sources (select → edit or remove)
    # ------------------------------------------------------------------ #

    async def async_step_manage_sources(self, user_input: dict | None = None):
        if not self._sources:
            return await self.async_step_init()

        if user_input is not None:
            source_id = user_input.get("source_id")
            self._editing_idx = next(
                (i for i, s in enumerate(self._sources) if s[CONF_SOURCE_ID] == source_id),
                None,
            )
            if user_input.get("action") == "remove":
                return await self.async_step_confirm_remove()
            return await self.async_step_edit_source()

        source_options = {
            s[CONF_SOURCE_ID]: f"{s[CONF_SOURCE_CALENDAR]} — {s.get(CONF_SOURCE_PREFIX) or _first_url(s)}"
            for s in self._sources
        }
        schema = vol.Schema(
            {
                vol.Required("source_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=k, label=v)
                            for k, v in source_options.items()
                        ]
                    )
                ),
                vol.Required("action", default="edit"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value="edit", label="Edit"),
                            selector.SelectOptionDict(value="remove", label="Remove"),
                        ]
                    )
                ),
            }
        )
        return self.async_show_form(step_id="manage_sources", data_schema=schema)

    async def async_step_edit_source(self, user_input: dict | None = None):
        if self._editing_idx is None:
            return await self.async_step_init()

        source = self._sources[self._editing_idx]
        errors: dict[str, str] = {}

        if user_input is not None:
            ics_urls = _parse_urls(user_input.get("ics_urls_raw", ""))
            if not ics_urls:
                errors["ics_urls_raw"] = "invalid_url"
            if not user_input.get(CONF_SOURCE_CALENDAR, "").strip():
                errors[CONF_SOURCE_CALENDAR] = "required"
            if not errors:
                self._sources[self._editing_idx] = {
                    **source,
                    CONF_SOURCE_URLS: ics_urls,
                    CONF_SOURCE_CALENDAR: user_input[CONF_SOURCE_CALENDAR].strip(),
                    CONF_SOURCE_PREFIX: user_input.get(CONF_SOURCE_PREFIX, "").strip(),
                    CONF_SOURCE_COLOR: user_input.get(CONF_SOURCE_COLOR, "").strip(),
                    CONF_SOURCE_USE_SE: user_input.get(CONF_SOURCE_USE_SE, False),
                    CONF_SOURCE_ENABLED: user_input.get(CONF_SOURCE_ENABLED, True),
                }
                self._options[CONF_SOURCES] = self._sources
                self._editing_idx = None
                return await self.async_step_init()

        existing_urls = source.get(CONF_SOURCE_URLS) or (
            [source[CONF_SOURCE_URL]] if source.get(CONF_SOURCE_URL) else []
        )
        urls_text = "\n".join(existing_urls)

        schema = vol.Schema(
            {
                vol.Required("ics_urls_raw", description={"suggested_value": urls_text}): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                vol.Required(CONF_SOURCE_CALENDAR, description={"suggested_value": source[CONF_SOURCE_CALENDAR]}): await self._calendar_selector(source[CONF_SOURCE_CALENDAR]),
                vol.Optional(CONF_SOURCE_PREFIX, description={"suggested_value": source.get(CONF_SOURCE_PREFIX, "")}): selector.TextSelector(),
                vol.Optional(CONF_SOURCE_COLOR, description={"suggested_value": source.get(CONF_SOURCE_COLOR, "")}): selector.TextSelector(),
                vol.Optional(CONF_SOURCE_USE_SE, default=source.get(CONF_SOURCE_USE_SE, False)): selector.BooleanSelector(),
                vol.Optional(CONF_SOURCE_ENABLED, default=source.get(CONF_SOURCE_ENABLED, True)): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="edit_source", data_schema=schema, errors=errors
        )

    async def async_step_confirm_remove(self, user_input: dict | None = None):
        if user_input is not None:
            if user_input.get("confirm") and self._editing_idx is not None:
                self._sources.pop(self._editing_idx)
                self._options[CONF_SOURCES] = self._sources
            self._editing_idx = None
            return await self.async_step_init()

        if self._editing_idx is None:
            return await self.async_step_init()

        source = self._sources[self._editing_idx]
        label = f"{source[CONF_SOURCE_CALENDAR]} — {source.get(CONF_SOURCE_PREFIX) or _first_url(source)}"
        schema = vol.Schema(
            {
                vol.Required("confirm", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="confirm_remove",
            data_schema=schema,
            description_placeholders={"source_label": label},
        )

    # ------------------------------------------------------------------ #
    # SportsEngine settings
    # ------------------------------------------------------------------ #

    async def async_step_se_settings(self, user_input: dict | None = None):
        if user_input is not None:
            # Convert textarea to dict/list
            raw_abbrevs = user_input.pop("location_abbreviations_raw", "")
            self._options[CONF_LOCATION_ABBREVIATIONS] = _parse_abbreviations(raw_abbrevs)

            raw_removals = user_input.pop("se_title_removals_raw", "")
            self._options[CONF_SE_TITLE_REMOVALS] = [
                t.strip() for t in raw_removals.split(",") if t.strip()
            ]

            self._options.update(user_input)
            return await self.async_step_init()

        # Render existing abbreviations as textarea text
        existing_abbrevs = self._options.get(CONF_LOCATION_ABBREVIATIONS, {})
        abbrevs_text = "\n".join(f"{k} = {v}" for k, v in existing_abbrevs.items())
        existing_removals = self._options.get(CONF_SE_TITLE_REMOVALS, [])
        removals_text = ", ".join(existing_removals)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SE_USERNAME,
                    description={"suggested_value": self._options.get(CONF_SE_USERNAME, "")},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type="email", autocomplete="username")
                ),
                vol.Optional(
                    CONF_SE_PASSWORD,
                    description={"suggested_value": self._options.get(CONF_SE_PASSWORD, "")},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type="password", autocomplete="current-password")
                ),
                vol.Optional(
                    "location_abbreviations_raw",
                    description={"suggested_value": abbrevs_text},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                vol.Optional(
                    "se_title_removals_raw",
                    description={"suggested_value": removals_text},
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="se_settings",
            data_schema=schema,
            description_placeholders={"abbrev_help": _ABBREV_HELP},
        )

    # ------------------------------------------------------------------ #
    # Done — save everything
    # ------------------------------------------------------------------ #

    async def async_step_done(self, user_input: dict | None = None):
        return self.async_create_entry(title="", data=self._options)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _parse_urls(raw: str) -> list[str]:
    """Parse a textarea of URLs (one per line) into a validated list."""
    urls = []
    for line in raw.splitlines():
        line = line.strip()
        if line and (line.startswith("http") or line.startswith("webcal")):
            urls.append(line.replace("webcal://", "https://"))
    return urls


def _first_url(source: dict) -> str:
    """Return a short display string for the first URL in a source (new or legacy format)."""
    urls = source.get(CONF_SOURCE_URLS) or []
    if not urls and source.get(CONF_SOURCE_URL):
        urls = [source[CONF_SOURCE_URL]]
    return urls[0][:60] if urls else "?"


def _parse_abbreviations(raw: str) -> dict[str, str]:
    """Parse 'Key = Value' lines into a dict, ignoring blanks and comments."""
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and value:
                result[key] = value
    return result

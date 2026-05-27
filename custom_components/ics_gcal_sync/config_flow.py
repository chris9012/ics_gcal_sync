"""Config flow and options flow for ICS to Google Calendar Sync."""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .emoji_data import EMOJI_OPTIONS
from .const import (
    CONF_ADD_EVENTS,
    CONF_GCAL_TARGETS,
    CONF_GCAL_TARGET_ID,
    CONF_GCAL_TARGET_NAME,
    CONF_GCAL_TARGET_SOURCE_IDS,
    CONF_GCAL_TARGET_SOURCE_PREFIXES,
    CONF_LOCATION_ABBREVIATIONS,
    CONF_MODIFY_EVENTS,
    CONF_REMOVE_EVENTS,
    CONF_REMOVE_PAST_EVENTS,
    CONF_TITLE_CASE,
    CONF_SE_ACCOUNT_ID,
    CONF_SE_ACCOUNT_NAME,
    CONF_SE_ACCOUNTS,
    CONF_SE_PASSWORD,
    CONF_SE_TITLE_REMOVALS,
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
    DEFAULT_ADD_EVENTS,
    DEFAULT_MODIFY_EVENTS,
    DEFAULT_REMOVE_EVENTS,
    DEFAULT_REMOVE_PAST_EVENTS,
    DEFAULT_SYNC_INTERVAL,
    DEFAULT_TITLE_CASE,
    DEFAULT_SE_TOURNEY_GAME_DURATION,
    DOMAIN,
    OAUTH2_SCOPES,
    SOURCE_TYPE_SE_TOURNEY,
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
        self._se_accounts: list[dict] = list(self._options.get(CONF_SE_ACCOUNTS, []))
        self._editing_idx: int | None = None
        self._editing_se_idx: int | None = None
        self._selected_calendar: str = ""
        self._gcal_targets: list[dict] = list(self._options.get(CONF_GCAL_TARGETS, []))
        self._editing_gcal_target_idx: int | None = None
        self._new_source_id: str = ""
        self._editing_gcal_target_source_id: str = ""
        # SE Tourney wizard state
        self._se_tourney_search_results: list[dict] = []
        self._se_tourney_tournament_id: str = ""
        self._se_tourney_tournament_name: str = ""
        self._se_tourney_divisions: list[dict] = []
        self._se_tourney_division_id: str = ""
        self._se_tourney_division_name: str = ""
        self._se_tourney_teams: list[dict] = []
        self._se_tourney_pending_team: dict = {}

    # ------------------------------------------------------------------ #
    # Main menu
    # ------------------------------------------------------------------ #

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["sync_settings", "calendars", "sportsengine", "shareable_cals", "done"],
        )

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

    async def _calendar_selector(self) -> selector.SelectSelector:
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
    # Calendar selection (entry point for calendar-first hierarchy)
    # ------------------------------------------------------------------ #

    async def async_step_calendars(self, user_input: dict | None = None):
        existing_cals = sorted({
            s[CONF_SOURCE_CALENDAR]
            for s in self._sources
            if s.get(CONF_SOURCE_CALENDAR)
        })

        if not existing_cals:
            return await self.async_step_new_calendar()

        if user_input is not None:
            choice = user_input.get("calendar_choice", "")
            if choice == "__new__":
                return await self.async_step_new_calendar()
            if choice:
                self._selected_calendar = choice
                return await self.async_step_manage_calendar()

        options = [selector.SelectOptionDict(value=c, label=c) for c in existing_cals]
        options.append(selector.SelectOptionDict(value="__new__", label="Set up a different calendar…"))
        schema = vol.Schema({
            vol.Required("calendar_choice"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(step_id="calendars", data_schema=schema)

    async def async_step_new_calendar(self, user_input: dict | None = None):
        if user_input is not None:
            cal = user_input.get(CONF_SOURCE_CALENDAR, "").strip()
            if cal:
                self._selected_calendar = cal
                return await self.async_step_manage_calendar()

        schema = vol.Schema({
            vol.Required(CONF_SOURCE_CALENDAR): await self._calendar_selector(),
        })
        return self.async_show_form(step_id="new_calendar", data_schema=schema)

    async def async_step_manage_calendar(self, user_input: dict | None = None):
        cal = self._selected_calendar
        cal_sources = [s for s in self._sources if s.get(CONF_SOURCE_CALENDAR) == cal]

        menu_options = ["add_ics_source", "add_se_tourney"]
        if cal_sources:
            menu_options += ["manage_calendar_sources", "remove_calendar"]
        menu_options += ["back_to_calendars", "done"]

        return self.async_show_menu(
            step_id="manage_calendar",
            menu_options=menu_options,
        )

    async def async_step_back_to_calendars(self, user_input: dict | None = None):
        return await self.async_step_calendars()

    # ------------------------------------------------------------------ #
    # Add ICS source (calendar already set via self._selected_calendar)
    # ------------------------------------------------------------------ #

    async def async_step_add_ics_source(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            ics_urls = _parse_urls(user_input.get("ics_urls_raw", ""))
            if not ics_urls:
                errors["ics_urls_raw"] = "invalid_url"
            if not errors:
                new_id = str(uuid.uuid4())
                self._sources.append(
                    {
                        CONF_SOURCE_ID: new_id,
                        CONF_SOURCE_URLS: ics_urls,
                        CONF_SOURCE_CALENDAR: self._selected_calendar,
                        CONF_SOURCE_PREFIX: _combine_prefix(
                            user_input.get("prefix_emoji", ""),
                            user_input.get("prefix_text", ""),
                        ),
                        CONF_SOURCE_COLOR: user_input.get(CONF_SOURCE_COLOR, "").strip(),
                        CONF_SOURCE_USE_SE: user_input.get(CONF_SOURCE_USE_SE, False),
                        CONF_SE_ACCOUNT_ID: user_input.get(CONF_SE_ACCOUNT_ID, ""),
                        CONF_SOURCE_ENABLED: True,
                    }
                )
                self._options[CONF_SOURCES] = self._sources
                self._new_source_id = new_id
                return await self.async_step_source_shared()

        schema_dict: dict = {
            vol.Required("ics_urls_raw"): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional("prefix_emoji", default=""): _emoji_selector(),
            vol.Optional("prefix_text", default=""): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Optional(CONF_SOURCE_COLOR, default=""): selector.TextSelector(),
            vol.Optional(CONF_SOURCE_USE_SE, default=False): selector.BooleanSelector(),
        }
        if self._se_accounts:
            schema_dict[vol.Optional(CONF_SE_ACCOUNT_ID, default="")] = _se_account_selector(self._se_accounts)
        return self.async_show_form(
            step_id="add_ics_source",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"calendar": self._selected_calendar},
        )

    # ------------------------------------------------------------------ #
    # Manage sources scoped to selected calendar
    # ------------------------------------------------------------------ #

    async def async_step_manage_calendar_sources(self, user_input: dict | None = None):
        cal_sources = [s for s in self._sources if s.get(CONF_SOURCE_CALENDAR) == self._selected_calendar]
        if not cal_sources:
            return await self.async_step_manage_calendar()

        errors: dict[str, str] = {}

        if user_input is not None:
            action = user_input.get("action", "back")
            if action == "back":
                return await self.async_step_manage_calendar()
            source_id = user_input.get("source_id")
            if not source_id:
                errors["source_id"] = "required"
            else:
                self._editing_idx = next(
                    (i for i, s in enumerate(self._sources) if s[CONF_SOURCE_ID] == source_id),
                    None,
                )
                if action == "remove":
                    return await self.async_step_confirm_remove()
                if (
                    self._editing_idx is not None
                    and self._sources[self._editing_idx].get(CONF_SOURCE_TYPE) == SOURCE_TYPE_SE_TOURNEY
                ):
                    return await self.async_step_edit_se_tourney()
                return await self.async_step_edit_source()

        source_options = {s[CONF_SOURCE_ID]: _source_label(s) for s in cal_sources}
        schema = vol.Schema(
            {
                vol.Optional("source_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=k, label=v)
                            for k, v in source_options.items()
                        ]
                    )
                ),
                vol.Required("action", default="back"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value="edit", label="Edit"),
                            selector.SelectOptionDict(value="remove", label="Remove"),
                            selector.SelectOptionDict(value="back", label="← Back to calendar"),
                        ]
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="manage_calendar_sources",
            data_schema=schema,
            description_placeholders={"calendar": self._selected_calendar},
            errors=errors,
        )

    async def async_step_edit_source(self, user_input: dict | None = None):
        if self._editing_idx is None:
            return await self.async_step_manage_calendar()

        source = self._sources[self._editing_idx]
        errors: dict[str, str] = {}

        if user_input is not None:
            ics_urls = _parse_urls(user_input.get("ics_urls_raw", ""))
            if not ics_urls:
                errors["ics_urls_raw"] = "invalid_url"
            if not errors:
                self._sources[self._editing_idx] = {
                    **source,
                    CONF_SOURCE_URLS: ics_urls,
                    CONF_SOURCE_PREFIX: _combine_prefix(
                        user_input.get("prefix_emoji", ""),
                        user_input.get("prefix_text", ""),
                    ),
                    CONF_SOURCE_COLOR: user_input.get(CONF_SOURCE_COLOR, "").strip(),
                    CONF_SOURCE_USE_SE: user_input.get(CONF_SOURCE_USE_SE, False),
                    CONF_SE_ACCOUNT_ID: user_input.get(CONF_SE_ACCOUNT_ID, ""),
                    CONF_SOURCE_ENABLED: user_input.get(CONF_SOURCE_ENABLED, True),
                }
                self._options[CONF_SOURCES] = self._sources
                self._editing_idx = None
                return await self.async_step_manage_calendar_sources()

        existing_urls = source.get(CONF_SOURCE_URLS) or (
            [source[CONF_SOURCE_URL]] if source.get(CONF_SOURCE_URL) else []
        )
        urls_text = "\n".join(existing_urls)
        _prefix_emoji, _prefix_text = _split_prefix(source.get(CONF_SOURCE_PREFIX, ""))

        schema_dict: dict = {
            vol.Required("ics_urls_raw", description={"suggested_value": urls_text}): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional("prefix_emoji", default=_prefix_emoji): _emoji_selector(),
            vol.Optional("prefix_text", default=_prefix_text): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Optional(CONF_SOURCE_COLOR, default=source.get(CONF_SOURCE_COLOR, "")): selector.TextSelector(),
            vol.Optional(CONF_SOURCE_USE_SE, default=source.get(CONF_SOURCE_USE_SE, False)): selector.BooleanSelector(),
            vol.Optional(CONF_SOURCE_ENABLED, default=source.get(CONF_SOURCE_ENABLED, True)): selector.BooleanSelector(),
        }
        if self._se_accounts:
            schema_dict[vol.Optional(CONF_SE_ACCOUNT_ID, default=source.get(CONF_SE_ACCOUNT_ID, ""))] = _se_account_selector(self._se_accounts)
        return self.async_show_form(
            step_id="edit_source", data_schema=vol.Schema(schema_dict), errors=errors
        )

    async def async_step_confirm_remove(self, user_input: dict | None = None):
        if user_input is not None:
            if user_input.get("confirm") and self._editing_idx is not None:
                removed_id = self._sources[self._editing_idx][CONF_SOURCE_ID]
                self._sources.pop(self._editing_idx)
                self._options[CONF_SOURCES] = self._sources
                _remove_source_from_gcal_targets(self._gcal_targets, removed_id)
                self._options[CONF_GCAL_TARGETS] = self._gcal_targets
            self._editing_idx = None
            cal_sources = [s for s in self._sources if s.get(CONF_SOURCE_CALENDAR) == self._selected_calendar]
            if cal_sources:
                return await self.async_step_manage_calendar_sources()
            return await self.async_step_manage_calendar()

        if self._editing_idx is None:
            return await self.async_step_manage_calendar()

        source = self._sources[self._editing_idx]
        label = _source_label(source)
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

    async def async_step_remove_calendar(self, user_input: dict | None = None):
        """Confirm removing all sources for the selected calendar from the config."""
        if user_input is not None:
            if user_input.get("confirm"):
                removed_ids = {
                    s[CONF_SOURCE_ID]
                    for s in self._sources
                    if s.get(CONF_SOURCE_CALENDAR) == self._selected_calendar
                }
                self._sources = [
                    s for s in self._sources
                    if s.get(CONF_SOURCE_CALENDAR) != self._selected_calendar
                ]
                self._options[CONF_SOURCES] = self._sources
                for rid in removed_ids:
                    _remove_source_from_gcal_targets(self._gcal_targets, rid)
                self._options[CONF_GCAL_TARGETS] = self._gcal_targets
            self._selected_calendar = ""
            return await self.async_step_calendars()

        schema = vol.Schema(
            {
                vol.Required("confirm", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="remove_calendar",
            data_schema=schema,
            description_placeholders={"calendar": self._selected_calendar},
        )

    # ------------------------------------------------------------------ #
    # SE Tourney wizard (search → tournament → division → team → finalize)
    # ------------------------------------------------------------------ #

    async def async_step_add_se_tourney(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            query = user_input.get("query", "").strip()
            if not query:
                errors["query"] = "required"
            else:
                from .se_tourney_parser import async_search_tournaments
                self._se_tourney_search_results = await async_search_tournaments(self.hass, query)
                if not self._se_tourney_search_results:
                    errors["query"] = "no_results"
                else:
                    return await self.async_step_se_tourney_pick_tournament()
        schema = vol.Schema({
            vol.Required("query"): selector.TextSelector(),
        })
        return self.async_show_form(
            step_id="add_se_tourney",
            data_schema=schema,
            errors=errors,
            description_placeholders={"calendar": self._selected_calendar},
        )

    async def async_step_se_tourney_pick_tournament(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._se_tourney_tournament_id = user_input["tournament_id"]
            self._se_tourney_tournament_name = next(
                (r["name"] for r in self._se_tourney_search_results if r["id"] == self._se_tourney_tournament_id),
                "",
            )
            from .se_tourney_parser import async_fetch_divisions
            self._se_tourney_divisions = await async_fetch_divisions(self.hass, self._se_tourney_tournament_id)
            if not self._se_tourney_divisions:
                errors["tournament_id"] = "no_divisions"
            else:
                return await self.async_step_se_tourney_pick_division()

        options = [
            selector.SelectOptionDict(
                value=r["id"],
                label=f"{r['name']}{' — ' + r['dates'] if r.get('dates') else ''}{' (' + r['location'] + ')' if r.get('location') else ''}",
            )
            for r in self._se_tourney_search_results
        ]
        schema = vol.Schema({
            vol.Required("tournament_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            ),
        })
        return self.async_show_form(
            step_id="se_tourney_pick_tournament", data_schema=schema, errors=errors
        )

    async def async_step_se_tourney_pick_division(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._se_tourney_division_id = user_input["division_id"]
            self._se_tourney_division_name = next(
                (d["name"] for d in self._se_tourney_divisions if d["id"] == self._se_tourney_division_id),
                "",
            )
            from .se_tourney_parser import async_fetch_teams
            self._se_tourney_teams = await async_fetch_teams(
                self.hass, self._se_tourney_tournament_id, self._se_tourney_division_id
            )
            if not self._se_tourney_teams:
                errors["division_id"] = "no_teams"
            else:
                return await self.async_step_se_tourney_pick_team()

        options = [
            selector.SelectOptionDict(value=d["id"], label=d["name"])
            for d in self._se_tourney_divisions
        ]
        schema = vol.Schema({
            vol.Required("division_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            ),
        })
        return self.async_show_form(
            step_id="se_tourney_pick_division", data_schema=schema, errors=errors
        )

    async def async_step_se_tourney_pick_team(self, user_input: dict | None = None):
        if user_input is not None:
            team_id = user_input["team_id"]
            team_name = next((t["name"] for t in self._se_tourney_teams if t["id"] == team_id), "")
            return await self.async_step_se_tourney_finalize(
                _se_selection={"team_id": team_id, "team_name": team_name}
            )

        options = [
            selector.SelectOptionDict(value=t["id"], label=t["name"])
            for t in self._se_tourney_teams
        ]
        schema = vol.Schema({
            vol.Required("team_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            ),
        })
        return self.async_show_form(
            step_id="se_tourney_pick_team",
            data_schema=schema,
            description_placeholders={
                "tournament_name": self._se_tourney_tournament_name,
                "division_name": self._se_tourney_division_name,
            },
        )

    async def async_step_se_tourney_finalize(
        self,
        user_input: dict | None = None,
        _se_selection: dict | None = None,
    ):
        """Final step: prefix, color, game duration. Calendar already set by context."""
        if _se_selection is not None:
            self._se_tourney_pending_team = _se_selection

        if user_input is not None:
            selection = self._se_tourney_pending_team
            new_id = str(uuid.uuid4())
            self._sources.append({
                CONF_SOURCE_ID: new_id,
                CONF_SOURCE_TYPE: SOURCE_TYPE_SE_TOURNEY,
                CONF_SOURCE_CALENDAR: self._selected_calendar,
                CONF_SOURCE_PREFIX: _combine_prefix(
                    user_input.get("prefix_emoji", ""),
                    user_input.get("prefix_text", ""),
                ),
                CONF_SOURCE_COLOR: user_input.get(CONF_SOURCE_COLOR, "").strip(),
                CONF_SOURCE_ENABLED: True,
                CONF_SE_TOURNEY_TOURNAMENT_ID: self._se_tourney_tournament_id,
                CONF_SE_TOURNEY_TOURNAMENT_NAME: self._se_tourney_tournament_name,
                CONF_SE_TOURNEY_DIVISION_ID: self._se_tourney_division_id,
                CONF_SE_TOURNEY_DIVISION_NAME: self._se_tourney_division_name,
                CONF_SE_TOURNEY_TEAM_ID: selection["team_id"],
                CONF_SE_TOURNEY_TEAM_NAME: selection["team_name"],
                CONF_SE_TOURNEY_GAME_DURATION: int(
                    user_input.get(CONF_SE_TOURNEY_GAME_DURATION, DEFAULT_SE_TOURNEY_GAME_DURATION)
                ),
            })
            self._options[CONF_SOURCES] = self._sources
            self._new_source_id = new_id
            return await self.async_step_source_shared()

        schema = vol.Schema({
            vol.Optional("prefix_emoji", default=""): _emoji_selector(),
            vol.Optional("prefix_text", default=""): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Optional(CONF_SOURCE_COLOR, default=""): selector.TextSelector(),
            vol.Optional(
                CONF_SE_TOURNEY_GAME_DURATION, default=DEFAULT_SE_TOURNEY_GAME_DURATION
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=15, max=480, mode="box", unit_of_measurement="min")
            ),
        })
        return self.async_show_form(
            step_id="se_tourney_finalize",
            data_schema=schema,
            description_placeholders={
                "tournament_name": self._se_tourney_tournament_name,
                "division_name": self._se_tourney_division_name,
                "team_name": self._se_tourney_pending_team.get("team_name", ""),
                "calendar": self._selected_calendar,
            },
        )

    async def async_step_edit_se_tourney(self, user_input: dict | None = None):
        """Edit non-wizard fields of an SE Tourney source."""
        if self._editing_idx is None:
            return await self.async_step_manage_calendar()

        source = self._sources[self._editing_idx]

        if user_input is not None:
            self._sources[self._editing_idx] = {
                **source,
                CONF_SOURCE_PREFIX: _combine_prefix(
                    user_input.get("prefix_emoji", ""),
                    user_input.get("prefix_text", ""),
                ),
                CONF_SOURCE_COLOR: user_input.get(CONF_SOURCE_COLOR, "").strip(),
                CONF_SE_TOURNEY_GAME_DURATION: int(
                    user_input.get(CONF_SE_TOURNEY_GAME_DURATION, DEFAULT_SE_TOURNEY_GAME_DURATION)
                ),
                CONF_SOURCE_ENABLED: user_input.get(CONF_SOURCE_ENABLED, True),
            }
            self._options[CONF_SOURCES] = self._sources
            self._editing_idx = None
            return await self.async_step_manage_calendar_sources()

        team_info = " / ".join(filter(None, [
            source.get(CONF_SE_TOURNEY_TOURNAMENT_NAME, ""),
            source.get(CONF_SE_TOURNEY_DIVISION_NAME, ""),
            source.get(CONF_SE_TOURNEY_TEAM_NAME, ""),
        ]))
        _prefix_emoji, _prefix_text = _split_prefix(source.get(CONF_SOURCE_PREFIX, ""))
        schema = vol.Schema({
            vol.Optional("prefix_emoji", default=_prefix_emoji): _emoji_selector(),
            vol.Optional("prefix_text", default=_prefix_text): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Optional(
                CONF_SOURCE_COLOR,
                default=source.get(CONF_SOURCE_COLOR, ""),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_SE_TOURNEY_GAME_DURATION,
                default=source.get(CONF_SE_TOURNEY_GAME_DURATION, DEFAULT_SE_TOURNEY_GAME_DURATION),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=15, max=480, mode="box", unit_of_measurement="min")
            ),
            vol.Optional(
                CONF_SOURCE_ENABLED,
                default=source.get(CONF_SOURCE_ENABLED, True),
            ): selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="edit_se_tourney",
            data_schema=schema,
            description_placeholders={"team_info": team_info},
        )

    # ------------------------------------------------------------------ #
    # Post-source-creation: optionally link to a shareable calendar
    # ------------------------------------------------------------------ #

    async def async_step_source_shared(self, user_input: dict | None = None):
        """After creating a source: optionally add it to a shareable calendar."""
        if not self._gcal_targets:
            self._new_source_id = ""
            return await self.async_step_manage_calendar()

        if user_input is not None:
            target_id = user_input.get("target_id", "__none__")
            shared_prefix = user_input.get("shared_prefix", "").strip()

            if target_id != "__none__" and self._new_source_id:
                idx = next(
                    (i for i, t in enumerate(self._gcal_targets)
                     if t.get(CONF_GCAL_TARGET_ID) == target_id),
                    None,
                )
                if idx is not None:
                    source_ids = self._gcal_targets[idx].get(CONF_GCAL_TARGET_SOURCE_IDS, [])
                    if self._new_source_id not in source_ids:
                        source_ids = list(source_ids) + [self._new_source_id]
                    self._gcal_targets[idx][CONF_GCAL_TARGET_SOURCE_IDS] = source_ids

                    if shared_prefix:
                        prefixes = dict(self._gcal_targets[idx].get(CONF_GCAL_TARGET_SOURCE_PREFIXES, {}))
                        prefixes[self._new_source_id] = shared_prefix
                        self._gcal_targets[idx][CONF_GCAL_TARGET_SOURCE_PREFIXES] = prefixes

                    self._options[CONF_GCAL_TARGETS] = self._gcal_targets

            self._new_source_id = ""
            return await self.async_step_manage_calendar()

        options = [selector.SelectOptionDict(value="__none__", label="(none — primary calendar only)")]
        options += [
            selector.SelectOptionDict(
                value=t[CONF_GCAL_TARGET_ID],
                label=t.get(CONF_GCAL_TARGET_NAME, t[CONF_GCAL_TARGET_ID]),
            )
            for t in self._gcal_targets
        ]
        schema = vol.Schema({
            vol.Required("target_id", default="__none__"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional("shared_prefix", default=""): selector.TextSelector(
                selector.TextSelectorConfig()
            ),
        })
        return self.async_show_form(
            step_id="source_shared",
            data_schema=schema,
        )

    # ------------------------------------------------------------------ #
    # Shareable Google Calendars
    # ------------------------------------------------------------------ #

    async def async_step_shareable_cals(self, user_input: dict | None = None):
        """List existing shareable calendar targets or jump straight to creating one."""
        if not self._gcal_targets:
            return await self.async_step_new_gcal_target()

        if user_input is not None:
            choice = user_input.get("target_choice", "")
            if choice == "__new__":
                return await self.async_step_new_gcal_target()
            if choice:
                self._editing_gcal_target_idx = next(
                    (i for i, t in enumerate(self._gcal_targets)
                     if t.get(CONF_GCAL_TARGET_ID) == choice),
                    None,
                )
                if self._editing_gcal_target_idx is not None:
                    return await self.async_step_manage_gcal_target()

        options = [
            selector.SelectOptionDict(
                value=t[CONF_GCAL_TARGET_ID],
                label=t.get(CONF_GCAL_TARGET_NAME, t[CONF_GCAL_TARGET_ID]),
            )
            for t in self._gcal_targets
        ]
        options.append(selector.SelectOptionDict(value="__new__", label="Add a shareable calendar…"))
        schema = vol.Schema({
            vol.Required("target_choice"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(step_id="shareable_cals", data_schema=schema)

    async def async_step_new_gcal_target(self, user_input: dict | None = None):
        """Pick the Google Calendar name for the new shareable calendar."""
        if user_input is not None:
            cal = user_input.get(CONF_GCAL_TARGET_NAME, "").strip()
            if cal:
                new_target = {
                    CONF_GCAL_TARGET_ID: str(uuid.uuid4()),
                    CONF_GCAL_TARGET_NAME: cal,
                    CONF_GCAL_TARGET_SOURCE_IDS: [],
                }
                self._gcal_targets.append(new_target)
                self._options[CONF_GCAL_TARGETS] = self._gcal_targets
                self._editing_gcal_target_idx = len(self._gcal_targets) - 1
                return await self.async_step_manage_gcal_target()

        schema = vol.Schema({
            vol.Required(CONF_GCAL_TARGET_NAME): await self._calendar_selector(),
        })
        return self.async_show_form(step_id="new_gcal_target", data_schema=schema)

    async def async_step_manage_gcal_target(self, user_input: dict | None = None):
        """Menu for a selected shareable calendar target."""
        target = (
            self._gcal_targets[self._editing_gcal_target_idx]
            if self._editing_gcal_target_idx is not None
            else {}
        )
        has_sources = bool(target.get(CONF_GCAL_TARGET_SOURCE_IDS))
        menu_options = ["gcal_target_sources"]
        if has_sources:
            menu_options.append("gcal_target_source_prefixes")
        menu_options += ["confirm_remove_gcal_target", "back_to_shareable_cals", "done"]
        return self.async_show_menu(step_id="manage_gcal_target", menu_options=menu_options)

    async def async_step_back_to_shareable_cals(self, user_input: dict | None = None):
        self._editing_gcal_target_idx = None
        return await self.async_step_shareable_cals()

    async def async_step_gcal_target_sources(self, user_input: dict | None = None):
        """Multi-select which existing sources to include in the shareable calendar."""
        if self._editing_gcal_target_idx is None:
            return await self.async_step_shareable_cals()

        target = self._gcal_targets[self._editing_gcal_target_idx]

        if user_input is not None:
            selected = user_input.get("source_ids", [])
            self._gcal_targets[self._editing_gcal_target_idx][CONF_GCAL_TARGET_SOURCE_IDS] = selected
            self._options[CONF_GCAL_TARGETS] = self._gcal_targets
            return await self.async_step_manage_gcal_target()

        source_options = [
            selector.SelectOptionDict(
                value=s[CONF_SOURCE_ID],
                label=f"[{s.get(CONF_SOURCE_CALENDAR, '?')}] {_source_label(s)}",
            )
            for s in self._sources
        ]
        current_ids = target.get(CONF_GCAL_TARGET_SOURCE_IDS, [])
        schema = vol.Schema({
            vol.Optional("source_ids", default=current_ids): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=source_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="gcal_target_sources",
            data_schema=schema,
            description_placeholders={"calendar": target.get(CONF_GCAL_TARGET_NAME, "")},
        )

    async def async_step_gcal_target_source_prefixes(self, user_input: dict | None = None):
        """Select which source to set a display name for."""
        if self._editing_gcal_target_idx is None:
            return await self.async_step_shareable_cals()

        target = self._gcal_targets[self._editing_gcal_target_idx]
        source_ids = target.get(CONF_GCAL_TARGET_SOURCE_IDS, [])
        current_prefixes: dict[str, str] = target.get(CONF_GCAL_TARGET_SOURCE_PREFIXES, {})
        source_by_id = {s[CONF_SOURCE_ID]: s for s in self._sources}

        if user_input is not None:
            chosen = user_input.get("source_id", "__done__")
            if chosen == "__done__":
                return await self.async_step_manage_gcal_target()
            self._editing_gcal_target_source_id = chosen
            return await self.async_step_gcal_target_source_prefix_edit()

        options = []
        for sid in source_ids:
            source = source_by_id.get(sid)
            if source is None:
                continue
            base = f"[{source.get(CONF_SOURCE_CALENDAR, '?')}] {_source_label(source)}"
            current = current_prefixes.get(sid, "")
            label = f"{base}  —  ({current})" if current else base
            options.append(selector.SelectOptionDict(value=sid, label=label))

        if not options:
            return await self.async_step_manage_gcal_target()

        options.append(selector.SelectOptionDict(value="__done__", label="← Done"))
        schema = vol.Schema({
            vol.Required("source_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="gcal_target_source_prefixes",
            data_schema=schema,
            description_placeholders={"calendar": target.get(CONF_GCAL_TARGET_NAME, "")},
        )

    async def async_step_gcal_target_source_prefix_edit(self, user_input: dict | None = None):
        """Set or clear the display name for one source on this shareable calendar."""
        if self._editing_gcal_target_idx is None or not self._editing_gcal_target_source_id:
            return await self.async_step_shareable_cals()

        target = self._gcal_targets[self._editing_gcal_target_idx]
        sid = self._editing_gcal_target_source_id
        current_prefixes: dict[str, str] = dict(target.get(CONF_GCAL_TARGET_SOURCE_PREFIXES, {}))
        source_by_id = {s[CONF_SOURCE_ID]: s for s in self._sources}
        source = source_by_id.get(sid, {})
        source_label = f"[{source.get(CONF_SOURCE_CALENDAR, '?')}] {_source_label(source)}"

        if user_input is not None:
            val = user_input.get("shared_prefix", "").strip()
            if val:
                current_prefixes[sid] = val
            else:
                current_prefixes.pop(sid, None)
            self._gcal_targets[self._editing_gcal_target_idx][CONF_GCAL_TARGET_SOURCE_PREFIXES] = current_prefixes
            self._options[CONF_GCAL_TARGETS] = self._gcal_targets
            self._editing_gcal_target_source_id = ""
            return await self.async_step_gcal_target_source_prefixes()

        current = current_prefixes.get(sid, "")
        schema = vol.Schema({
            vol.Optional("shared_prefix", default=current): selector.TextSelector(
                selector.TextSelectorConfig()
            ),
        })
        return self.async_show_form(
            step_id="gcal_target_source_prefix_edit",
            data_schema=schema,
            description_placeholders={
                "calendar": target.get(CONF_GCAL_TARGET_NAME, ""),
                "source": source_label,
            },
        )

    async def async_step_confirm_remove_gcal_target(self, user_input: dict | None = None):
        """Confirm removal of a shareable calendar target."""
        if self._editing_gcal_target_idx is None:
            return await self.async_step_shareable_cals()

        if user_input is not None:
            if user_input.get("confirm"):
                self._gcal_targets.pop(self._editing_gcal_target_idx)
                self._options[CONF_GCAL_TARGETS] = self._gcal_targets
            self._editing_gcal_target_idx = None
            return await self.async_step_shareable_cals()

        target = self._gcal_targets[self._editing_gcal_target_idx]
        schema = vol.Schema({
            vol.Required("confirm", default=False): selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="confirm_remove_gcal_target",
            data_schema=schema,
            description_placeholders={"calendar": target.get(CONF_GCAL_TARGET_NAME, "")},
        )

    # ------------------------------------------------------------------ #
    # SportsEngine hub
    # ------------------------------------------------------------------ #

    async def async_step_sportsengine(self, user_input: dict | None = None):
        menu_options = ["add_se_account"]
        if self._se_accounts:
            menu_options.append("manage_se_accounts")
        menu_options += ["se_settings", "back_to_init", "done"]
        return self.async_show_menu(step_id="sportsengine", menu_options=menu_options)

    async def async_step_back_to_init(self, user_input: dict | None = None):
        return await self.async_step_init()

    # ------------------------------------------------------------------ #
    # SportsEngine settings
    # ------------------------------------------------------------------ #

    async def async_step_se_settings(self, user_input: dict | None = None):
        if user_input is not None:
            raw_abbrevs = user_input.pop("location_abbreviations_raw", "")
            self._options[CONF_LOCATION_ABBREVIATIONS] = _parse_abbreviations(raw_abbrevs)

            raw_removals = user_input.pop("se_title_removals_raw", "")
            self._options[CONF_SE_TITLE_REMOVALS] = [
                t.strip() for t in raw_removals.split(",") if t.strip()
            ]

            self._options.update(user_input)
            return await self.async_step_sportsengine()

        existing_abbrevs = self._options.get(CONF_LOCATION_ABBREVIATIONS, {})
        abbrevs_text = "\n".join(f"{k} = {v}" for k, v in existing_abbrevs.items())
        existing_removals = self._options.get(CONF_SE_TITLE_REMOVALS, [])
        removals_text = ", ".join(existing_removals)

        schema = vol.Schema(
            {
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
    # SE account management
    # ------------------------------------------------------------------ #

    async def async_step_add_se_account(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SE_ACCOUNT_NAME, "").strip():
                errors[CONF_SE_ACCOUNT_NAME] = "required"
            if not user_input.get(CONF_SE_USERNAME, "").strip():
                errors[CONF_SE_USERNAME] = "required"
            if not user_input.get(CONF_SE_PASSWORD, "").strip():
                errors[CONF_SE_PASSWORD] = "required"
            if not errors:
                self._se_accounts.append(
                    {
                        CONF_SE_ACCOUNT_ID: str(uuid.uuid4()),
                        CONF_SE_ACCOUNT_NAME: user_input[CONF_SE_ACCOUNT_NAME].strip(),
                        CONF_SE_USERNAME: user_input[CONF_SE_USERNAME].strip(),
                        CONF_SE_PASSWORD: user_input[CONF_SE_PASSWORD].strip(),
                    }
                )
                self._options[CONF_SE_ACCOUNTS] = self._se_accounts
                return await self.async_step_sportsengine()

        schema = vol.Schema(
            {
                vol.Required(CONF_SE_ACCOUNT_NAME): selector.TextSelector(),
                vol.Required(CONF_SE_USERNAME): selector.TextSelector(
                    selector.TextSelectorConfig(type="email", autocomplete="username")
                ),
                vol.Required(CONF_SE_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type="password", autocomplete="current-password")
                ),
            }
        )
        return self.async_show_form(step_id="add_se_account", data_schema=schema, errors=errors)

    async def async_step_manage_se_accounts(self, user_input: dict | None = None):
        if not self._se_accounts:
            return await self.async_step_sportsengine()

        if user_input is not None:
            account_id = user_input.get("se_account_id")
            self._editing_se_idx = next(
                (i for i, a in enumerate(self._se_accounts) if a[CONF_SE_ACCOUNT_ID] == account_id),
                None,
            )
            if user_input.get("action") == "remove":
                return await self.async_step_confirm_remove_se_account()
            return await self.async_step_edit_se_account()

        account_options = {
            a[CONF_SE_ACCOUNT_ID]: a[CONF_SE_ACCOUNT_NAME]
            for a in self._se_accounts
        }
        schema = vol.Schema(
            {
                vol.Required("se_account_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=k, label=v)
                            for k, v in account_options.items()
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
        return self.async_show_form(step_id="manage_se_accounts", data_schema=schema)

    async def async_step_edit_se_account(self, user_input: dict | None = None):
        if self._editing_se_idx is None:
            return await self.async_step_sportsengine()

        account = self._se_accounts[self._editing_se_idx]
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_SE_ACCOUNT_NAME, "").strip():
                errors[CONF_SE_ACCOUNT_NAME] = "required"
            if not user_input.get(CONF_SE_USERNAME, "").strip():
                errors[CONF_SE_USERNAME] = "required"
            if not user_input.get(CONF_SE_PASSWORD, "").strip():
                errors[CONF_SE_PASSWORD] = "required"
            if not errors:
                self._se_accounts[self._editing_se_idx] = {
                    **account,
                    CONF_SE_ACCOUNT_NAME: user_input[CONF_SE_ACCOUNT_NAME].strip(),
                    CONF_SE_USERNAME: user_input[CONF_SE_USERNAME].strip(),
                    CONF_SE_PASSWORD: user_input[CONF_SE_PASSWORD].strip(),
                }
                self._options[CONF_SE_ACCOUNTS] = self._se_accounts
                self._editing_se_idx = None
                return await self.async_step_sportsengine()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SE_ACCOUNT_NAME,
                    description={"suggested_value": account.get(CONF_SE_ACCOUNT_NAME, "")},
                ): selector.TextSelector(),
                vol.Required(
                    CONF_SE_USERNAME,
                    description={"suggested_value": account.get(CONF_SE_USERNAME, "")},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type="email", autocomplete="username")
                ),
                vol.Required(
                    CONF_SE_PASSWORD,
                    description={"suggested_value": account.get(CONF_SE_PASSWORD, "")},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type="password", autocomplete="current-password")
                ),
            }
        )
        return self.async_show_form(step_id="edit_se_account", data_schema=schema, errors=errors)

    async def async_step_confirm_remove_se_account(self, user_input: dict | None = None):
        if user_input is not None:
            if user_input.get("confirm") and self._editing_se_idx is not None:
                self._se_accounts.pop(self._editing_se_idx)
                self._options[CONF_SE_ACCOUNTS] = self._se_accounts
            self._editing_se_idx = None
            return await self.async_step_sportsengine()

        if self._editing_se_idx is None:
            return await self.async_step_sportsengine()

        account = self._se_accounts[self._editing_se_idx]
        schema = vol.Schema(
            {
                vol.Required("confirm", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="confirm_remove_se_account",
            data_schema=schema,
            description_placeholders={"account_label": account[CONF_SE_ACCOUNT_NAME]},
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


def _source_label(source: dict) -> str:
    """Return a display label for a source (calendar-scoped, so no calendar prefix)."""
    if source.get(CONF_SOURCE_TYPE) == SOURCE_TYPE_SE_TOURNEY:
        parts = [p for p in [
            source.get(CONF_SE_TOURNEY_TOURNAMENT_NAME, ""),
            source.get(CONF_SE_TOURNEY_TEAM_NAME, ""),
        ] if p]
        detail = " / ".join(parts) if parts else "SE Tourney"
        return f"SE Tourney: {detail}"
    return source.get(CONF_SOURCE_PREFIX) or _first_url(source)


def _emoji_selector() -> selector.SelectSelector:
    """Dropdown of searchable sport/activity emojis, with a blank '(none)' option."""
    options = [selector.SelectOptionDict(value="", label="(none)")]
    options += [
        selector.SelectOptionDict(value=emoji, label=label)
        for emoji, label in EMOJI_OPTIONS
    ]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _split_prefix(prefix: str) -> tuple[str, str]:
    """Split a stored prefix string into (emoji, text) for the two-field form.

    Handles prefixes written by _combine_prefix: '{emoji} {text}', '{emoji}', or '{text}'.
    """
    if not prefix:
        return ("", "")
    emoji_set = {opt[0] for opt in EMOJI_OPTIONS}
    for emoji in sorted(emoji_set, key=len, reverse=True):
        if prefix == emoji:
            return (emoji, "")
        if prefix.startswith(emoji + " "):
            return (emoji, prefix[len(emoji) + 1:])
    return ("", prefix)


def _combine_prefix(emoji: str, text: str) -> str:
    """Combine emoji and text fields into a single stored prefix string."""
    emoji = emoji.strip()
    text = text.strip()
    if emoji and text:
        return f"{emoji} {text}"
    return emoji or text


def _se_account_selector(se_accounts: list[dict]) -> selector.SelectSelector:
    """Return a SelectSelector for choosing an SE account (blank = none/any)."""
    options = [selector.SelectOptionDict(value="", label="(none)")]
    for acct in se_accounts:
        options.append(selector.SelectOptionDict(
            value=acct[CONF_SE_ACCOUNT_ID],
            label=acct[CONF_SE_ACCOUNT_NAME],
        ))
    return selector.SelectSelector(selector.SelectSelectorConfig(options=options))


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


def _remove_source_from_gcal_targets(gcal_targets: list[dict], source_id: str) -> None:
    """Remove a source ID from all shareable calendar target lists and prefix maps."""
    for target in gcal_targets:
        ids = target.get(CONF_GCAL_TARGET_SOURCE_IDS, [])
        if source_id in ids:
            target[CONF_GCAL_TARGET_SOURCE_IDS] = [i for i in ids if i != source_id]
        prefixes = target.get(CONF_GCAL_TARGET_SOURCE_PREFIXES, {})
        if source_id in prefixes:
            prefixes = dict(prefixes)
            del prefixes[source_id]
            target[CONF_GCAL_TARGET_SOURCE_PREFIXES] = prefixes

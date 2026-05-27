"""Constants for the ICS to Google Calendar Sync integration."""

DOMAIN = "ics_gcal_sync"

OAUTH2_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH2_TOKEN = "https://oauth2.googleapis.com/token"
OAUTH2_SCOPES = ["https://www.googleapis.com/auth/calendar"]

GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"

# Extended property used to identify events created by this integration.
# Named "fromGAS" for backwards compatibility with events already synced
# by the predecessor Google Apps Script.
GCAL_MARKER_KEY = "fromGAS"
GCAL_MARKER_VALUE = "true"

# ------------------------------------------------------------------ #
# Config / options keys
# ------------------------------------------------------------------ #
CONF_SOURCES = "sources"
CONF_SYNC_INTERVAL = "sync_interval"
CONF_ADD_EVENTS = "add_events"
CONF_MODIFY_EVENTS = "modify_events"
CONF_REMOVE_EVENTS = "remove_events"
CONF_REMOVE_PAST_EVENTS = "remove_past_events"
CONF_TITLE_CASE = "title_case"
CONF_LOCATION_ABBREVIATIONS = "location_abbreviations"

# Per-source keys
CONF_SOURCE_ID = "id"
CONF_SOURCE_URL = "ics_url"    # legacy single-URL key — kept for migration reads only
CONF_SOURCE_URLS = "ics_urls"  # current multi-URL key (list of strings)
CONF_SOURCE_CALENDAR = "target_calendar"
CONF_SOURCE_PREFIX = "team_name"  # stored key kept as "team_name" for backwards compat
CONF_SOURCE_COLOR = "color_id"
CONF_SOURCE_ENABLED = "enabled"
CONF_SOURCE_USE_SE = "use_se_enricher"

# SportsEngine accounts list
CONF_SE_ACCOUNTS = "se_accounts"
CONF_SE_ACCOUNT_ID = "se_account_id"
CONF_SE_ACCOUNT_NAME = "se_account_name"

# SportsEngine per-account keys (also used as global fallback keys for backwards compat)
CONF_SE_USERNAME = "se_username"
CONF_SE_PASSWORD = "se_password"

# SportsEngine global settings
CONF_SE_TITLE_REMOVALS = "se_title_removals"

# ------------------------------------------------------------------ #
# Defaults
# ------------------------------------------------------------------ #
DEFAULT_SYNC_INTERVAL = 15
DEFAULT_ADD_EVENTS = True
DEFAULT_MODIFY_EVENTS = True
DEFAULT_REMOVE_EVENTS = True
DEFAULT_REMOVE_PAST_EVENTS = True
DEFAULT_TITLE_CASE = True

# ------------------------------------------------------------------ #
# Source types
# ------------------------------------------------------------------ #
CONF_SOURCE_TYPE = "source_type"
SOURCE_TYPE_ICS = "ics"
SOURCE_TYPE_SE_TOURNEY = "se_tourney"

# SportsEngine Tourney per-source keys.
# NOTE: string VALUES are kept as "tm_*" for backward compatibility with
# already-stored config entries — only the Python constant names changed.
CONF_SE_TOURNEY_TOURNAMENT_ID = "tm_tournament_id"
CONF_SE_TOURNEY_DIVISION_ID = "tm_division_id"
CONF_SE_TOURNEY_TEAM_ID = "tm_team_id"
CONF_SE_TOURNEY_TOURNAMENT_NAME = "tm_tournament_name"
CONF_SE_TOURNEY_DIVISION_NAME = "tm_division_name"
CONF_SE_TOURNEY_TEAM_NAME = "tm_team_name"
CONF_SE_TOURNEY_GAME_DURATION = "tm_game_duration"

DEFAULT_SE_TOURNEY_GAME_DURATION = 90

# ------------------------------------------------------------------ #
# SportsEngine API URLs
# ------------------------------------------------------------------ #
SE_LOGIN_URL = "https://user.sportngin.com/users/sign_in"
SE_API_CALENDAR_URL = "https://api.sportngin.com/v3/calendar/mine"

# ------------------------------------------------------------------ #
# SportsEngine Tourney (TourneyMachine) page URLs
# ------------------------------------------------------------------ #
SE_TOURNEY_SEARCH_API_URL = "https://api.tourneymachine.com/private/v1/TournamentSearch/Tournaments/{query}"
SE_TOURNEY_TOURNAMENT_PAGE_URL = "https://tourneymachine.com/Public/Results/Tournament.aspx"
SE_TOURNEY_DIVISION_PAGE_URL = "https://tourneymachine.com/Public/Results/Division.aspx"
SE_TOURNEY_TEAM_PAGE_URL = "https://tourneymachine.com/Public/Results/Team.aspx"

# ------------------------------------------------------------------ #
# Shareable / additional Google Calendar targets
# ------------------------------------------------------------------ #
# A list of extra Google Calendars that pull events from existing sources
# without duplicating the source configuration.  Each entry is a dict with:
#   CONF_GCAL_TARGET_ID        — internal UUID
#   CONF_GCAL_TARGET_NAME      — Google Calendar name
#   CONF_GCAL_TARGET_SOURCE_IDS — list of CONF_SOURCE_ID values to include
CONF_GCAL_TARGETS = "gcal_targets"
CONF_GCAL_TARGET_ID = "gcal_target_id"
CONF_GCAL_TARGET_NAME = "gcal_target_calendar"
CONF_GCAL_TARGET_SOURCE_IDS = "gcal_target_source_ids"
# Maps source_id → display prefix used only on the shareable calendar copy.
CONF_GCAL_TARGET_SOURCE_PREFIXES = "gcal_target_source_prefixes"

# ------------------------------------------------------------------ #
# Repair issue IDs
# ------------------------------------------------------------------ #
ISSUE_SE_LOGIN_FAILED = "se_login_failed"

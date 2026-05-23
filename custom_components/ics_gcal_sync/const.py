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

# SportsEngine settings (global)
CONF_SE_USERNAME = "se_username"
CONF_SE_PASSWORD = "se_password"
CONF_SE_TITLE_REMOVALS = "se_title_removals"

# ------------------------------------------------------------------ #
# Defaults
# ------------------------------------------------------------------ #
DEFAULT_SYNC_INTERVAL = 15
DEFAULT_ADD_EVENTS = True
DEFAULT_MODIFY_EVENTS = True
DEFAULT_REMOVE_EVENTS = True
DEFAULT_REMOVE_PAST_EVENTS = True

# ------------------------------------------------------------------ #
# SportsEngine URLs
# ------------------------------------------------------------------ #
SE_LOGIN_URL = "https://user.sportngin.com/users/sign_in"
SE_API_CALENDAR_URL = "https://api.sportngin.com/v3/calendar/mine"

# ------------------------------------------------------------------ #
# Repair issue IDs
# ------------------------------------------------------------------ #
ISSUE_SE_LOGIN_FAILED = "se_login_failed"

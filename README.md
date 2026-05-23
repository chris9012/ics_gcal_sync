# ICS to Google Calendar Sync

A Home Assistant custom integration that syncs ICS/iCal calendar feeds into Google Calendar, with support for SportsEngine field lookup and automatic session management.

## Features

- Sync one or more ICS/iCal feeds into Google Google Calendar
- Add, update, and remove events automatically as the source feeds change
- **SportsEngine enrichment**: automatically fetches field/location details from the SportsEngine API and applies friendly venue name abbreviations
- Automatic SportsEngine session management — re-authenticates silently when the session expires
- Configurable location abbreviations and title cleanup rules
- All configuration managed through the Home Assistant UI

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS (Integrations category)
2. Install **ICS to Google Calendar Sync**
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for *ICS to Google Calendar Sync*

### Manual

Copy the `custom_components/ics_gcal_sync` directory into your Home Assistant `config/custom_components/` directory, then restart.

## Setup

### 1. Google Cloud OAuth credentials

This integration uses OAuth2 to access your Google Calendar. You need to create credentials in Google Cloud:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or reuse the one you created for the built-in `google_calendar` integration)
3. Enable the **Google Calendar API**
4. Under **APIs & Services → Credentials**, create an **OAuth 2.0 Client ID** (type: Web application)
5. Add `https://<your-ha-url>/auth/external/callback` as an authorized redirect URI
6. Copy the **Client ID** and **Client Secret**

### 2. Add the integration

1. In Home Assistant, go to **Settings → Devices & Services → Add Integration**
2. Search for *ICS to Google Calendar Sync*
3. Enter your Google OAuth Client ID and Client Secret when prompted
4. Complete the Google sign-in flow

### 3. Configure calendar sources

After authentication, configure your ICS sources via **Configure** on the integration card:

- **ICS URL**: The `.ics` feed URL to sync
- **Target Google Calendar**: Name of the destination Google Calendar (created if it doesn't exist)
- **Team name**: Optional label prepended to event titles
- **SportsEngine enrichment**: Enable to fetch field/location data from the SportsEngine API

## SportsEngine Enrichment

When enabled, the SportsEngine enricher:

- Fetches upcoming event field/location details from `api.sportngin.com`
- Applies **location abbreviations** to replace long venue names/addresses with short friendly names
- Strips configured **title tokens** (e.g. sport/division codes) from event titles
- Automatically re-authenticates using your SE username and password when the session expires

## License

MIT

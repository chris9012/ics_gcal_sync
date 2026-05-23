"""ICS to Google Calendar Sync integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN
from .coordinator import ICSGCalSyncCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ICS GCal Sync from a config entry."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    coordinator = ICSGCalSyncCoordinator(hass, entry, oauth_session)

    # Trigger an initial sync immediately on setup
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True

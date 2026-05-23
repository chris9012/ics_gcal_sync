"""Sensor platform: one sensor per config entry showing last sync status."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ICSGCalSyncCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ICSGCalSyncCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ICSGCalSyncStatusSensor(coordinator, entry)])


class ICSGCalSyncStatusSensor(CoordinatorEntity[ICSGCalSyncCoordinator], SensorEntity):
    """Reports the outcome of the most recent sync pass."""

    _attr_icon = "mdi:calendar-sync"
    _attr_has_entity_name = True
    _attr_name = "Last Sync"

    def __init__(self, coordinator: ICSGCalSyncCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_sync"
        self._entry = entry

    @property
    def native_value(self) -> str:
        """Return last sync time or error indicator."""
        if not self.coordinator.last_update_success:
            return "Error"
        if self.coordinator.last_sync_time is None:
            return "Never"
        return self.coordinator.last_sync_time.isoformat()

    @property
    def extra_state_attributes(self) -> dict:
        results = self.coordinator.data or []
        calendars = []
        total_errors: list[str] = []
        for result in results:
            calendars.append(
                {
                    "calendar": result.calendar_name,
                    "added": result.added,
                    "modified": result.modified,
                    "removed": result.removed,
                    "errors": len(result.errors),
                }
            )
            total_errors.extend(result.errors)
        return {
            "calendars": calendars,
            "total_added": sum(r.added for r in results),
            "total_modified": sum(r.modified for r in results),
            "total_removed": sum(r.removed for r in results),
            "error_count": len(total_errors),
            "last_errors": total_errors[:5],  # cap to avoid huge state
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "ICS GCal Sync",
            "manufacturer": "ICS GCal Sync",
            "model": "Calendar Sync",
            "entry_type": "service",
        }

"""Enricher plugin system for ICS → Google Calendar sync.

Enrichers run after ICS parsing and before MD5 computation / GCal diffing.
They can mutate ParsedEvent fields (location, summary, etc.) and set
enrichment_suffix so that their changes are reflected in the change-detection hash.

To add a new enricher:
  1. Subclass BaseEnricher.
  2. Implement async_prepare (called once per sync, before events are processed).
  3. Implement async_enrich (called per event).
  4. Register it in coordinator.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from homeassistant.core import HomeAssistant

from ..models import CalendarSource, ParsedEvent


class BaseEnricher(ABC):
    """Abstract base for all enrichers."""

    @abstractmethod
    async def async_prepare(
        self,
        hass: HomeAssistant,
        sources: list[CalendarSource],
        options: dict,
    ) -> None:
        """Called once per sync before any events are processed.

        Use this for bulk API lookups (e.g. fetch all SE locations in one call).
        """

    @abstractmethod
    async def async_enrich(
        self,
        event: ParsedEvent,
        options: dict,
    ) -> ParsedEvent:
        """Enrich a single event. Mutate and return the event."""

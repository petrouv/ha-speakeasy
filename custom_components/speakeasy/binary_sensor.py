"""`binary_sensor.speakeasy_recording`: on while any recording runs or saves its file."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SpeakeasyConfigEntry
from .const import DOMAIN
from .recorder import RecordingManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpeakeasyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the single sensor."""
    async_add_entities([RecordingSensor(entry.runtime_data, entry.entry_id)])


class RecordingSensor(BinarySensorEntity):
    """Makes a forgotten recording visible and lets automations react to it."""

    _attr_has_entity_name = True
    _attr_translation_key = "recording"
    _attr_should_poll = False

    def __init__(self, manager: RecordingManager, entry_id: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_recording"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Speakeasy",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Follow recording changes."""
        self.async_on_remove(self._manager.async_subscribe(self._changed))

    @callback
    def _changed(self, _event: str, _payload: dict[str, Any]) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Whether a recording is running or saving."""
        return bool(self._manager.recordings)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Recordings without their URLs, which would land in the history database."""
        return {"recordings": [rec.as_attribute() for rec in self._manager.recordings]}

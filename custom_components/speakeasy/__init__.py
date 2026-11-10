"""Speakeasy: server-side recording of go2rtc camera streams."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .recorder import RecordingManager
from .services import async_setup_services
from .websocket_api import async_follow_manager, async_setup_websocket_api

PLATFORMS = [Platform.BINARY_SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SpeakeasyConfigEntry = ConfigEntry[RecordingManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the WebSocket commands and actions once.

    Home Assistant runs this only for an enabled config entry (config-entry-only schema;
    a leftover `speakeasy:` YAML key runs it too), so without one since the start the card
    gets `unknown_command`, as if not installed. The commands stay after the entry is
    removed, disabled or unloaded: info and the subscription report `configured: false`,
    starting and stopping answer `not_configured`.
    """
    async_setup_websocket_api(hass)
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SpeakeasyConfigEntry) -> bool:
    """Set up the single config entry."""
    manager = RecordingManager(hass, entry)
    entry.runtime_data = manager

    # A shutdown job runs before Home Assistant cancels background tasks, so
    # recordings get up to 20 s to be finalized; whatever is left stays `.part`.
    entry.async_on_unload(hass.async_add_shutdown_job(HassJob(manager.async_stop_all)))
    entry.async_on_unload(manager.async_track_cleanup())
    # Undone after async_unload_entry, so the subscriptions hear the recordings it finishes.
    entry.async_on_unload(async_follow_manager(hass, manager))

    # Listed now: until the entry is loaded no recording can start, so every part found
    # is a leftover. A later listing could catch the empty part of a new recording.
    leftover_parts = await manager.async_list_parts()

    async def _upkeep() -> None:
        await manager.async_finish_parts(leftover_parts)
        await manager.async_delete_old()

    @callback
    def _start_upkeep(_hass: HomeAssistant) -> None:
        # In the background: remuxing a long leftover recording must not hold up anything.
        entry.async_create_background_task(hass, _upkeep(), f"{DOMAIN} upkeep")

    entry.async_on_unload(async_at_started(hass, _start_upkeep))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpeakeasyConfigEntry) -> bool:
    """Finish all recordings, then unload."""
    await entry.runtime_data.async_stop_all()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

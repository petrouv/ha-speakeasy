"""WebSocket commands for the card.

Any logged-in user may record (owner's decision), so no command requires admin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.loader import async_get_integration
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, FEATURES, PROTOCOL_VERSION
from .recorder import EVENT_LIST, RecordingError, RecordingManager, Source, async_get_manager

SOURCE_SCHEMA = vol.Schema(
    {
        vol.Exclusive("url", "source"): cv.string,
        vol.Exclusive("entity", "source"): cv.entity_id,
        vol.Optional("server"): cv.string,
        vol.Optional("record_url"): cv.string,
    }
)


@dataclass
class RecordingFeed:
    """What `record/subscribe` follows: whether an entry is loaded, and its recordings.

    It lives as long as the commands do, not as long as one entry's manager, so a
    subscription stays open while the entry is removed, disabled, reloaded or added.
    """

    configured: bool = False
    manager: RecordingManager | None = None
    listeners: list[Callable[[str, dict[str, Any]], None]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        """Payload of the `list` event."""
        recordings = self.manager.recordings if self.manager is not None else []
        return {"configured": self.configured, "recordings": [rec.as_dict() for rec in recordings]}

    @callback
    def send(self, event: str, payload: dict[str, Any]) -> None:
        """Pass an event to every subscription."""
        for listener in list(self.listeners):
            listener(event, payload)


DATA_FEED: HassKey[RecordingFeed] = HassKey(DOMAIN)


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Register the commands and start following the config entry."""
    feed = hass.data[DATA_FEED] = RecordingFeed()

    @callback
    def entry_changed(_change: ConfigEntryChange, entry: ConfigEntry) -> None:
        # Same test as `info`. An unload turns it false as it begins, so the recordings
        # it finishes are still listed, and their `stopping` and `stopped` follow.
        if entry.domain != DOMAIN:
            return
        configured = bool(hass.config_entries.async_loaded_entries(DOMAIN))
        if configured != feed.configured:
            feed.configured = configured
            feed.send(EVENT_LIST, feed.snapshot())

    # Never disconnected: like the commands, it stays until Home Assistant stops.
    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, entry_changed)
    websocket_api.async_register_command(hass, ws_info)
    websocket_api.async_register_command(hass, ws_record_start)
    websocket_api.async_register_command(hass, ws_record_stop)
    websocket_api.async_register_command(hass, ws_record_subscribe)


@callback
def async_follow_manager(hass: HomeAssistant, manager: RecordingManager) -> CALLBACK_TYPE:
    """Pass an entry's recording events to the subscriptions until the entry unloads."""
    feed = hass.data[DATA_FEED]
    feed.manager = manager
    unsubscribe = manager.async_subscribe(feed.send)

    @callback
    def stop() -> None:
        unsubscribe()
        if feed.manager is manager:
            feed.manager = None

    return stop


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/info"})
@websocket_api.async_response
async def ws_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Report the version and the protocol, so the card can tell what is supported."""
    integration = await async_get_integration(hass, DOMAIN)
    connection.send_result(
        msg["id"],
        {
            "version": str(integration.version),
            "protocol": PROTOCOL_VERSION,
            "features": FEATURES,
            "configured": bool(hass.config_entries.async_loaded_entries(DOMAIN)),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/record/start", vol.Required("source"): SOURCE_SCHEMA}
)
@websocket_api.async_response
async def ws_record_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Start recording, or return the recording already running for this source."""
    try:
        recording = await async_get_manager(hass).async_start(Source(**msg["source"]))
    except RecordingError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(
        msg["id"], {"id": recording.id, "started_at": recording.started_at.isoformat()}
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/record/stop", vol.Required("recording_id"): str}
)
@websocket_api.async_response
async def ws_record_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Stop a recording and reply once the file is final."""
    try:
        manager = async_get_manager(hass)
    except RecordingError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    if (recording := manager.get(msg["recording_id"])) is None:
        connection.send_error(msg["id"], "not_found", "No such recording")
        return
    result = await manager.async_stop(recording)
    if result is None and recording.part_kept:
        connection.send_error(
            msg["id"],
            "data_kept",
            "Not saved yet: the data is kept and saving is tried again at the integration's "
            "next start, if the recordings folder in the options is then still the one it was "
            "recorded in",
        )
        return
    if result is None:
        connection.send_error(
            msg["id"], "no_data", "Nothing was saved: the file could not be written, or was removed"
        )
        return
    connection.send_result(msg["id"], result.as_dict())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/record/subscribe"})
@callback
def ws_record_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send whether an entry is loaded and the current recordings, then every change.

    Works without a loaded entry too. Events: `list` first and again whenever
    `configured` changes, then `started`, `stopping`, `stopped` and `failed`.
    """
    feed = hass.data[DATA_FEED]

    @callback
    def forward(event: str, payload: dict[str, Any]) -> None:
        connection.send_event(msg["id"], {"event": event, **payload})

    @callback
    def unsubscribe() -> None:
        feed.listeners.remove(forward)

    feed.listeners.append(forward)
    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    forward(EVENT_LIST, feed.snapshot())

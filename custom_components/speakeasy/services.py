"""Actions for automations: none waits for a file to be saved.

A stop answers right away, a start once go2rtc sends data.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID, CONF_URL
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .go2rtc import clean_url
from .recorder import RecordingError, Source, async_get_manager

SERVICE_RECORD_START = "record_start"
SERVICE_RECORD_STOP = "record_stop"
SERVICE_RECORD_STOP_ALL = "record_stop_all"

ATTR_SERVER = "server"
ATTR_RECORD_URL = "record_url"


def _server(value: Any) -> str:
    # Kept as written, not cleaned: it is part of the source's key, as the card sends it.
    server = cv.string(value)
    # Empty means the configured server, as in async_start and the WebSocket schema.
    if server and clean_url(server) is None:
        raise vol.Invalid("server is not a URL")
    return server


def _not_blank(field: str) -> Callable[[Any], str]:
    def validate(value: Any) -> str:
        # A template that renders empty would reach go2rtc as no stream at all.
        text = cv.string(value)
        if not text.strip():
            raise vol.Invalid(f"{field} is empty")
        return text

    return validate


# Wrong caller input fails in the schema: a 400, not a server error.
_SOURCE_FIELDS = {
    vol.Exclusive(CONF_URL, "source"): _not_blank(CONF_URL),
    # cv.entity_id first: entity_domain alone also takes a list of one id.
    vol.Exclusive(ATTR_ENTITY_ID, "source"): vol.All(cv.entity_id, cv.entity_domain("camera")),
    vol.Optional(ATTR_SERVER): _server,
}
_HAS_SOURCE = cv.has_at_least_one_key(CONF_URL, ATTR_ENTITY_ID)
START_SCHEMA = vol.All(
    vol.Schema({**_SOURCE_FIELDS, vol.Optional(ATTR_RECORD_URL): _not_blank(ATTR_RECORD_URL)}),
    _HAS_SOURCE,
)
STOP_SCHEMA = vol.All(vol.Schema(_SOURCE_FIELDS), _HAS_SOURCE)


def _source(call: ServiceCall) -> Source:
    return Source(
        url=call.data.get(CONF_URL),
        entity=call.data.get(ATTR_ENTITY_ID),
        server=call.data.get(ATTR_SERVER),
        record_url=call.data.get(ATTR_RECORD_URL),
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions."""

    async def record_start(call: ServiceCall) -> ServiceResponse:
        source = _source(call)
        manager = async_get_manager(hass)
        # An id with no state is the caller's mistake; an unavailable camera stays a failure.
        # A recording already running for it is still returned, as before the state went.
        if (
            source.entity
            and hass.states.get(source.entity) is None
            and manager.find(source) is None
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="camera_not_found",
                translation_placeholders={"entity": source.entity},
            )
        # Waits for an ending recording of the source (stopped, or ended by itself) to close
        # its file, then for go2rtc's first data; never for the new recording to end.
        try:
            recording = await manager.async_start(source)
        except RecordingError as err:
            # A `server` other than the configured go2rtc is the caller's mistake: a 400.
            if err.code == "invalid_source":
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="invalid_source"
                ) from None
            raise
        return {"id": recording.id, "started_at": recording.started_at.isoformat()}

    async def record_stop(call: ServiceCall) -> ServiceResponse:
        manager = async_get_manager(hass)
        source = _source(call)
        checking = manager.async_stop_checks(source)
        # Reply with the running one once it has data, else the newest saving one (a
        # source's recordings turn `saving` in the order they started). A start without
        # data was never announced, so it is skipped, as in record_stop_all, whether still
        # in the checks or waiting for data: null when the source has only such starts,
        # not_found when it has nothing at all.
        if (recording := manager.find(source)) is not None:
            manager.async_request_stop(recording)
            if recording.connected.done():
                return {"id": recording.id}
        if (saving := manager.find_saving(source)) is not None:
            return {"id": saving.id}
        if recording is None and not checking:
            # The caller's mistake, not a failure: REST answers 400 with the translated text.
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_found")
        return {"id": None}

    async def record_stop_all(call: ServiceCall) -> ServiceResponse:
        return {"ids": async_get_manager(hass).async_request_stop_all()}

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_START,
        record_start,
        schema=START_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_STOP,
        record_stop,
        schema=STOP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_STOP_ALL,
        record_stop_all,
        supports_response=SupportsResponse.OPTIONAL,
    )

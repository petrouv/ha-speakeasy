"""UI setup: go2rtc address and recording options."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_FOLDER,
    CONF_KEEP_DAYS,
    CONF_MIN_FREE_GB,
    DEFAULT_FOLDER,
    DEFAULT_KEEP_DAYS,
    DEFAULT_MIN_FREE_GB,
    DOMAIN,
    MAX_KEEP_DAYS,
    MAX_MIN_FREE_GB,
)
from .go2rtc import Go2rtcError, check_server, clean_url, discover_url

# One or more plain folder names below the media folder: no `..`, no absolute path.
_FOLDER = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")


def _number(maximum: int) -> vol.All:
    selector = NumberSelector(
        NumberSelectorConfig(min=0, max=maximum, step=1, mode=NumberSelectorMode.BOX)
    )
    return vol.All(selector, vol.Coerce(int))


class SpeakeasyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Creates the single entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: Any) -> SpeakeasyOptionsFlow:
        """Return the options flow."""
        return SpeakeasyOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the go2rtc address, prefilled with the one WebRTC Camera uses.

        A second entry is refused by `single_config_entry` in the manifest.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            url = clean_url(user_input[CONF_URL])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            else:
                try:
                    await check_server(async_get_clientsession(self.hass), url)
                except Go2rtcError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(title="Speakeasy", data={CONF_URL: url})

        default = user_input[CONF_URL] if user_input else discover_url(self.hass.config_entries)
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=default): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the go2rtc address in place.

        The entry is reloaded, which finishes running recordings.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = clean_url(user_input[CONF_URL])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            else:
                try:
                    await check_server(async_get_clientsession(self.hass), url)
                except Go2rtcError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_URL: url})

        default = user_input[CONF_URL] if user_input else entry.data[CONF_URL]
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=default): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                )
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class SpeakeasyOptionsFlow(OptionsFlow):
    """Options are read on every use, so saving them needs no reload.

    A reload would cut running recordings.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and validate the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            folder = user_input[CONF_FOLDER].strip().strip("/")
            if _FOLDER.fullmatch(folder) is None:
                errors[CONF_FOLDER] = "invalid_folder"
            else:
                return self.async_create_entry(data={**user_input, CONF_FOLDER: folder})

        values = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FOLDER, default=values.get(CONF_FOLDER, DEFAULT_FOLDER)
                ): TextSelector(),
                vol.Required(
                    CONF_MIN_FREE_GB, default=values.get(CONF_MIN_FREE_GB, DEFAULT_MIN_FREE_GB)
                ): _number(MAX_MIN_FREE_GB),
                vol.Required(
                    CONF_KEEP_DAYS, default=values.get(CONF_KEEP_DAYS, DEFAULT_KEEP_DAYS)
                ): _number(MAX_KEEP_DAYS),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

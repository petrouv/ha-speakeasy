"""Config flow and options."""

from __future__ import annotations

import json
import re
from pathlib import Path

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.speakeasy.const import (
    CONF_FOLDER,
    CONF_KEEP_DAYS,
    CONF_MIN_FREE_GB,
    DEFAULT_URL,
    DOMAIN,
)
from custom_components.speakeasy.go2rtc import clean_url, mask_secrets, source_name

from .conftest import FakeGo2rtc


def suggested_url(result) -> str:
    """Default of the URL field in a shown form."""
    for key in result["data_schema"].schema:
        if key == CONF_URL:
            return key.default()
    raise AssertionError("no URL field")


async def test_default_address_without_webrtc(hass: HomeAssistant) -> None:
    """Without WebRTC Camera the form suggests the address WebRTC would fall back to."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert suggested_url(result) == DEFAULT_URL


async def test_default_address_from_webrtc(hass: HomeAssistant) -> None:
    """A server set in WebRTC Camera is suggested."""
    MockConfigEntry(domain="webrtc", data={CONF_URL: "http://nas.lan:1984/"}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert suggested_url(result) == "http://nas.lan:1984"


async def test_create_entry(hass: HomeAssistant, go2rtc: FakeGo2rtc) -> None:
    """A reachable go2rtc creates the entry; a page name copied from the UI is dropped."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{go2rtc.url}/stream.html?src=door"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: go2rtc.url}

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "single_instance_allowed"


async def test_errors(hass: HomeAssistant, socket_enabled) -> None:
    """A bad address and an unreachable go2rtc are reported on the form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "not a url"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://127.0.0.1:9"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_do_not_reload(
    hass: HomeAssistant, loaded: MockConfigEntry, media_dir: Path
) -> None:
    """Saving options keeps the entry loaded, so recordings are not cut."""
    manager = loaded.runtime_data
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FOLDER: "../etc", CONF_MIN_FREE_GB: 2, CONF_KEEP_DAYS: 0}
    )
    assert result["errors"] == {CONF_FOLDER: "invalid_folder"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FOLDER: "/cameras/door/", CONF_MIN_FREE_GB: 5, CONF_KEEP_DAYS: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert loaded.options == {CONF_FOLDER: "cameras/door", CONF_MIN_FREE_GB: 5, CONF_KEEP_DAYS: 30}
    assert loaded.runtime_data is manager
    assert manager.folder == media_dir / "cameras" / "door"


async def test_reconfigure(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """The go2rtc address is changed in place and the entry is reloaded."""
    result = await loaded.start_reconfigure_flow(hass)
    assert suggested_url(result) == loaded.data[CONF_URL]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "not a url"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{go2rtc.url}/"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded.data == {CONF_URL: go2rtc.url}
    assert loaded.state is ConfigEntryState.LOADED


def test_clean_url_and_masking() -> None:
    """Addresses are normalized; camera passwords never leave in clear text."""
    assert clean_url(" http://host:1984/ ") == "http://host:1984"
    assert clean_url("ftp:// nope") is None
    assert mask_secrets("rtsp://admin:pw@cam/1 and http://u:p@x/y") == "rtsp://*** and http://***"
    # As a request's `?src=` carries a source: URL-encoded.
    assert mask_secrets("/api/streams?src=http%3A%2F%2Fu%3Ap%40cam%2F1&x=1") == (
        "/api/streams?src=http://***&x=1"
    )
    assert mask_secrets("src=exec%3Affmpeg+-token+t&mp4=all") == "src=exec:***&mp4=all"
    # A program's arguments go; that there were some stays.
    assert mask_secrets("exec:ffmpeg -headers 'Authorization: Basic x'") == "exec:ffmpeg ***"
    assert mask_secrets("exec:ffmpeg") == "exec:ffmpeg"
    assert mask_secrets("exec: exit status 1") == "exec: exit status 1"


def test_translations_agree() -> None:
    """English and Russian have the same messages, with the same placeholders."""
    folder = Path(__file__).parents[1] / "custom_components" / "speakeasy"
    strings = json.loads((folder / "strings.json").read_text())
    assert json.loads((folder / "translations" / "en.json").read_text()) == strings

    def messages(tree: object, path: str = "") -> dict[str, set[str]]:
        if isinstance(tree, dict):
            return {
                k: v for key, sub in tree.items() for k, v in messages(sub, f"{path}/{key}").items()
            }
        return {path: set(re.findall(r"\{(\w+)\}", str(tree)))}

    russian = json.loads((folder / "translations" / "ru.json").read_text())
    assert messages(russian) == messages(strings)


def test_source_name_keeps_only_the_camera_address() -> None:
    """A URL source is named by scheme, host and port: two cameras differ, no password shows."""
    assert source_name("rtsp://admin:pw@192.168.1.20:554/ch1?pwd=pw") == "rtsp://192.168.1.20:554"
    assert source_name("ffmpeg:rtsp://u:p@[fe80::1]/x#video=copy") == "ffmpeg:rtsp://[fe80::1]"
    assert source_name("http://cam.local/snap.cgi?user=admin&pwd=pw") == "http://cam.local"
    assert source_name("rtsp://u:p@") == "rtsp://***"
    # A password with `#`, `/` or `@` must not pass for the host or the port.
    assert source_name("rtsp://admin:12345#abc@192.168.1.20:554/x") == "rtsp://***"
    assert source_name("rtsp://admin:4321/x@cam/1") == "rtsp://***"
    assert source_name("rtsp://admin:p@ss@cam/1") == "rtsp://***"
    # Sources without `://` keep their tokens after `?` or `#`.
    assert source_name("nest:?client_id=a&client_secret=s") == "nest:"
    assert source_name("ffmpeg:doorbell#video=h264") == "ffmpeg:doorbell"
    # A program's arguments may carry a password or a token: only the program is kept.
    assert source_name("exec:ffmpeg -i rtsp://u:pw@cam/1 -headers token") == "exec:ffmpeg"
    assert source_name("echo:/config/url.sh secret") == "echo:/config/url.sh"
    assert source_name("doorbell_hd") == "doorbell_hd"

"""WebSocket protocol, actions and permissions."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.speakeasy import recorder
from custom_components.speakeasy.const import CONF_MIN_FREE_GB, DOMAIN
from custom_components.speakeasy.recorder import RecordingError, Source
from custom_components.speakeasy.services import START_SCHEMA, STOP_SCHEMA

from .conftest import FakeGo2rtc, part_removed_on_save, remux_rename_fails, wait_for


async def next_event(client: Any) -> dict[str, Any]:
    """The next subscription event, skipping command results."""
    while (msg := await client.receive_json())["type"] != "event":
        pass
    return msg["event"]


async def test_info_after_entry_removed(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_ws_client
) -> None:
    """The entry is gone but the commands stay: `configured` is false, start `not_configured`.

    Without an enabled entry (and no YAML key) since the start Home Assistant never loads
    the integration; the card then gets `unknown_command`, as in the next test.
    """
    assert await hass.config_entries.async_remove(loaded.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/info"})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"] == {
        "version": "0.1.2",
        "protocol": 1,
        "features": ["record"],
        "configured": False,
    }

    await client.send_json_auto_id({"type": f"{DOMAIN}/record/start", "source": {"url": "door"}})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "not_configured"

    await client.send_json_auto_id({"type": f"{DOMAIN}/record/stop", "recording_id": "x"})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "not_configured"

    await client.send_json_auto_id({"type": f"{DOMAIN}/record/subscribe"})
    assert (await client.receive_json())["success"]
    assert await next_event(client) == {"event": "list", "configured": False, "recordings": []}


async def test_subscription_follows_the_entry(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, hass_ws_client
) -> None:
    """One subscription outlasts removing, re-adding, reloading and disabling the entry.

    `list` comes again whenever `configured` changes. An unload turns it false as it
    begins, so the recordings it finishes are listed and then end as usual.
    """
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/subscribe"})
    assert (await client.receive_json())["success"]
    assert await next_event(client) == {"event": "list", "configured": True, "recordings": []}

    recording = await loaded.runtime_data.async_start(Source(url="door"))
    assert (await next_event(client))["event"] == "started"
    assert await hass.config_entries.async_remove(loaded.entry_id)
    event = await next_event(client)
    assert event["event"] == "list" and event["configured"] is False
    assert [(rec["id"], rec["status"]) for rec in event["recordings"]] == [
        (recording.id, "recording")
    ]
    stopping = await next_event(client)
    assert (stopping["event"], stopping["reason"]) == ("stopping", "shutdown")
    assert (await next_event(client))["event"] == "stopped"

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: go2rtc.url}, options={CONF_MIN_FREE_GB: 0}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await next_event(client) == {"event": "list", "configured": True, "recordings": []}
    recording = await entry.runtime_data.async_start(Source(url="door"))
    assert (await next_event(client))["recording"]["id"] == recording.id

    assert await hass.config_entries.async_reload(entry.entry_id)
    assert (await next_event(client))["configured"] is False
    assert [(await next_event(client))["event"] for _ in range(2)] == ["stopping", "stopped"]
    assert await next_event(client) == {"event": "list", "configured": True, "recordings": []}

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    assert await next_event(client) == {"event": "list", "configured": False, "recordings": []}


async def test_unknown_command_error_shape(hass: HomeAssistant, hass_ws_client) -> None:
    """What the card sees when the integration is not loaded.

    Not installed, no enabled entry, or Home Assistant is still starting.
    """
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/info"})
    msg = await client.receive_json()
    assert msg["error"] == {"code": "unknown_command", "message": "Unknown command."}


async def test_ws_flow_for_a_non_admin_user(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    hass_ws_client,
    hass_read_only_access_token: str,
) -> None:
    """Any logged-in user records: subscribe, start, stop, get the file."""
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json_auto_id({"type": f"{DOMAIN}/record/subscribe"})
    subscription = (await client.receive_json())["id"]
    event = await client.receive_json()
    assert event["id"] == subscription
    assert event["event"] == {"event": "list", "configured": True, "recordings": []}

    source = {"url": "door", "server": go2rtc.url}
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/start", "source": source})
    started = await client.receive_json()
    assert started["event"]["event"] == "started"
    assert started["event"]["recording"]["source"] == source
    assert started["event"]["recording"]["status"] == "recording"
    assert started["event"]["recording"]["reason"] is None
    result = await client.receive_json()
    assert result["success"]
    recording_id = result["result"]["id"]
    assert started["event"]["recording"]["id"] == recording_id

    await asyncio.sleep(0.2)
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/stop", "recording_id": recording_id})
    stopping = await client.receive_json()
    assert stopping["event"]["event"] == "stopping"
    assert stopping["event"]["recording"]["status"] == "saving"
    assert stopping["event"]["recording"]["reason"] == stopping["event"]["reason"] == "user"
    stopped = await client.receive_json()
    assert stopped["event"]["event"] == "stopped"
    reply = await client.receive_json()
    assert reply["success"]
    assert reply["result"] == stopped["event"]["result"]
    assert reply["result"]["filename"].startswith("door_")
    assert reply["result"]["media_content_id"].startswith(
        "media-source://media_source/local/speakeasy/door_"
    )
    assert reply["result"]["size"] > 0


async def test_subscribe_lists_running_recordings(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, hass_ws_client
) -> None:
    """A card opened later (or after a reload) sees what is running."""
    recording = await loaded.runtime_data.async_start(Source(url="door"))
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/subscribe"})
    await client.receive_json()
    event = await client.receive_json()
    assert [rec["id"] for rec in event["event"]["recordings"]] == [recording.id]
    await loaded.runtime_data.async_stop_all()


async def test_late_subscriber_sees_why_a_saving_recording_ended(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    hass_ws_client,
    held_finalize: asyncio.Event,
) -> None:
    """A card that subscribes while a file is saving missed `stopping`: `list` says why."""
    go2rtc.mode = "end"
    recording = await loaded.runtime_data.async_start(Source(url="door"))
    await wait_for(lambda: recording.status == "saving")
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/subscribe"})
    assert (await client.receive_json())["success"]
    listed = (await next_event(client))["recordings"]
    held_finalize.set()
    stopped = await next_event(client)
    assert [(rec["id"], rec["status"], rec["reason"]) for rec in listed] == [
        (recording.id, "saving", "stream_ended")
    ]
    assert stopped["event"] == "stopped"
    assert stopped["recording"]["reason"] == stopped["result"]["reason"] == "stream_ended"


async def test_entity_is_lowercased(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, hass_ws_client
) -> None:
    """The schema lowercases an entity id: the key and every echoed source use that form."""
    client = await hass_ws_client(hass)
    with patch.object(recorder, "async_get_stream_source", return_value="rtsp://cam/1"):
        for entity in ("Camera.Door", "camera.door"):
            await client.send_json_auto_id(
                {"type": f"{DOMAIN}/record/start", "source": {"entity": entity}}
            )
            msg = await client.receive_json()
            assert msg["success"]
    (recording,) = loaded.runtime_data.recordings
    assert msg["result"]["id"] == recording.id
    assert recording.as_dict()["source"] == {"entity": "camera.door"}
    assert len(go2rtc.requests) == 1
    await loaded.runtime_data.async_stop_all()


async def test_action_start_returns_the_recording_of_a_camera_without_state(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A camera's state can be gone for a moment (its integration reloads): its recording stays."""
    with patch.object(recorder, "async_get_stream_source", return_value="rtsp://cam/1"):
        recording = await loaded.runtime_data.async_start(Source(entity="camera.door"))
    assert hass.states.get("camera.door") is None
    started = await hass.services.async_call(
        DOMAIN, "record_start", {"entity_id": "camera.door"}, blocking=True, return_response=True
    )
    assert started["id"] == recording.id
    assert len(go2rtc.requests) == 1
    await loaded.runtime_data.async_stop_all()


@pytest.mark.parametrize(
    "body",
    [
        {"entity_id": ["camera.door"]},
        {"url": " "},
        {"url": "door", "record_url": " "},
    ],
)
@pytest.mark.parametrize("schema", [START_SCHEMA, STOP_SCHEMA])
def test_action_schemas_refuse(schema: Any, body: dict[str, Any]) -> None:
    """A list of camera ids and blank addresses fail in the schema, as a 400."""
    with pytest.raises(vol.Invalid):
        schema(body)


def test_source_trims_addresses() -> None:
    """`" door"` and `"door"` are one source, as the card trims what it sends."""
    source = Source(url=" door\t", record_url=" door_hd ")
    assert source.key == Source(url="door").key
    assert (source.url, source.record_url) == ("door", "door_hd")


def test_source_refuses_blank_addresses() -> None:
    """A blank `url` or `record_url` is not a source, whoever builds it."""
    for fields in ({"url": ""}, {"url": "door", "record_url": "\t"}):
        with pytest.raises(RecordingError) as err:
            Source(**fields)
        assert err.value.code == "invalid_source"


async def test_ws_errors(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, hass_ws_client
) -> None:
    """Bad requests get coded errors."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/stop", "recording_id": "nope"})
    assert (await client.receive_json())["error"]["code"] == "not_found"

    await client.send_json_auto_id({"type": f"{DOMAIN}/record/start", "source": {}})
    assert (await client.receive_json())["error"]["code"] == "invalid_source"

    # Blank addresses: the WebSocket schema takes any string, the source refuses them.
    for source in ({"url": "  "}, {"url": "door", "record_url": ""}):
        await client.send_json_auto_id({"type": f"{DOMAIN}/record/start", "source": source})
        assert (await client.receive_json())["error"]["code"] == "invalid_source"
    assert not go2rtc.requests

    # Schema failures are Home Assistant's own code.
    for source in ({"url": "x", "entity": "camera.x"}, {"entity": "not an entity"}):
        await client.send_json_auto_id({"type": f"{DOMAIN}/record/start", "source": source})
        assert (await client.receive_json())["error"]["code"] == "invalid_format"

    go2rtc.mode = "missing"
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/start", "source": {"url": "x"}})
    assert (await client.receive_json())["error"]["code"] == "stream_not_found"


async def test_actions_answer_right_away(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Start answers after the first data; stop answers before the file is saved."""
    started = await hass.services.async_call(
        DOMAIN, "record_start", {"url": "door"}, blocking=True, return_response=True
    )
    recording = loaded.runtime_data.get(started["id"])
    stopped = await hass.services.async_call(
        DOMAIN, "record_stop", {"url": "door"}, blocking=True, return_response=True
    )
    assert stopped == {"id": started["id"]}
    result = await asyncio.wait_for(asyncio.shield(recording.done), 10)
    assert result.reason == "user"

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "record_stop", {"url": "door"}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "not_found"


async def test_action_input_errors_are_not_server_errors(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_client, caplog: pytest.LogCaptureFixture
) -> None:
    """A stop with nothing to stop or a wrong source is a 400 with a text, not a 500."""
    assert await async_setup_component(hass, "api", {})
    client = await hass_client()

    response = await client.post(f"/api/services/{DOMAIN}/record_stop", json={"url": "door"})
    assert response.status == 400
    assert (await response.json())["message"] == (
        "This source has no recording running, ending, being saved or starting"
    )
    for action in ("record_start", "record_stop"):
        for body in (
            {"server": "http://go2rtc:1984"},
            {"url": "door", "server": "go2rtc:1984"},
            {"entity_id": "light.door"},
            {"entity_id": ["camera.door"]},
            {"url": ""},
            {"url": "  "},
        ):
            response = await client.post(f"/api/services/{DOMAIN}/{action}", json=body)
            assert response.status == 400, (action, body)
    response = await client.post(
        f"/api/services/{DOMAIN}/record_start", json={"entity_id": "camera.typo"}
    )
    assert response.status == 400
    assert (await response.json())["message"] == "There is no camera camera.typo"
    # An empty server means the configured one: the schema lets it through.
    response = await client.post(
        f"/api/services/{DOMAIN}/record_stop", json={"url": "door", "server": ""}
    )
    assert response.status == 400
    assert (await response.json())["message"] == (
        "This source has no recording running, ending, being saved or starting"
    )
    # Only the configured go2rtc: another address is refused before anything is sent.
    response = await client.post(
        f"/api/services/{DOMAIN}/record_start",
        json={"url": "door", "server": "http://attacker.example:1984"},
    )
    assert response.status == 400
    assert "configured go2rtc address" in (await response.json())["message"]
    assert "Error during service call" not in caplog.text


@pytest.mark.parametrize(
    ("fault", "code", "kept"), [("removed", "no_data", False), ("rename", "data_kept", True)]
)
async def test_stop_of_a_file_that_was_not_saved(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    hass_ws_client,
    fault: str,
    code: str,
    kept: bool,
) -> None:
    """`failed` and the stop error tell a lost file from one kept for the next start."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/record/subscribe"})
    assert (await client.receive_json())["success"]
    assert (await next_event(client))["event"] == "list"
    recording = await loaded.runtime_data.async_start(Source(url="door"))
    assert (await next_event(client))["event"] == "started"
    with part_removed_on_save() if fault == "removed" else remux_rename_fails():
        await client.send_json_auto_id(
            {"type": f"{DOMAIN}/record/stop", "recording_id": recording.id}
        )
        assert (await client.receive_json())["event"]["event"] == "stopping"
        failed = (await client.receive_json())["event"]
        reply = await client.receive_json()

    assert (failed["event"], failed["reason"], failed["kept"]) == ("failed", "user", kept)
    assert failed["recording"]["id"] == recording.id
    assert reply["error"]["code"] == code
    # A kept part is finished only from the recordings folder set at the next start.
    assert ("recordings folder" in reply["error"]["message"]) is kept
    assert recording.part_path.exists() is kept
    assert list(recording.part_path.parent.glob("*.tmp")) == []

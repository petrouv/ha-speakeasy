"""Recording through the fake go2rtc: start, stop, emergency stops, upkeep."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.speakeasy import recorder
from custom_components.speakeasy.const import CONF_KEEP_DAYS, CONF_MIN_FREE_GB, DOMAIN
from custom_components.speakeasy.go2rtc import parse_g711
from custom_components.speakeasy.recorder import (
    RecordingError,
    RecordingManager,
    Source,
    ffmpeg_command,
)

from .conftest import (
    G711_CONTENT_TYPE,
    OPUS_CONTENT_TYPE,
    TAPO_MEDIAS,
    FakeGo2rtc,
    part_removed_on_save,
    remux_rename_fails,
    wait_for,
)


def probe(path: Path) -> dict:
    """Codecs and container of a file, read by ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def manager(entry: MockConfigEntry) -> RecordingManager:
    return entry.runtime_data


@pytest.fixture
def folder(media_dir: Path) -> Path:
    """The recordings folder of `entry`, created before the entry loads."""
    path = media_dir / "speakeasy"
    path.mkdir()
    return path


@pytest.fixture
def held_writes(hass: HomeAssistant) -> Iterator[list[asyncio.Future[int]]]:
    """Make every chunk its own block whose write is replaced by a future in the list.

    Nothing reaches the file: the test sets each outcome (e.g. `set_exception`).
    """
    writes: list[asyncio.Future[int]] = []
    add_job = hass.async_add_executor_job

    def held(target, *args):
        if getattr(target, "__name__", None) == "write":
            writes.append(hass.loop.create_future())
            return writes[-1]
        return add_job(target, *args)

    with (
        patch.object(recorder, "WRITE_BLOCK", 1),
        patch.object(hass, "async_add_executor_job", held),
    ):
        yield writes


@contextmanager
def check_disk_held() -> Iterator[tuple[asyncio.Event, asyncio.Event]]:
    """Hold every start at its disk check, still in the checks.

    Yields:
        `(entered, release)`: `entered` is set once a start is held; setting `release`
        lets it and every later one go.
    """
    entered, release = asyncio.Event(), asyncio.Event()
    check_disk = RecordingManager._check_disk

    async def held(self, folder):
        entered.set()
        await release.wait()
        return await check_disk(self, folder)

    with patch.object(RecordingManager, "_check_disk", held):
        yield entered, release


async def test_start_stop_makes_regular_mp4(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, media_dir: Path
) -> None:
    """A stopped recording becomes a regular MP4 with AAC, and the events say so."""
    events = async_capture_events(hass, f"{DOMAIN}_recording_finished")
    rec = await manager(loaded).async_start(Source(url="door"))

    assert go2rtc.requests == [{"src": "door", "mp4": "all"}]
    assert rec.part_path.name.startswith("door_") and rec.part_path.name.endswith(".mp4.part")
    assert hass.states.get("binary_sensor.speakeasy_recording").state == "on"

    await asyncio.sleep(0.3)  # let the whole sample arrive
    result = await manager(loaded).async_stop(rec)

    assert result is not None
    assert result.reason == "user" and result.finalized
    assert result.path == media_dir / "speakeasy" / rec.part_path.name.removesuffix(".part")
    assert (
        result.media_content_id == f"media-source://media_source/local/speakeasy/{result.path.name}"
    )
    assert not rec.part_path.exists()
    info = probe(result.path)
    assert {s["codec_name"] for s in info["streams"]} == {"h264", "aac"}
    assert float(info["format"]["duration"]) > 2
    await hass.async_block_till_done()
    assert events[0].data["filename"] == result.path.name
    assert events[0].data["reason"] == "user"
    assert hass.states.get("binary_sensor.speakeasy_recording").state == "off"
    await wait_for(lambda: go2rtc.connections == 0)


async def test_opus_is_kept_as_is(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, samples: dict[str, bytes]
) -> None:
    """Audio is never transcoded: Opus from the device stays Opus in a regular MP4."""
    go2rtc.body, go2rtc.content_type = samples["opus"], OPUS_CONTENT_TYPE
    rec = await manager(loaded).async_start(Source(url="door"))
    await asyncio.sleep(0.3)
    result = await manager(loaded).async_stop(rec)
    assert result.finalized
    assert {s["codec_name"] for s in probe(result.path)["streams"]} == {"h264", "opus"}


def audio_bytes(path: Path, raw_format: str) -> bytes:
    """The audio track of a file as raw samples, copied without decoding."""
    return subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-c",
            "copy",
            "-f",
            raw_format,
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout


async def test_g711_is_restored_in_mov(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, samples: dict[str, bytes]
) -> None:
    """G.711 that go2rtc sent as FLAC ends up as the camera's own A-law bytes, in MOV."""
    go2rtc.body, go2rtc.content_type = samples["g711"], G711_CONTENT_TYPE
    go2rtc.medias = TAPO_MEDIAS
    rec = await manager(loaded).async_start(Source(url="door"))
    await asyncio.sleep(0.3)
    result = await manager(loaded).async_stop(rec)

    assert result.finalized and result.path.suffix == ".mov"
    assert result.path.name == rec.part_path.name.removesuffix(".mp4.part") + ".mov"
    streams = {s["codec_type"]: s for s in probe(result.path)["streams"]}
    assert streams["video"]["codec_name"] == "hevc"
    assert streams["video"]["codec_tag_string"] == "hvc1"
    assert streams["audio"]["codec_name"] == "pcm_alaw"
    assert audio_bytes(result.path, "alaw") == samples["alaw"]


async def test_flac_from_other_audio_stays_in_mp4(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, samples: dict[str, bytes]
) -> None:
    """FLAC whose source is not G.711, or unknown, is copied into MP4 as is."""
    go2rtc.body, go2rtc.content_type = samples["g711"], G711_CONTENT_TYPE
    go2rtc.medias = ["video, recvonly, H265", "audio, recvonly, L16/16000"]
    rec = await manager(loaded).async_start(Source(url="door"))
    await asyncio.sleep(0.3)
    result = await manager(loaded).async_stop(rec)

    assert result.finalized and result.path.suffix == ".mp4"
    streams = {s["codec_type"]: s for s in probe(result.path)["streams"]}
    assert streams["video"]["codec_tag_string"] == "hvc1"
    assert streams["audio"]["codec_name"] == "flac"


async def test_failed_audio_question_still_saves_the_file(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    samples: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected error while asking go2rtc about the sound leaves the FLAC as it came."""
    go2rtc.body, go2rtc.content_type = samples["g711"], G711_CONTENT_TYPE
    with patch.object(recorder, "source_g711", side_effect=RuntimeError("Session is closed")):
        rec = await manager(loaded).async_start(Source(url="door"))
        await asyncio.sleep(0.3)
        result = await asyncio.wait_for(manager(loaded).async_stop(rec), 10)

    assert result.finalized and result.path.suffix == ".mp4"
    assert "Could not ask go2rtc about the sound" in caplog.text


async def test_audio_question_of_a_start_without_data_is_not_left_behind(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    samples: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stream ends before any data: the audio question's error is not left unread."""
    go2rtc.mode, go2rtc.content_type = "empty", G711_CONTENT_TYPE
    with patch.object(recorder, "source_g711", side_effect=RuntimeError("Session is closed")):
        with pytest.raises(RecordingError) as err:
            await manager(loaded).async_start(Source(url="door"))
        await hass.async_block_till_done()
    assert err.value.code == "go2rtc_error"
    # Worded here, so translated as its own message rather than through `detail`.
    assert err.value.translation_key == "go2rtc_no_data"
    del err  # its traceback holds the recording and so the task
    gc.collect()
    await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text


def test_g711_from_stream_info() -> None:
    """Only the audio the camera sends counts; the way back to its speaker does not."""
    assert parse_g711({"producers": [{"medias": TAPO_MEDIAS}]}) == "pcm_alaw"
    assert parse_g711({"producers": [{"medias": ["audio, recvonly, PCMU/8000"]}]}) == "pcm_mulaw"
    assert parse_g711({"producers": [{"medias": ["audio, sendonly, PCMA/8000"]}]}) is None
    assert parse_g711({"producers": [{"medias": ["audio, recvonly, OPUS/48000/2"]}]}) is None
    assert parse_g711({"producers": [{"medias": None}, {"url": "x"}]}) is None
    assert parse_g711(None) is None
    # A broken or future go2rtc: any other shape means no G.711, never an exception.
    assert parse_g711({"producers": 3}) is None
    assert parse_g711({"producers": [{"medias": 3}, "x"]}) is None
    assert parse_g711({"producers": [{"medias": dict.fromkeys(TAPO_MEDIAS)}]}) is None
    # A deep item `str()` cannot print (RecursionError).
    deep: list[object] = []
    for _ in range(100_000):
        deep = [deep]
    assert parse_g711({"producers": [{"medias": [deep]}]}) is None


async def test_second_start_returns_running_recording(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """One recording per source: cards on several devices share it."""
    first = await manager(loaded).async_start(Source(url="door"))
    second = await manager(loaded).async_start(Source(url="door", record_url="door_aac"))
    assert first is second
    assert len(go2rtc.requests) == 1
    other = await manager(loaded).async_start(Source(url="door", server=go2rtc.url))
    assert other is not first
    # Same camera name, most likely in the same second: each gets its own file.
    assert other.part_path != first.part_path
    # The server is compared as the card compares it: trimmed, without trailing slashes.
    same = await manager(loaded).async_start(Source(url="door", server=f" {go2rtc.url}// "))
    assert same is other
    assert manager(loaded).find(Source(url="door", server=f"{go2rtc.url}/")) is other
    assert len(go2rtc.requests) == 2
    await manager(loaded).async_stop_all()


async def test_server_other_than_configured_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A camera's stream source, credentials included, never goes to another go2rtc."""
    with patch.object(
        recorder, "async_get_stream_source", return_value="rtsp://u:p@cam/ch1"
    ) as get_source:
        for server in ("http://attacker.example:1984", "not a url", f"{go2rtc.url}/other"):
            with pytest.raises(RecordingError) as err:
                await manager(loaded).async_start(Source(entity="camera.door", server=server))
            assert err.value.code == "invalid_source"
        get_source.assert_not_awaited()
        assert go2rtc.requests == []
        # The configured address, as the card may write it, is accepted.
        rec = await manager(loaded).async_start(
            Source(entity="camera.door", server=f" {go2rtc.url}/stream.html?src=x ")
        )
    assert rec.server == go2rtc.url
    assert len(go2rtc.requests) == 1
    await manager(loaded).async_stop_all()


async def test_start_after_a_stop_begins_a_new_recording(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    media_dir: Path,
    held_close: tuple[threading.Event, threading.Event],
) -> None:
    """record_stop then record_start, as an automation splitting a file: a new recording.

    The stopped one stays in `_recordings` until its last write ends; the start waits for
    that instead of returning it.
    """
    entered, release = held_close
    first = await hass.services.async_call(
        DOMAIN, "record_start", {"url": "door"}, blocking=True, return_response=True
    )
    await hass.services.async_call(
        DOMAIN, "record_stop", {"url": "door"}, blocking=True, return_response=True
    )
    await hass.async_add_executor_job(entered.wait, 10)
    starting = hass.async_create_task(
        hass.services.async_call(
            DOMAIN, "record_start", {"url": "door"}, blocking=True, return_response=True
        )
    )
    await asyncio.sleep(0.2)
    assert not starting.done()
    release.set()
    async with asyncio.timeout(10):
        second = await starting
    assert second["id"] != first["id"]
    assert [rec.id for rec in manager(loaded).recordings if rec.status == "recording"] == [
        second["id"]
    ]
    await manager(loaded).async_stop_all()
    assert len(go2rtc.requests) == 2
    assert len(list((media_dir / "speakeasy").glob("door_*.mp4"))) == 2


async def test_record_url_and_entity_requests(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """`record_url` replaces the stream; an entity is pulled the way WebRTC Camera does it."""
    rec = await manager(loaded).async_start(Source(url="door", record_url="door_aac"))
    await manager(loaded).async_stop(rec)

    with patch.object(
        recorder, "async_get_stream_source", return_value="rtsp://u:p@cam/ch1"
    ) as get_source:
        rec = await manager(loaded).async_start(Source(entity="camera.door"))
    get_source.assert_awaited_once_with(hass, "camera.door")
    assert rec.part_path.name.startswith("door_")
    await manager(loaded).async_stop(rec)

    assert go2rtc.requests == [
        {"src": "door_aac", "mp4": "all"},
        {"src": "rtsp://u:p@cam/ch1", "mp4": "all", "name": "camera.door"},
    ]


async def test_url_source_never_reaches_file_name_or_event(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A stream URL carries the camera password: it is masked outside the card protocol."""
    events = async_capture_events(hass, f"{DOMAIN}_recording_finished")
    rec = await manager(loaded).async_start(Source(url="rtsp://admin:hunter2@cam/1"))
    assert rec.part_path.name.startswith("camera_")
    assert rec.name == "rtsp://cam"
    await asyncio.sleep(0.1)
    await manager(loaded).async_stop(rec)
    await hass.async_block_till_done()
    assert "hunter2" not in str(events[0].data)
    assert "hunter2" not in str(hass.states.get("binary_sensor.speakeasy_recording").attributes)


async def test_entity_without_stream_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Without a stream source there is nothing go2rtc can record."""
    with (
        patch.object(recorder, "async_get_stream_source", return_value=None),
        pytest.raises(RecordingError) as err,
    ):
        await manager(loaded).async_start(Source(entity="camera.door"))
    assert err.value.code == "no_stream_source"
    assert go2rtc.requests == []


async def test_unknown_stream_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, media_dir: Path
) -> None:
    """go2rtc's 404 reaches the caller; no file is left."""
    go2rtc.mode = "missing"
    with pytest.raises(RecordingError) as err:
        await manager(loaded).async_start(Source(url="nope"))
    assert err.value.code == "stream_not_found"
    assert manager(loaded).recordings == []
    # The file and its folder are created only at the first data.
    assert list(media_dir.iterdir()) == []


async def test_sleeping_camera_is_waited_for(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, quick_retry
) -> None:
    """go2rtc's 500 at the start is retried until the camera wakes up.

    The wake-up is not part of the recording: the start time, the file name and
    the duration count from the first data.
    """
    go2rtc.unavailable = 11
    asked = dt_util.now()
    with patch.object(recorder, "START_RETRY_WINDOW", 5):
        rec = await manager(loaded).async_start(Source(url="door"))
    assert len(go2rtc.requests) == 12
    # Over a second, so the file name would show a request-time stamp.
    assert rec.started_at - asked >= timedelta(seconds=1)
    assert rec.part_path.name == f"door_{rec.started_at:%Y%m%d_%H%M%S}.mp4.part"
    result = await manager(loaded).async_stop(rec)
    assert result is not None and result.finalized
    assert result.duration < (dt_util.now() - asked).total_seconds() - 1


async def test_unreachable_camera_fails_start(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, media_dir: Path, quick_retry
) -> None:
    """A camera that never answers fails with go2rtc's reason, without the password."""
    go2rtc.unavailable = 1000
    with pytest.raises(RecordingError) as err:
        await manager(loaded).async_start(Source(url="door"))
    assert err.value.code == "camera_unavailable"
    detail = err.value.translation_placeholders["detail"]
    assert "i/o timeout" in detail and "secret" not in detail
    assert 5 <= len(go2rtc.requests) <= 12
    assert manager(loaded).recordings == []
    # The file and its folder are created only at the first data.
    assert list(media_dir.iterdir()) == []


async def test_cancelled_start_of_unreachable_camera_logs_nothing(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    quick_retry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A start cancelled while the camera wakes leaves no unhandled error when it fails.

    An automation in `restart` mode cancels its running action this way.
    """
    go2rtc.unavailable = 1000
    # Records of the retries would hold the start's error, and through it the recording:
    # an unretrieved future error is logged only when the future is collected.
    caplog.set_level(logging.ERROR, logger=recorder.__name__)
    source_manager = manager(loaded)
    start = hass.async_create_task(source_manager.async_start(Source(url="door")))
    await wait_for(lambda: go2rtc.requests)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    await wait_for(lambda: source_manager.find(Source(url="door")) is None)
    await hass.async_block_till_done()
    gc.collect()
    assert "shielded future" not in caplog.text
    assert "never retrieved" not in caplog.text


async def test_silent_stream_fails_start(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, short_stall
) -> None:
    """No first data within the stall timeout: the start fails."""
    go2rtc.mode = "silent"
    with pytest.raises(RecordingError) as err:
        await manager(loaded).async_start(Source(url="door"))
    assert err.value.code == "go2rtc_error"


async def test_unwritable_folder_fails_start(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A folder that cannot be written is reported as such, not as a go2rtc failure."""
    denied = PermissionError(13, "Permission denied", "/media/speakeasy")
    with (
        patch.object(recorder, "_create_part", side_effect=denied),
        pytest.raises(RecordingError) as err,
    ):
        await manager(loaded).async_start(Source(url="door"))
    assert err.value.code == "folder_not_writable"
    assert "Permission denied" in err.value.translation_placeholders["detail"]
    assert manager(loaded).recordings == []


async def test_start_racing_an_unload_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A start still checking the camera when the entry goes away adds no recording.

    The unload's stop-all has already run by then; a recording added now would
    keep running unseen and unstoppable.
    """
    asked, release = asyncio.Event(), asyncio.Event()
    source_manager = manager(loaded)

    async def slow_stream_source(_hass: HomeAssistant, _entity: str) -> str:
        asked.set()
        await release.wait()
        return "rtsp://door"

    with patch.object(recorder, "async_get_stream_source", slow_stream_source):
        start = hass.async_create_task(source_manager.async_start(Source(entity="camera.door")))
        await asked.wait()
        await hass.config_entries.async_remove(loaded.entry_id)
        release.set()
        with pytest.raises(RecordingError) as err:
            await start
    assert err.value.code == "not_configured"
    assert source_manager.recordings == []
    assert source_manager._recordings == {}
    assert go2rtc.requests == []


async def test_stop_while_creating_the_file_leaves_nothing(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, media_dir: Path
) -> None:
    """A stop that lands while the `.part` is being created closes and removes it."""
    entered, release = threading.Event(), threading.Event()
    created = []
    create_part = recorder._create_part

    def slow_create_part(folder: Path, stem: str):
        entered.set()
        release.wait(10)
        created.append(create_part(folder, stem))
        return created[-1]

    with patch.object(recorder, "_create_part", slow_create_part):
        start = hass.async_create_task(manager(loaded).async_start(Source(url="door")))
        await hass.async_add_executor_job(entered.wait, 10)
        manager(loaded).async_request_stop(manager(loaded).find(Source(url="door")))
        with pytest.raises(RecordingError) as err:
            await start
        assert err.value.code == "stopped"
        release.set()
        await wait_for(lambda: created)
        ((path, file),) = created
        # the cleanup is a background executor job that async_block_till_done does not wait for
        await wait_for(lambda: file.closed and not path.exists())


async def test_unload_waits_for_the_removal_of_a_file_being_created(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, media_dir: Path
) -> None:
    """The next setup must not find the `.part` of a start the unload stopped as a leftover."""
    entered, release = threading.Event(), threading.Event()
    created = []
    create_part = recorder._create_part

    def slow_create_part(folder: Path, stem: str):
        entered.set()
        release.wait(10)
        created.append(create_part(folder, stem))
        return created[-1]

    with patch.object(recorder, "_create_part", slow_create_part):
        start = hass.async_create_task(manager(loaded).async_start(Source(url="door")))
        await hass.async_add_executor_job(entered.wait, 10)
        stop_all = hass.async_create_task(manager(loaded).async_stop_all())
        with pytest.raises(RecordingError):
            await start
        await asyncio.sleep(0.2)
        assert not stop_all.done()
        release.set()
        await asyncio.wait_for(stop_all, 10)
        ((path, file),) = created
        assert file.closed and not path.exists()


async def test_stop_during_a_queued_write_keeps_the_block(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, saved_parts: list[bytes]
) -> None:
    """A stop that lands while a block waits for an executor thread still writes it."""
    pool = ThreadPoolExecutor(max_workers=1)
    busy = threading.Event()
    pool.submit(busy.wait, 10)  # the block's write queues behind this, not started
    blocks: list[bytes] = []
    add_job = hass.async_add_executor_job

    def queued_writes(target, *args):
        if getattr(target, "__name__", None) == "write":
            blocks.append(args[0])
            return hass.loop.run_in_executor(pool, target, *args)
        return add_job(target, *args)

    try:
        with (
            patch.object(recorder, "WRITE_BLOCK", 1),
            patch.object(hass, "async_add_executor_job", queued_writes),
        ):
            rec = await manager(loaded).async_start(Source(url="door"))
            await wait_for(lambda: blocks)
            manager(loaded).async_request_stop(rec)
            await asyncio.sleep(0.1)  # the cancel reaches the waiting write
            busy.set()
            async with asyncio.timeout(10):
                await asyncio.shield(rec.done)
    finally:
        busy.set()
        pool.shutdown()
    assert saved_parts == [b"".join(blocks)]


@pytest.mark.parametrize("same_turn", [False, True], ids=["running", "same_turn"])
async def test_write_failing_after_a_stop_ends_with_error(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    caplog: pytest.LogCaptureFixture,
    saved_parts: list[bytes],
    held_writes: list[asyncio.Future[int]],
    same_turn: bool,
) -> None:
    """A block whose write fails around a stop is not dropped unseen: reason `error`.

    `same_turn`: the write fails in the loop turn the stop lands in, before the
    recording sees it; otherwise it fails while the stop's cleanup waits for it.
    """
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[tuple[str, str]] = []
    manager(loaded).async_subscribe(
        lambda event, payload: events.append((event, payload["reason"]))
    )
    await wait_for(lambda: held_writes)
    manager(loaded).async_request_stop(rec)
    if not same_turn:
        await asyncio.sleep(0.1)  # the cancel reaches the waiting write
    held_writes[0].set_exception(OSError(28, "No space left on device"))
    async with asyncio.timeout(10):
        result = await asyncio.shield(rec.done)
    # The failed block was the only one: nothing was saved, and the empty part is gone.
    assert len(held_writes) == 1
    assert result is None and saved_parts == [b""]
    assert events == [("stopping", "error"), ("failed", "error")]
    assert not rec.part_path.exists()
    assert "Recording door failed" in caplog.text
    assert "No space left on device" in caplog.text


async def test_stop_while_closing_after_a_write_error_keeps_the_error(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    caplog: pytest.LogCaptureFixture,
    held_close: tuple[threading.Event, threading.Event],
    held_writes: list[asyncio.Future[int]],
) -> None:
    """A write fails with no stop; a stop while the file then closes changes nothing."""
    entered, release = held_close
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[tuple[str, str]] = []
    manager(loaded).async_subscribe(
        lambda event, payload: events.append((event, payload["reason"]))
    )
    await wait_for(lambda: held_writes)
    held_writes[0].set_exception(OSError(28, "No space left on device"))
    await hass.async_add_executor_job(entered.wait, 10)
    manager(loaded).async_request_stop(rec)
    await asyncio.sleep(0.1)
    release.set()
    async with asyncio.timeout(10):
        result = await asyncio.shield(rec.done)
    assert result is None
    assert events == [("stopping", "error"), ("failed", "error")]
    assert "Recording door failed" in caplog.text


@pytest.mark.parametrize("stop", ["stop", "stop_all", "shutdown"])
async def test_stop_during_the_last_write_of_an_ended_recording_changes_nothing(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    held_close: tuple[threading.Event, threading.Event],
    saved_parts: list[bytes],
    stop: str,
) -> None:
    """The stream ended: a stop while its last block is written neither cuts nor renames it.

    `stop` is `async_stop` (record_stop, the card's Stop), `record_stop_all`, or the
    unload's or Home Assistant's `async_stop_all`.
    """
    entered, release = held_close
    go2rtc.mode = "end"
    rec = await manager(loaded).async_start(Source(url="door"))
    await hass.async_add_executor_job(entered.wait, 10)
    if stop == "stop":
        stopping = hass.async_create_task(manager(loaded).async_stop(rec))
    elif stop == "stop_all":
        # Already ending, not stopped by this call; listed, as its outcome is still to come.
        assert manager(loaded).async_request_stop_all() == [rec.id]
        stopping = asyncio.shield(rec.done)
    else:
        stopping = hass.async_create_task(manager(loaded).async_stop_all())
    await asyncio.sleep(0.1)
    assert not stopping.done()
    release.set()
    async with asyncio.timeout(10):
        await stopping
        result = await asyncio.shield(rec.done)
    assert result.reason == "stream_ended" and result.finalized
    assert saved_parts == [go2rtc.body]


async def test_start_during_the_last_write_of_an_ended_recording_begins_a_new_one(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    media_dir: Path,
    held_close: tuple[threading.Event, threading.Event],
) -> None:
    """A recording whose stream ended is not running: a start waits for it, then begins anew."""
    entered, release = held_close
    go2rtc.mode = "end"
    first = await manager(loaded).async_start(Source(url="door"))
    await hass.async_add_executor_job(entered.wait, 10)
    starting = hass.async_create_task(manager(loaded).async_start(Source(url="door")))
    await asyncio.sleep(0.2)
    assert not starting.done()
    release.set()
    async with asyncio.timeout(10):
        second = await starting
        results = [await asyncio.shield(rec.done) for rec in (first, second)]
    assert second is not first
    assert [result.reason for result in results] == ["stream_ended", "stream_ended"]
    assert len(go2rtc.requests) == 2
    assert len(list((media_dir / "speakeasy").glob("door_*.mp4"))) == 2


async def test_saving_recording_is_found_until_its_events(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Saved but not yet announced as such: a stop still finds it, and it is still listed."""
    entered, release = threading.Event(), threading.Event()
    file_size = recorder._file_size

    def held_size(path: Path) -> int:
        if path.suffix != ".part":
            entered.set()
            release.wait(10)
        return file_size(path)

    rec = await manager(loaded).async_start(Source(url="door"))
    with patch.object(recorder, "_file_size", held_size):
        try:
            stopping = hass.async_create_task(manager(loaded).async_stop(rec))
            await hass.async_add_executor_job(entered.wait, 10)
            assert manager(loaded).get(rec.id) is rec
            assert manager(loaded).recordings == [rec]
        finally:
            release.set()
        async with asyncio.timeout(10):
            result = await stopping
    assert result.size > 0
    assert manager(loaded).get(rec.id) is None


async def test_unreadable_size_of_a_saved_file_still_ends_the_recording(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The file is saved but its size cannot be read: the stop, the events and the sensor end."""
    file_size = recorder._file_size

    def denied(path: Path) -> int:
        if path.suffix != ".part":
            raise PermissionError(13, "Permission denied")
        return file_size(path)

    finished = async_capture_events(hass, f"{DOMAIN}_recording_finished")
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[str] = []
    manager(loaded).async_subscribe(lambda event, _payload: events.append(event))
    with patch.object(recorder, "_file_size", denied):
        async with asyncio.timeout(10):
            result = await manager(loaded).async_stop(rec)
    assert result.finalized and result.path.exists()
    assert result.size == 0
    assert events == ["stopping", "stopped"]
    await hass.async_block_till_done()
    assert len(finished) == 1
    assert manager(loaded).recordings == []
    assert "Permission denied" in caplog.text


async def test_stalled_stream_is_finished(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, short_stall
) -> None:
    """Data stops coming: the recording ends with what it has."""
    events = async_capture_events(hass, f"{DOMAIN}_recording_finished")
    rec = await manager(loaded).async_start(Source(url="door"))
    result = await asyncio.wait_for(asyncio.shield(rec.done), 10)
    assert result.reason == "stalled" and result.finalized
    # The silent second before the stall was noticed is not part of the recording.
    assert result.duration < 1
    await hass.async_block_till_done()
    assert events[0].data["reason"] == "stalled"
    assert events[0].data["duration"] < 1


async def test_stream_drop_is_finished(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """go2rtc closes the stream: the recording ends with what it has."""
    go2rtc.mode = "end"
    rec = await manager(loaded).async_start(Source(url="door"))
    result = await asyncio.wait_for(asyncio.shield(rec.done), 10)
    assert result.reason == "stream_ended" and result.size > 0


async def test_low_disk_refuses_start(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Below the free space threshold a recording does not start."""
    hass.config_entries.async_update_entry(loaded, options={CONF_MIN_FREE_GB: 2})
    with (
        patch.object(recorder, "_free_bytes", return_value=1024**3),
        pytest.raises(RecordingError) as err,
    ):
        await manager(loaded).async_start(Source(url="door"))
    assert err.value.code == "disk_low"
    assert err.value.translation_placeholders["free"] == "1.0"
    assert go2rtc.requests == []


async def test_low_disk_stops_running_recording(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Free space runs out during a recording: it is finished properly."""
    go2rtc.mode = "end"  # data keeps arriving until the disk check fires
    hass.config_entries.async_update_entry(loaded, options={CONF_MIN_FREE_GB: 2})
    free = iter([10 * 1024**3])
    with (
        patch.object(recorder, "DISK_CHECK_INTERVAL", 0),
        patch.object(recorder, "_free_bytes", side_effect=lambda _p: next(free, 0)),
    ):
        rec = await manager(loaded).async_start(Source(url="door"))
        result = await asyncio.wait_for(asyncio.shield(rec.done), 10)
    assert result.reason == "disk_low" and result.finalized


async def test_hourly_reminder(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Every hour of recording raises a notification; stopping dismisses it."""
    rec = await manager(loaded).async_start(Source(url="door"))
    with (
        patch.object(recorder.persistent_notification, "async_create") as create,
        patch.object(recorder.persistent_notification, "async_dismiss") as dismiss,
    ):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, seconds=1))
        await hass.async_block_till_done()
        assert "1 h" in create.call_args.args[1]
        assert create.call_args.kwargs["title"] == "Speakeasy recording is still running"
        # The text follows the language Home Assistant has at the time of the reminder.
        hass.config.language = "ru"
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2, seconds=2))
        # Russian texts are read from disk in the executor on first use.
        await wait_for(lambda: create.call_count >= 2, 5)
        assert "идёт уже 2 ч" in create.call_args.args[1]
        assert create.call_args.kwargs["title"] == "Speakeasy всё ещё пишет"
        notification_id = create.call_args.kwargs["notification_id"]
        await manager(loaded).async_stop(rec)
    dismiss.assert_called_once_with(hass, notification_id)


async def test_sensor_is_on_until_the_file_is_saved(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, held_finalize: asyncio.Event
) -> None:
    """A stopped recording keeps the sensor on, listed as `saving`, until its file is saved."""
    rec = await manager(loaded).async_start(Source(url="door"))
    manager(loaded).async_request_stop(rec)
    await wait_for(lambda: rec.status == "saving")
    state = hass.states.get("binary_sensor.speakeasy_recording")
    assert state.state == "on"
    assert [(r["id"], r["status"]) for r in state.attributes["recordings"]] == [(rec.id, "saving")]
    held_finalize.set()
    async with asyncio.timeout(10):
        await asyncio.shield(rec.done)
    state = hass.states.get("binary_sensor.speakeasy_recording")
    assert (state.state, state.attributes["recordings"]) == ("off", [])


async def test_recordings_with_the_same_start_keep_their_order(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, held_finalize: asyncio.Event
) -> None:
    """Oldest first holds when two recordings started at the same moment, saving or not."""
    first = await manager(loaded).async_start(Source(url="door"))
    second = await manager(loaded).async_start(Source(url="yard"))
    second.started_at = first.started_at
    manager(loaded).async_request_stop(first)
    await wait_for(lambda: first.status == "saving")
    assert [rec.id for rec in manager(loaded).recordings] == [first.id, second.id]
    response = await hass.services.async_call(
        DOMAIN, "record_stop_all", blocking=True, return_response=True
    )
    assert response["ids"] == [first.id, second.id]
    held_finalize.set()
    await manager(loaded).async_stop_all()


async def test_stop_all(hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc) -> None:
    """The action stops every recording and answers right away."""
    first = await manager(loaded).async_start(Source(url="door"))
    second = await manager(loaded).async_start(Source(url="yard"))
    response = await hass.services.async_call(
        DOMAIN, "record_stop_all", blocking=True, return_response=True
    )
    assert response["ids"] == [first.id, second.id]
    results = await asyncio.gather(asyncio.shield(first.done), asyncio.shield(second.done))
    assert all(result.finalized for result in results)


async def test_stop_all_lists_every_recording_still_to_end(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, held_finalize: asyncio.Event
) -> None:
    """`ids` are every recording whose outcome is still to come, whoever stopped it.

    Saving, stopped and closing its file, ended by itself and closing its file, running;
    a start still waiting for data is stopped too, but never got an id.
    """
    entered = {"yard": threading.Event(), "porch": threading.Event()}
    release = threading.Event()
    write_and_close = recorder._write_and_close

    def held_close(file, data: bytes) -> None:
        for name, event in entered.items():
            if Path(file.name).name.startswith(name):
                event.set()
                release.wait(10)
        write_and_close(file, data)

    rm = manager(loaded)
    with patch.object(recorder, "_write_and_close", held_close):
        try:
            saving = await rm.async_start(Source(url="door"))
            rm.async_request_stop(saving)
            await wait_for(lambda: saving.status == "saving")
            stopped = await rm.async_start(Source(url="yard"))
            rm.async_request_stop(stopped)
            await hass.async_add_executor_job(entered["yard"].wait, 10)
            go2rtc.mode = "end"
            ended = await rm.async_start(Source(url="porch"))
            await hass.async_add_executor_job(entered["porch"].wait, 10)
            go2rtc.mode = "hold"
            running = await rm.async_start(Source(url="gate"))
            go2rtc.mode = "silent"
            start = hass.async_create_task(rm.async_start(Source(url="attic")))
            await wait_for(lambda: len(go2rtc.requests) == 5)

            response = await hass.services.async_call(
                DOMAIN, "record_stop_all", blocking=True, return_response=True
            )
        finally:
            release.set()
        held_finalize.set()
        recordings = (saving, stopped, ended, running)
        async with asyncio.timeout(10):
            results = [await asyncio.shield(rec.done) for rec in recordings]
    assert response["ids"] == [rec.id for rec in recordings]
    assert [result.reason for result in results] == ["user", "user", "stream_ended", "user"]
    with pytest.raises(RecordingError) as err:
        async with asyncio.timeout(5):
            await start
    assert err.value.code == "stopped"
    assert rm.recordings == []


@pytest.mark.parametrize("ending", ["stopped", "by_itself"])
async def test_stop_action_replies_until_the_outcome(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    held_close: tuple[threading.Event, threading.Event],
    held_finalize: asyncio.Event,
    ending: str,
) -> None:
    """record_stop replies the source's recording while it ends and saves, whoever stopped it.

    A start of the source waiting for the ending recording to close its file is stopped too.
    `not_found` only once the outcome came.
    """
    entered, release = held_close
    rm = manager(loaded)

    async def stop_action() -> dict:
        return await hass.services.async_call(
            DOMAIN, "record_stop", {"url": "door"}, blocking=True, return_response=True
        )

    if ending == "by_itself":
        go2rtc.mode = "end"
    rec = await rm.async_start(Source(url="door"))
    if ending == "stopped":
        assert await stop_action() == {"id": rec.id}
    await hass.async_add_executor_job(entered.wait, 10)
    start = hass.async_create_task(rm.async_start(Source(url="door")))
    await wait_for(lambda: rm._checks)
    assert await stop_action() == {"id": rec.id}
    release.set()
    with pytest.raises(RecordingError) as err:
        async with asyncio.timeout(5):
            await start
    assert err.value.code == "stopped"
    await wait_for(lambda: rec.status == "saving")
    assert await stop_action() == {"id": rec.id}
    held_finalize.set()
    async with asyncio.timeout(10):
        result = await asyncio.shield(rec.done)
    assert result.reason == ("user" if ending == "stopped" else "stream_ended")
    with pytest.raises(ServiceValidationError) as err:
        await stop_action()
    assert err.value.translation_key == "not_found"
    assert len(go2rtc.requests) == 1


async def test_stop_action_on_start_without_data(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Stopping a start still waiting for data replies `id` null: it never got one."""
    go2rtc.mode = "silent"
    start = hass.async_create_task(manager(loaded).async_start(Source(url="gate")))
    await wait_for(lambda: len(go2rtc.requests) == 1)

    response = await hass.services.async_call(
        DOMAIN, "record_stop", {"url": "gate"}, blocking=True, return_response=True
    )
    assert response == {"id": None}
    with pytest.raises(RecordingError) as err:
        async with asyncio.timeout(5):
            await start
    assert err.value.code == "stopped"


@pytest.mark.parametrize("where", ["checks", "recordings"])
async def test_stop_action_on_start_without_data_beside_saving(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    held_finalize: asyncio.Event,
    where: str,
) -> None:
    """A start without data is skipped in the reply wherever it is: the saving one is replied."""
    rm = manager(loaded)
    saving = await rm.async_start(Source(url="door"))
    rm.async_request_stop(saving)
    await wait_for(lambda: saving.status == "saving")
    go2rtc.mode = "silent"
    with check_disk_held() as (entered, release):
        if where == "recordings":
            release.set()
        start = hass.async_create_task(rm.async_start(Source(url="door")))
        if where == "checks":
            await entered.wait()
        else:
            await wait_for(lambda: len(go2rtc.requests) == 2)
        response = await hass.services.async_call(
            DOMAIN, "record_stop", {"url": "door"}, blocking=True, return_response=True
        )
        release.set()
        with pytest.raises(RecordingError) as err:
            async with asyncio.timeout(5):
                await start
    assert response == {"id": saving.id}
    assert err.value.code == "stopped"
    held_finalize.set()
    async with asyncio.timeout(10):
        result = await asyncio.shield(saving.done)
    assert result.reason == "user"


@pytest.mark.parametrize(
    ("service", "data", "reply"),
    [("record_stop", {"url": "gate"}, {"id": None}), ("record_stop_all", {}, {"ids": []})],
)
async def test_stop_actions_on_start_in_checks(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    service: str,
    data: dict,
    reply: dict,
) -> None:
    """A start still checking the disk is not in `_recordings` yet, and is stopped all the same."""
    with check_disk_held() as (entered, release):
        start = hass.async_create_task(manager(loaded).async_start(Source(url="gate")))
        await entered.wait()
        response = await hass.services.async_call(
            DOMAIN, service, data, blocking=True, return_response=True
        )
        release.set()
        with pytest.raises(RecordingError) as err:
            async with asyncio.timeout(5):
                await start
    assert response == reply
    assert err.value.code == "stopped"
    assert go2rtc.requests == []


@pytest.mark.parametrize(
    ("service", "data"), [("record_stop", {"url": "gate"}), ("record_stop_all", {})]
)
async def test_stop_actions_on_starts_leaving_checks_out_of_order(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc, service: str, data: dict
) -> None:
    """Two starts of one source in the checks; the later leaves first, then a stop comes."""
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    check_disk = RecordingManager._check_disk
    calls = 0

    async def held_check_disk(self, folder):
        nonlocal calls
        n, calls = calls, calls + 1
        entered[n].set()
        await release[n].wait()
        return await check_disk(self, folder)

    rm = manager(loaded)
    with patch.object(RecordingManager, "_check_disk", held_check_disk):
        first = hass.async_create_task(rm.async_start(Source(url="gate")))
        await entered[0].wait()
        second = hass.async_create_task(rm.async_start(Source(url="gate")))
        await entered[1].wait()
        release[1].set()
        async with asyncio.timeout(5):
            rec = await second
        await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)
        release[0].set()
        with pytest.raises(RecordingError) as err:
            async with asyncio.timeout(5):
                await first
    assert err.value.code == "stopped"
    assert rm._checks == []
    async with asyncio.timeout(10):
        await asyncio.shield(rec.done)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "record_stop", {"url": "gate"}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "not_found"


async def test_start_during_home_assistant_stop_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A start still checking when the shutdown job ran, or asked after it, records nothing.

    The shutdown job has saved the recordings by then; one added later would be cut off
    when background tasks are cancelled, leaving a `.part`.
    """
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_check_disk(self, folder):
        entered.set()
        await release.wait()

    @callback
    def stopping(_event: Event) -> None:
        # Stage 2: the shutdown jobs are done, background tasks are being cancelled.
        release.set()

    rm = manager(loaded)
    with patch.object(RecordingManager, "_check_disk", held_check_disk):
        start = hass.async_create_task(rm.async_start(Source(url="gate")))
        await entered.wait()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping)
        await hass.async_stop()
        with pytest.raises(RecordingError) as err:
            async with asyncio.timeout(5):
                await start
        assert err.value.code == "stopped"
        with pytest.raises(RecordingError) as err:
            await rm.async_start(Source(url="yard"))
        assert err.value.code == "stopped"
    assert rm._recordings == {}
    assert go2rtc.requests == []


@pytest.mark.parametrize(("close", "code"), [("stop_all", "stopped"), ("unload", "not_configured")])
async def test_start_of_a_recording_being_closed_is_refused(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    go2rtc: FakeGo2rtc,
    held_close: tuple[threading.Event, threading.Event],
    close: str,
    code: str,
) -> None:
    """A start while the stop-all waits for a recording's last write does not get that recording.

    The recording stays in `_recordings` until its stream task ends; the start that finds it
    there must still fail, as any start after the stop-all does.
    """
    entered, release = held_close
    rm = manager(loaded)
    rec = await rm.async_start(Source(url="door"))
    closing = hass.async_create_task(
        rm.async_stop_all()
        if close == "stop_all"
        else hass.config_entries.async_unload(loaded.entry_id)
    )
    await hass.async_add_executor_job(entered.wait, 10)
    assert rm.find(Source(url="door")) is rec
    with pytest.raises(RecordingError) as err:
        await rm.async_start(Source(url="door"))
    release.set()
    async with asyncio.timeout(10):
        await closing
    assert err.value.code == code
    assert rec.done.result().reason == "shutdown"
    assert len(go2rtc.requests) == 1


async def test_unload_finishes_recordings(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Unloading (and so a reload) waits for the files."""
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[str] = []
    manager(loaded).async_subscribe(lambda event, _payload: events.append(event))
    await asyncio.sleep(0.2)
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert events == ["stopping", "stopped"]
    result = rec.done.result()
    assert result.reason == "shutdown" and result.finalized


async def test_home_assistant_stop_finishes_recordings(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """The shutdown job finishes recordings before background tasks are cancelled."""
    rec = await manager(loaded).async_start(Source(url="door"))
    await asyncio.sleep(0.2)
    await hass.async_stop()
    result = rec.done.result()
    assert result.reason == "shutdown" and result.finalized
    assert result.path.exists()


async def test_leftover_part_is_finished_at_start(
    hass: HomeAssistant, entry: MockConfigEntry, samples: dict[str, bytes], folder: Path
) -> None:
    """A `.part` left by a crash becomes a regular MP4 once Home Assistant has started."""
    (folder / "door_20261110_120000.mp4.part").write_bytes(samples["opus"])
    (folder / "empty_20261110_120000.mp4.part").write_bytes(b"")
    events = async_capture_events(hass, f"{DOMAIN}_recording_finished")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sorted(path.name for path in folder.iterdir()) == ["door_20261110_120000.mp4"]
    assert {s["codec_name"] for s in probe(folder / "door_20261110_120000.mp4")["streams"]} == {
        "h264",
        "opus",
    }
    assert events[0].data["filename"] == "door_20261110_120000.mp4"


async def test_leftover_upkeep_keeps_part_of_new_recording(
    hass: HomeAssistant, entry: MockConfigEntry, samples: dict[str, bytes], folder: Path
) -> None:
    """A recording whose first data arrives while the leftovers are finished keeps its part."""
    leftover = folder / "yard_20261110_120000.mp4.part"
    leftover.write_bytes(samples["opus"])
    started = False
    list_parts = recorder._list_parts

    def slow_list_parts(path: Path) -> list[Path]:
        # Once Home Assistant has started, a listing would see the new, still empty part.
        deadline = time.monotonic() + 5
        while started and not list(folder.glob("door_*.part")):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        return list_parts(path)

    hass.set_state(CoreState.not_running)
    with patch.object(recorder, "_list_parts", slow_list_parts):
        assert await hass.config_entries.async_setup(entry.entry_id)
        start = hass.async_create_task(manager(entry).async_start(Source(url="door")))
        started = True
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        rec = await start
        await wait_for(lambda: not leftover.exists())

    assert rec.part_path.exists()
    result = await manager(entry).async_stop(rec)
    assert result is not None and result.finalized


async def test_broken_part_is_kept_raw(
    hass: HomeAssistant, entry: MockConfigEntry, folder: Path
) -> None:
    """When ffmpeg fails, the data is kept under the final name."""
    (folder / "door_20261110_120000.mp4.part").write_bytes(b"not a video")
    events = async_capture_events(hass, f"{DOMAIN}_recording_finished")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert (folder / "door_20261110_120000.mp4").read_bytes() == b"not a video"
    assert not (folder / "door_20261110_120000.mp4.tmp").exists()
    assert events[0].data["finalized"] is False
    assert events[0].data["error"]


async def test_failed_rename_after_remux_leaves_no_temporary(
    hass: HomeAssistant, entry: MockConfigEntry, samples: dict[str, bytes], folder: Path
) -> None:
    """ffmpeg succeeded but its output cannot take the final name: only the part stays."""
    part = folder / "door_20261110_120000.mp4.part"
    part.write_bytes(samples["opus"])
    with remux_rename_fails():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert sorted(path.name for path in folder.iterdir()) == [part.name]


class _MissingFfmpeg:
    binary = "/nonexistent/ffmpeg"


async def test_missing_ffmpeg_keeps_the_raw_file(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """Without the ffmpeg binary a recording still ends `stopped`, with its raw file."""
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[str] = []
    manager(loaded).async_subscribe(lambda event, _payload: events.append(event))
    with patch.object(recorder, "get_ffmpeg_manager", lambda _hass: _MissingFfmpeg()):
        async with asyncio.timeout(10):
            result = await manager(loaded).async_stop(rec)

    assert result is not None and not result.finalized and result.error
    assert result.path.exists() and not rec.part_path.exists()
    assert events == ["stopping", "stopped"]
    assert hass.states.get("binary_sensor.speakeasy_recording").state == "off"


async def test_vanished_part_ends_failed(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A part removed while it is being saved ends the recording `failed`, not hung."""
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[str] = []
    payloads: list[dict] = []

    def listener(event: str, payload: dict) -> None:
        events.append(event)
        payloads.append(payload)

    manager(loaded).async_subscribe(listener)
    with part_removed_on_save():
        async with asyncio.timeout(10):
            result = await manager(loaded).async_stop(rec)

    assert result is None
    assert events == ["stopping", "failed"]
    assert payloads[-1]["kept"] is False
    assert hass.states.get("binary_sensor.speakeasy_recording").state == "off"


async def test_part_vanished_during_remux_keeps_file(
    hass: HomeAssistant, loaded: MockConfigEntry, go2rtc: FakeGo2rtc
) -> None:
    """A part removed after ffmpeg read it still ends `stopped` with the saved file."""
    rec = await manager(loaded).async_start(Source(url="door"))
    events: list[str] = []
    manager(loaded).async_subscribe(lambda event, _payload: events.append(event))
    spawn = asyncio.create_subprocess_exec

    async def spawn_then_remove(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        communicate = process.communicate

        async def communicate_then_remove():
            output = await communicate()
            rec.part_path.unlink()
            return output

        process.communicate = communicate_then_remove
        return process

    with patch.object(recorder.asyncio, "create_subprocess_exec", spawn_then_remove):
        async with asyncio.timeout(10):
            result = await manager(loaded).async_stop(rec)

    assert result is not None and result.finalized and result.path.exists()
    assert events == ["stopping", "stopped"]


async def test_leftover_part_error_does_not_stop_retention(
    hass: HomeAssistant, entry: MockConfigEntry, samples: dict[str, bytes], folder: Path
) -> None:
    """A leftover part that cannot be finished is skipped; the others and retention still run."""
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_KEEP_DAYS: 7})
    (folder / "a_20261110_120000.mp4.part").write_bytes(samples["opus"])
    (folder / "b_20261110_120000.mp4.part").write_bytes(samples["opus"])
    old = folder / "old.mp4"
    old.write_bytes(b"x")
    eight_days_ago = time.time() - 8 * 86400
    os.utime(old, (eight_days_ago, eight_days_ago))
    is_hevc = recorder._is_hevc

    def broken_a(part: Path) -> bool:
        if part.name.startswith("a_"):
            raise PermissionError(13, "Permission denied", str(part))
        return is_hevc(part)

    with patch.object(recorder, "_is_hevc", broken_a):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert sorted(path.name for path in folder.iterdir()) == [
        "a_20261110_120000.mp4.part",
        "b_20261110_120000.mp4",
    ]


async def test_retention_deletes_old_recordings(
    hass: HomeAssistant, entry: MockConfigEntry, folder: Path
) -> None:
    """Recordings older than the retention are deleted; others and `.part` stay."""
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_KEEP_DAYS: 7})
    old, old_mov = folder / "old.mp4", folder / "old.mov"
    new, other = folder / "new.mp4", folder / "note.txt"
    for path in (old, old_mov, new, other):
        path.write_bytes(b"x")
    eight_days_ago = time.time() - 8 * 86400
    for path in (old, old_mov, other):
        os.utime(path, (eight_days_ago, eight_days_ago))

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sorted(path.name for path in folder.iterdir()) == ["new.mp4", "note.txt"]


def test_ffmpeg_command_only_remuxes() -> None:
    """Every track is copied; the index goes to the front."""
    command = ffmpeg_command("ffmpeg", Path("a.mp4.part"), Path("a.mp4.tmp"))
    assert command[command.index("-c") + 1] == "copy"
    assert "-c:a" not in command and "-c:v" not in command and "-tag:v" not in command
    assert "+faststart" in command and "0:a?" in command
    assert command[command.index("-f") + 1] == "mp4"


def test_ffmpeg_command_for_hevc_and_g711() -> None:
    """H.265 gets the Apple tag; G.711 is encoded back to its own codec, in MOV."""
    command = ffmpeg_command(
        "ffmpeg", Path("a.mp4.part"), Path("a.mov.tmp"), hevc=True, g711="pcm_mulaw"
    )
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-c:a") + 1] == "pcm_mulaw"
    assert command[command.index("-f") + 1] == "mov"


def test_source_needs_exactly_one_of_url_and_entity() -> None:
    """A source is a stream or a camera, not both and not neither."""
    with pytest.raises(RecordingError):
        Source()
    with pytest.raises(RecordingError):
        Source(url="door", entity="camera.door")

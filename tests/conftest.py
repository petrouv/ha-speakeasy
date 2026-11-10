"""Shared fixtures: a fake go2rtc that serves a fragmented MP4 made by ffmpeg."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.speakeasy import recorder
from custom_components.speakeasy.const import CONF_FOLDER, CONF_MIN_FREE_GB, DOMAIN

AAC_CONTENT_TYPE = 'video/mp4; codecs="avc1.64000c,mp4a.40.2"'
OPUS_CONTENT_TYPE = 'video/mp4; codecs="avc1.64000c,opus"'
G711_CONTENT_TYPE = 'video/mp4; codecs="hvc1.1.6.L60.B0,flac"'
TAPO_MEDIAS = ["video, recvonly, H265", "audio, recvonly, PCMA/8000", "audio, sendonly, PCMA/8000"]
CHUNK = 4096


def _make_sample(path: Path, audio_codec: str) -> bytes:
    """Three seconds of test picture and tone as fragmented MP4, the way go2rtc sends it."""
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _make_g711_sample(folder: Path) -> tuple[bytes, bytes]:
    """A Tapo-like stream as go2rtc sends it: H.265 and the camera's A-law turned into FLAC.

    Returns:
        The fragmented MP4 and the A-law bytes its sound was made from.
    """
    alaw, sample = folder / "tone.alaw", folder / "g711.mp4"
    quiet = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(
        [*quiet, "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "3"]
        + ["-c:a", "pcm_alaw", "-f", "alaw", str(alaw)],
        check=True,
    )
    subprocess.run(
        [*quiet, "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10"]
        + ["-f", "alaw", "-ar", "8000", "-ac", "1", "-i", str(alaw), "-t", "3"]
        + ["-c:v", "libx265", "-x265-params", "log-level=error", "-g", "10"]
        + ["-pix_fmt", "yuv420p", "-c:a", "flac", "-strict", "-2"]
        + ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(sample)],
        check=True,
    )
    return sample.read_bytes(), alaw.read_bytes()


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """Fragmented MP4 samples: AAC, Opus, and H.265 with G.711 sent as FLAC (`g711`, `alaw`)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    folder = tmp_path_factory.mktemp("samples")
    g711, alaw = _make_g711_sample(folder)
    return {
        "aac": _make_sample(folder / "aac.mp4", "aac"),
        "opus": _make_sample(folder / "opus.mp4", "libopus"),
        "g711": g711,
        "alaw": alaw,
    }


@dataclass
class FakeGo2rtc:
    """Fake go2rtc: what `stream.mp4` does is set per test.

    Attributes:
        mode: `hold` sends the sample and keeps the connection open, `end` sends
            it and closes, `silent` sends nothing, `empty` sends the headers and
            closes, `missing` answers 404.
        unavailable: how many first requests get go2rtc's 500 for a camera it cannot reach.
        requests: query of every `stream.mp4` request.
        medias: media lines of the stream's producer in `/api/streams?src=`.
    """

    body: bytes
    content_type: str = AAC_CONTENT_TYPE
    mode: str = "hold"
    url: str = ""
    requests: list[dict[str, str]] = field(default_factory=list)
    medias: list[str] = field(default_factory=list)
    unavailable: int = 0
    connections: int = 0

    async def streams(self, request: web.Request) -> web.Response:
        producer = {"url": "rtsp://user:secret@cam/1", "medias": self.medias}
        if "src" in request.query:
            return web.json_response({"producers": [producer], "consumers": []})
        return web.json_response({"door": {"producers": [producer]}})

    async def stream_mp4(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(dict(request.query))
        if self.mode == "missing":
            return web.Response(status=404, text="stream not found")
        if self.unavailable > 0:
            self.unavailable -= 1
            return web.Response(
                status=500, text="streams: dial tcp rtsp://user:secret@cam: i/o timeout\n"
            )
        if self.mode == "silent":
            await asyncio.sleep(3600)
        response = web.StreamResponse(headers={"Content-Type": self.content_type})
        await response.prepare(request)
        self.connections += 1
        try:
            if self.mode == "empty":
                return response
            for start in range(0, len(self.body), CHUNK):
                await response.write(self.body[start : start + CHUNK])
            if self.mode == "hold":
                await asyncio.sleep(3600)
        finally:
            self.connections -= 1
        return response


@pytest.fixture
async def go2rtc(samples: dict[str, bytes], socket_enabled) -> AsyncIterator[FakeGo2rtc]:
    """Running fake go2rtc on localhost; real sockets are allowed for it."""
    fake = FakeGo2rtc(body=samples["aac"])
    app = web.Application()
    app.router.add_get("/api/streams", fake.streams)
    app.router.add_get("/api/stream.mp4", fake.stream_mp4)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading the integration from custom_components."""
    return


@pytest.fixture
def short_stall():
    """Stall timeout of one second, for tests where go2rtc goes quiet."""
    with patch.object(recorder, "STALL_TIMEOUT", 1):
        yield


@pytest.fixture
def quick_retry():
    """Start retries every 0.1 s for at most one second."""
    with (
        patch.object(recorder, "START_RETRY_PAUSE", 0.1),
        patch.object(recorder, "START_RETRY_WINDOW", 1),
    ):
        yield


@pytest.fixture
def held_finalize() -> Iterator[asyncio.Event]:
    """Hold every file in `saving` until the returned event is set.

    Set at teardown too, so a failed test does not leave the unload waiting forever.
    """
    release = asyncio.Event()
    finalize = recorder.finalize_part

    async def held(*args):
        await release.wait()
        return await finalize(*args)

    with patch.object(recorder, "finalize_part", held):
        try:
            yield release
        finally:
            release.set()


@pytest.fixture
def held_close() -> Iterator[tuple[threading.Event, threading.Event]]:
    """Hold every recording's last write and close in its executor thread.

    Yields:
        `(entered, release)`: `entered` is set once a close is held; setting `release`
        lets it and every later one go.
    """
    entered, release = threading.Event(), threading.Event()
    write_and_close = recorder._write_and_close

    def held(file, data: bytes) -> None:
        entered.set()
        release.wait(10)
        write_and_close(file, data)

    with patch.object(recorder, "_write_and_close", held):
        try:
            yield entered, release
        finally:
            release.set()


@pytest.fixture
def saved_parts(hass: HomeAssistant) -> Iterator[list[bytes]]:
    """Collect each `.part` file's bytes as saving reads them, then save it as usual."""
    saved: list[bytes] = []
    # Captured here, before a test patches `async_add_executor_job` to hold its writes.
    add_job, finalize = hass.async_add_executor_job, recorder.finalize_part

    async def reading(hass_: HomeAssistant, part: Path, g711=None):
        saved.append(await add_job(part.read_bytes))
        return await finalize(hass_, part, g711)

    with patch.object(recorder, "finalize_part", reading):
        yield saved


@contextmanager
def part_removed_on_save() -> Iterator[None]:
    """Remove each `.part` file just as saving starts to read it."""
    is_hevc = recorder._is_hevc

    def removed_first(part: Path) -> bool:
        part.unlink()
        return is_hevc(part)

    with patch.object(recorder, "_is_hevc", removed_first):
        yield


@contextmanager
def remux_rename_fails() -> Iterator[None]:
    """ffmpeg's output (`.tmp`) cannot take the final name."""
    replace = os.replace

    def rename_fails(src, dst) -> None:
        if str(src).endswith(".tmp"):
            raise PermissionError(13, "Permission denied")
        replace(src, dst)

    with patch.object(recorder.os, "replace", rename_fails):
        yield


async def wait_for(condition, timeout: float = 10) -> None:
    """Poll a condition on the running loop."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


@pytest.fixture
def media_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Media folder of the test instance."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    return tmp_path


@pytest.fixture
def entry(hass: HomeAssistant, go2rtc: FakeGo2rtc, media_dir: Path) -> MockConfigEntry:
    """Config entry pointing at the fake go2rtc, with no free space limit."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Speakeasy",
        data={CONF_URL: go2rtc.url},
        options={CONF_FOLDER: "speakeasy", CONF_MIN_FREE_GB: 0},
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
async def loaded(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Set up config entry."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry

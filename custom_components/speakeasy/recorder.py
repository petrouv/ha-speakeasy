"""Server-side recording of go2rtc streams into Home Assistant media."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.components.camera import async_get_stream_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from homeassistant.util.ulid import ulid_now

from .const import (
    CONF_FOLDER,
    CONF_KEEP_DAYS,
    CONF_MIN_FREE_GB,
    DEFAULT_FOLDER,
    DEFAULT_KEEP_DAYS,
    DEFAULT_MIN_FREE_GB,
    DOMAIN,
    EVENT_RECORDING_FINISHED,
    MEDIA_SOURCE_KEY,
    PART_SUFFIX,
)
from .go2rtc import clean_url, mask_secrets, source_g711, source_name, stream_url

_LOGGER = logging.getLogger(__name__)

# No data from go2rtc for this long ends the recording; also the wait for the first data.
STALL_TIMEOUT = 30
CONNECT_TIMEOUT = 10
# go2rtc answers 5xx while it cannot open the camera: a short network outage, a camera
# that is rebooting or waking up. The start is retried for this long before it fails.
START_RETRY_WINDOW = 10
START_RETRY_PAUSE = 2
# Enough of go2rtc's error text to say why, without filling the log.
ERROR_TEXT_LIMIT = 200
DISK_CHECK_INTERVAL = 10
REMINDER_INTERVAL = timedelta(hours=1)
CLEANUP_INTERVAL = timedelta(days=1)
# Chunks from go2rtc are small; they are written to disk in blocks of this size.
WRITE_BLOCK = 512 * 1024
# go2rtc writes the init segment, with the codec boxes, at the very start of the stream.
HEADER_BYTES = 64 * 1024
FINAL_SUFFIXES = (".mp4", ".mov")
GIGABYTE = 1024**3

# Why a recording ended.
REASON_USER = "user"
REASON_DISK_LOW = "disk_low"
REASON_STALLED = "stalled"
REASON_STREAM_ENDED = "stream_ended"
REASON_SHUTDOWN = "shutdown"
REASON_ERROR = "error"

STATUS_RECORDING = "recording"
STATUS_SAVING = "saving"

# Subscriber events.
EVENT_LIST = "list"
EVENT_STARTED = "started"
EVENT_STOPPING = "stopping"
EVENT_STOPPED = "stopped"
EVENT_FAILED = "failed"


class RecordingError(HomeAssistantError):
    """A recording request cannot be served.

    `code` is the WebSocket error code and, unless `key` names another, the translation
    key of the service error. `detail` is English, for the WebSocket reply and the log;
    translations show it only when it is a text from outside (go2rtc, the system, Home
    Assistant) or a technical token, and say so. Anything worded here goes to the
    translations as its own key or as a language-neutral placeholder instead.
    """

    def __init__(
        self, code: str, detail: str = "", *, key: str | None = None, **placeholders: str
    ) -> None:
        super().__init__(
            detail or code,
            translation_domain=DOMAIN,
            translation_key=key or code,
            translation_placeholders={"detail": detail, **placeholders},
        )
        self.code = code


@dataclass(frozen=True)
class Source:
    """What to record, as the card describes it.

    The key is `url` or `entity` with `server`; `record_url` only changes what is
    pulled from go2rtc, so a card and a service call address the same recording.
    `entity` comes lowercased: the WebSocket schema validates it with `cv.entity_id`,
    the action schemas with `cv.entity_domain("camera")`, and both lowercase it.
    """

    url: str | None = None
    entity: str | None = None
    server: str | None = None
    record_url: str | None = None

    def __post_init__(self) -> None:
        # Trimmed, so `" door"` and `"door"` are one source; the card trims them too.
        for name in ("url", "record_url"):
            if (value := getattr(self, name)) is not None:
                object.__setattr__(self, name, value.strip())
        if (self.url is None) == (self.entity is None):
            raise RecordingError("invalid_source", "exactly one of url and entity is required")
        # Blank would reach go2rtc as no stream at all; the action schemas refuse it too.
        for name in ("url", "record_url"):
            if getattr(self, name) == "":
                raise RecordingError("invalid_source", f"{name} is empty")

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity of the recording: one active recording per key.

        `server` is compared trimmed and without trailing slashes, exactly as the
        card's `isOwnRecording` does, so both agree on which recording is a card's.
        """
        kind, value = ("url", self.url) if self.url is not None else ("entity", self.entity)
        return ((self.server or "").strip().rstrip("/"), kind, value or "")

    def as_dict(self) -> dict[str, str]:
        """Form the card matches against its own config.

        The URL is sent as is: it comes from a dashboard config that the same
        users can read anyway, and the card needs it to find its recording.
        """
        data = {"url": self.url} if self.url is not None else {"entity": self.entity}
        if self.server:
            data["server"] = self.server
        return data


@dataclass(frozen=True)
class RecordingResult:
    """A finished recording file.

    Attributes:
        finalized: ffmpeg remuxed the file; `False` leaves the raw fragmented MP4.
        error: ffmpeg error text when `finalized` is `False`.
    """

    id: str
    path: Path
    media_content_id: str
    duration: float
    size: int
    reason: str
    finalized: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """WebSocket form."""
        return {
            "id": self.id,
            "filename": self.path.name,
            "media_content_id": self.media_content_id,
            "duration": round(self.duration, 1),
            "size": self.size,
            "reason": self.reason,
            "finalized": self.finalized,
        }


@dataclass
class Recording:
    """One running recording."""

    id: str
    source: Source
    name: str
    # When go2rtc sent the first data, so a camera's wake-up is not counted;
    # until then, when the start was asked. Only announced recordings are shown.
    started_at: datetime
    folder: Path
    connected: asyncio.Future[None]
    done: asyncio.Future[RecordingResult | None]
    # Created at the first data and named after `started_at`, so the name agrees with it.
    part_path: Path | None = None
    # Saving failed but the part is still there: the next start finishes it, if the
    # recordings folder in the options is still the part's.
    part_kept: bool = False
    status: str = STATUS_RECORDING
    # No longer running: a stop asked for it, or the stream ended by itself (or a write
    # failed) and the last write is closing the file. Later stops change nothing.
    ending: bool = False
    # Asked for by a stop; `reason` is why it ended, set as it turns `saving`.
    stop_reason: str | None = None
    reason: str | None = None
    hours_reminded: int = 0
    # `time.monotonic()` of the first and the last data: the duration, without a stall.
    first_data: float = field(default=0.0, repr=False)
    last_data: float = field(default=0.0, repr=False)
    # Order of the first data, for recordings whose `started_at` is the same.
    started_order: int = field(default=0, repr=False)
    stream_task: asyncio.Task[str] | None = None
    # go2rtc that serves the stream, asked about the camera's audio too.
    server: str = ""
    g711_task: asyncio.Task[str | None] | None = None
    cancel_reminder: CALLBACK_TYPE | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """WebSocket form.

        `reason` is `None` while recording. Once saving it says why the recording ended,
        so a card that missed `stopping` (subscribed later) still can tell its user.
        """
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source.as_dict(),
            "started_at": self.started_at.isoformat(),
            "status": self.status,
            "reason": self.reason,
        }

    def as_attribute(self) -> dict[str, Any]:
        """Entity attribute form for the history database.

        No source, and `name` of a URL source has no login, path or query.
        """
        return {
            "id": self.id,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "status": self.status,
        }


# eq=False: `_checks` removes by identity, and two starts of one source would compare equal.
@dataclass(eq=False)
class _Check:
    """A start still checking its source and the disk, before it joins `_recordings`."""

    key: tuple[str, str, str]
    # Set by a stop that came meanwhile: the start then fails with `stopped`.
    stopped: bool = False


def camera_slug(source: Source) -> str:
    """File name part for a camera; never contains a URL."""
    if source.entity is not None:
        return slugify(source.entity.partition(".")[2]) or "camera"
    if source.url is None or "://" in source.url:
        return "camera"
    return slugify(source.url) or "camera"


def ffmpeg_command(
    binary: str, part: Path, output: Path, *, hevc: bool = False, g711: str | None = None
) -> list[str]:
    """Command that turns a fragmented MP4 into a regular file, with the index in front.

    Video and audio are copied as the device sends them, never transcoded (owner's
    decision). A missing audio track is fine (`0:a?`).

    Args:
        hevc: the video is H.265; it is tagged `hvc1`, the only tag Apple players accept.
        g711: the camera sends G.711 (`pcm_alaw` or `pcm_mulaw`), which go2rtc passed
            on as FLAC. Encoding it back gives the camera's own bytes, and the file is
            MOV: MP4 can't hold G.711.
    """
    video_tag = ["-tag:v", "hvc1"] if hevc else []
    audio = ["-c:a", g711] if g711 else []
    return [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(part),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c",
        "copy",
        *video_tag,
        *audio,
        "-movflags",
        "+faststart",
        "-f",
        "mov" if g711 else "mp4",
        str(output),
    ]


def _is_hevc(part: Path) -> bool:
    """Whether the video in a `.part` file is H.265: its init segment has an `hvcC` box."""
    with part.open("rb") as file:
        return b"hvcC" in file.read(HEADER_BYTES)


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _create_part(folder: Path, stem: str) -> tuple[Path, IO[bytes]]:
    """Create a `.part` file under a name not taken by a finished or running recording.

    Created exclusively, so two recordings named in the same second never share a file.
    """
    folder.mkdir(parents=True, exist_ok=True)
    candidate, number = stem, 1
    while True:
        if not any((folder / f"{candidate}{suffix}").exists() for suffix in FINAL_SUFFIXES):
            path = folder / f"{candidate}.mp4{PART_SUFFIX}"
            try:
                return path, path.open("xb")
            except FileExistsError:
                pass
        number += 1
        candidate = f"{stem}_{number}"


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _list_parts(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"*.mp4{PART_SUFFIX}"))


def _delete_older_than(folder: Path, days: int) -> list[str]:
    """Delete finished recordings older than `days`; running `.part` files stay."""
    if not folder.is_dir():
        return []
    limit = time.time() - days * 86400
    deleted = []
    for path in folder.iterdir():
        if path.suffix not in FINAL_SUFFIXES:
            continue
        if path.is_file() and path.stat().st_mtime < limit:
            path.unlink(missing_ok=True)
            deleted.append(path.name)
    return deleted


async def finalize_part(
    hass: HomeAssistant, part: Path, g711: str | None = None
) -> tuple[Path, bool, str | None] | None:
    """Remux a `.part` file into the final MP4, or MOV for G.711 audio (see `ffmpeg_command`).

    Returns:
        `(path, finalized, error)`, or `None` when the part held no data and was removed,
        or was missing. When ffmpeg fails or cannot run, the raw file is kept under the
        final name.

    Raises:
        OSError: the part could not be read or moved, e.g. it was removed meanwhile.
            A part that is still there stays as it was, for the next start.
    """
    add_job = hass.async_add_executor_job
    if await add_job(_file_size, part) == 0:
        await add_job(part.unlink, True)
        return None
    raw = part.with_name(part.name.removesuffix(PART_SUFFIX))
    output = raw.with_suffix(".mov") if g711 else raw
    temporary = output.with_name(f"{output.name}.tmp")
    hevc = await add_job(_is_hevc, part)
    command = ffmpeg_command(get_ffmpeg_manager(hass).binary, part, temporary, hevc=hevc, g711=g711)
    try:
        error = await _run_ffmpeg(command)
        if error is None:
            # One job: a cancel (Home Assistant stopping) leaves both done or neither. A part
            # left next to its saved file would be remuxed again at the next start.
            await add_job(_replace_and_remove, temporary, output, part)
            return output, True, None
    finally:
        # A failed, cut or unrenamed remux: retention never removes a `.tmp`, so it would
        # stay forever. A part still there is remuxed again at the next start.
        await add_job(temporary.unlink, True)
    _LOGGER.warning("ffmpeg could not remux %s, keeping the raw file: %s", part.name, error)
    await add_job(os.replace, part, raw)
    return raw, False, error


async def _run_ffmpeg(command: list[str]) -> str | None:
    """Run an ffmpeg command; return why it failed, or `None`."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except OSError as err:
        # No ffmpeg binary (a Core install without it still loads the ffmpeg integration).
        return f"ffmpeg could not run: {err}"
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        # Home Assistant is shutting down: the `.part` stays and is finished at the next start.
        process.kill()
        await process.wait()
        raise
    if process.returncode == 0:
        return None
    return mask_secrets(stderr.decode(errors="replace").strip()[-500:]) or "ffmpeg failed"


class RecordingManager:
    """Owns all recordings of the integration: start, stop, events, upkeep."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._recordings: dict[tuple[str, str, str], Recording] = {}
        # Ended recordings (stopped, or ended by themselves) whose file is being saved, by ID.
        self._saving: dict[str, Recording] = {}
        # Hands out `Recording.started_order`.
        self._started_order = itertools.count(1)
        # Starts not in `_recordings` yet; stops mark them, as they cannot cancel them.
        self._checks: list[_Check] = []
        # Set by `async_stop_all`: every later start fails (`_check_open`).
        self._closed = False
        # Removals of `.part` files created for starts stopped meanwhile (`_discard_created`).
        self._discards: set[asyncio.Task[None]] = set()
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []

    # Options are read on every use: changing them needs no reload, so running
    # recordings are never cut by an options change.

    @property
    def folder(self) -> Path:
        """Absolute folder for recordings, as set in the options.

        Raises:
            RecordingError: Home Assistant has no `local` media folder.
        """
        media_dir = self.hass.config.media_dirs.get(MEDIA_SOURCE_KEY)
        if media_dir is None:
            raise RecordingError("media_dir_missing")
        return Path(media_dir) / self.entry.options.get(CONF_FOLDER, DEFAULT_FOLDER)

    @property
    def min_free_bytes(self) -> int:
        """Free space below which recordings stop."""
        return int(self.entry.options.get(CONF_MIN_FREE_GB, DEFAULT_MIN_FREE_GB) * GIGABYTE)

    @property
    def recordings(self) -> list[Recording]:
        """Announced recordings, running or saving, oldest first.

        A recording that is still waiting for its first data is left out.
        """
        announced = [
            rec
            for rec in (*self._recordings.values(), *self._saving.values())
            if rec.connected.done() and rec.connected.exception() is None
        ]
        return sorted(announced, key=lambda rec: (rec.started_at, rec.started_order))

    def get(self, recording_id: str) -> Recording | None:
        """Find a running or saving recording by ID."""
        if (recording := self._saving.get(recording_id)) is not None:
            return recording
        return next((rec for rec in self._recordings.values() if rec.id == recording_id), None)

    def find(self, source: Source) -> Recording | None:
        """Find the running recording of a source.

        Running, ending, or a start still waiting for its first data.
        """
        return self._recordings.get(source.key)

    def find_saving(self, source: Source) -> Recording | None:
        """Find the newest saving recording of a source."""
        return next(
            (rec for rec in reversed(self._saving.values()) if rec.source.key == source.key), None
        )

    @callback
    def async_subscribe(self, listener: Callable[[str, dict[str, Any]], None]) -> CALLBACK_TYPE:
        """Call `listener(event, payload)` on every recording change."""
        self._listeners.append(listener)

        @callback
        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    @callback
    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            listener(event, payload)

    async def async_start(self, source: Source) -> Recording:
        """Start recording a source, or return the recording already running for it.

        A recording that is ending (stopped, or its stream ended) is not running: the start
        waits for its file to close, then begins a new one.

        Waits until go2rtc sends the first data: a camera go2rtc cannot open yet (HTTP 5xx)
        is retried for `START_RETRY_WINDOW` seconds, then the first data may take up to
        `STALL_TIMEOUT` more.

        Raises:
            RecordingError: the source, the disk, the folder or go2rtc does not allow
                recording, the entry unloads (not_configured), or the recording was
                stopped before its first data, or Home Assistant stops (stopped).
        """
        self._check_open()
        # One that is ending is not running: the start goes on, and waits for it below.
        existing = self._recordings.get(source.key)
        if existing is not None and not existing.ending:
            await _wait_connected(existing)
            return existing

        server = self.entry.data[CONF_URL]
        if source.server and (server := clean_url(source.server)) is None:
            raise RecordingError("invalid_source", "server is not a URL")
        check = _Check(source.key)
        self._checks.append(check)
        try:
            url = await self._request_url(server, source)
            folder = self.folder
            await self._check_disk(folder)
            # Still a check meanwhile, so a stop of the source stops this start too.
            existing = await self._running(source.key)
        finally:
            self._checks.remove(check)

        # An unload or Home Assistant stopping may have begun while this call waited on
        # the checks above. No await from here to the insert.
        self._check_open()
        if check.stopped:
            raise RecordingError("stopped")
        # The key may have been taken while this call waited on the checks above.
        if existing is not None:
            await _wait_connected(existing)
            return existing

        loop = self.hass.loop
        recording = Recording(
            id=ulid_now(),
            source=source,
            name=self._display_name(source),
            started_at=dt_util.now(),
            folder=folder,
            connected=loop.create_future(),
            done=loop.create_future(),
            server=server,
        )
        self._recordings[source.key] = recording
        self.entry.async_create_background_task(
            self.hass, self._run(recording, url), f"{DOMAIN} recording {recording.id}"
        )
        await _wait_connected(recording)
        return recording

    async def _running(self, key: tuple[str, str, str]) -> Recording | None:
        """The recording running for a source, once one that is ending has freed it."""
        while True:
            existing = self._recordings.get(key)
            if existing is None or not existing.ending:
                return existing
            # The stream task is set before anyone sees the recording (`_run` starts
            # eagerly). Its end lets `_run_inner` free the source, maybe a step later: look again.
            await asyncio.wait([existing.stream_task])

    def _check_open(self) -> None:
        """Refuse a start once `async_stop_all` has run: it would not stop a recording added later.

        An unload answers `not_configured`, as the commands do once the entry is no longer
        loaded; Home Assistant stopping answers `stopped`.
        """
        if async_get_manager(self.hass) is not self:
            raise RecordingError("not_configured")
        if self._closed:
            raise RecordingError("stopped")

    @callback
    def async_request_stop(self, recording: Recording, reason: str = REASON_USER) -> None:
        """Ask a recording to stop; finishing happens in the background.

        Only the first request cancels the stream, and none once the stream ended by
        itself: a cancel then would cut the last write of the file.
        """
        if recording.ending:
            return
        recording.ending = True
        recording.stop_reason = reason
        if recording.stream_task is not None:
            recording.stream_task.cancel()

    async def async_stop(self, recording: Recording) -> RecordingResult | None:
        """Stop a recording, reason `user`, and wait for the final file.

        One already ending keeps its own reason and outcome.
        """
        self.async_request_stop(recording)
        return await asyncio.shield(recording.done)

    @callback
    def async_stop_checks(self, source: Source) -> bool:
        """Make the starts of a source still being checked fail with `stopped`.

        Returns:
            Whether there were any.
        """
        checks = [check for check in self._checks if check.key == source.key]
        for check in checks:
            check.stopped = True
        return bool(checks)

    @callback
    def async_request_stop_all(self) -> list[str]:
        """Ask every running recording to stop, starts not yet announced included.

        Returns:
            IDs of every announced recording whose outcome is still to come, oldest first,
            whoever stopped it. Starts without data were never announced and have no ID.
        """
        ids = [rec.id for rec in self.recordings]
        for recording in self._recordings.values():
            self.async_request_stop(recording)
        for check in self._checks:
            check.stopped = True
        return ids

    async def async_stop_all(self) -> None:
        """Stop every recording for good, reason `shutdown`, and wait until all files are final.

        One already ending or saving keeps its own reason, and one whose last write fails
        ends with `error`. Called as the entry unloads or Home Assistant stops. Starts still
        being checked, and any start after this, fail with `stopped` (`not_configured` once
        an unload has begun).
        """
        self._closed = True
        recordings = [*self._recordings.values(), *self._saving.values()]
        for recording in recordings:
            self.async_request_stop(recording, REASON_SHUTDOWN)
        if recordings:
            await asyncio.wait([asyncio.shield(rec.done) for rec in recordings])
        # A stopped start's file may still be on its way: the next setup of the entry would
        # take it for a leftover while it is being removed.
        if self._discards:
            await asyncio.wait([asyncio.shield(task) for task in self._discards])

    async def async_list_parts(self) -> list[Path]:
        """`.part` files in the recordings folder."""
        try:
            folder = self.folder
        except RecordingError:
            return []
        return await self.hass.async_add_executor_job(_list_parts, folder)

    async def async_finish_parts(self, parts: list[Path]) -> None:
        """Finish `.part` files left by a crash or a cut-off shutdown.

        Args:
            parts: listed by `async_list_parts` before the entry loaded, when no
                recording could have created its own.
        """
        for part in parts:
            # One bad part must not keep the others, and retention after them, from running.
            try:
                result = await finalize_part(self.hass, part)
            except OSError as err:
                _LOGGER.error("Could not finish %s left from the last run: %s", part.name, err)
                continue
            _LOGGER.info("Finished a recording left from the last run: %s", part.name)
            if result is not None:
                path, finalized, error = result
                self._fire_finished_event(None, path, finalized, error, 0.0, None)

    async def async_delete_old(self, _now: datetime | None = None) -> None:
        """Delete recordings older than the retention set in the options (0 keeps all)."""
        days = self.entry.options.get(CONF_KEEP_DAYS, DEFAULT_KEEP_DAYS)
        if days <= 0:
            return
        try:
            folder = self.folder
        except RecordingError:
            return
        deleted = await self.hass.async_add_executor_job(_delete_older_than, folder, days)
        if deleted:
            _LOGGER.info("Deleted %d recordings older than %d days", len(deleted), days)

    @callback
    def async_track_cleanup(self) -> CALLBACK_TYPE:
        """Run retention once a day."""
        return async_track_time_interval(
            self.hass, self.async_delete_old, CLEANUP_INTERVAL, name=f"{DOMAIN} retention"
        )

    async def _request_url(self, server: str, source: Source) -> str:
        if source.record_url:
            return stream_url(server, source.record_url)
        if source.url is not None:
            return stream_url(server, source.url)
        try:
            src = await async_get_stream_source(self.hass, source.entity)
        except HomeAssistantError as err:
            raise RecordingError(
                "no_stream_source", str(err), key="no_stream_source_error"
            ) from None
        if not src:
            raise RecordingError("no_stream_source", "the camera has no stream source")
        # Same `src` and `name` as WebRTC Camera: go2rtc reuses the stream the card is showing.
        return stream_url(server, src, name=source.entity)

    def _display_name(self, source: Source) -> str:
        if source.entity is not None:
            state = self.hass.states.get(source.entity)
            return state.name if state is not None else source.entity
        return source_name(source.url or "")

    async def _check_disk(self, folder: Path) -> None:
        free = await self.hass.async_add_executor_job(_free_bytes, _existing_parent(folder))
        if free < self.min_free_bytes:
            gigabytes = f"{free / GIGABYTE:.1f}"
            raise RecordingError("disk_low", f"{gigabytes} GB free", free=gigabytes)

    async def _run(self, recording: Recording, url: str) -> None:
        """Drive one recording from the first request to the final file.

        When Home Assistant stops, background tasks are cancelled after the
        shutdown jobs; a recording cut there keeps its `.part` file, which
        `async_finish_parts` completes at the next start.
        """
        try:
            await self._run_inner(recording, url)
        except asyncio.CancelledError:
            if recording.stream_task is not None:
                recording.stream_task.cancel()
            _drop(recording.g711_task)
            if not recording.connected.done():
                recording.connected.set_exception(RecordingError("stopped"))
                # Retrieved: its waiters may all have been cancelled (`_wait_connected`).
                recording.connected.exception()
            if not recording.done.done():
                recording.done.cancel()
            self._recordings.pop(recording.source.key, None)
            self._saving.pop(recording.id, None)
            raise

    async def _run_inner(self, recording: Recording, url: str) -> None:
        recording.stream_task = self.hass.async_create_background_task(
            self._stream(recording, url), f"{DOMAIN} stream {recording.id}", eager_start=False
        )
        await asyncio.wait([recording.stream_task])
        if recording.stream_task.cancelled():
            reason = recording.stop_reason or REASON_USER
        elif (err := recording.stream_task.exception()) is not None:
            if recording.connected.done():
                _LOGGER.error("Recording %s failed: %s", recording.name, mask_secrets(repr(err)))
            reason = REASON_ERROR
        else:
            reason = recording.stream_task.result()

        # From here on the source is free: a new start begins a new recording.
        del self._recordings[recording.source.key]
        if not recording.connected.done():
            # Stopped or failed before go2rtc sent anything: nothing was announced or written.
            recording.connected.set_exception(_start_error(recording.stream_task))
            # Retrieved: its waiters may all have been cancelled (`_wait_connected`).
            recording.connected.exception()
            _drop(recording.g711_task)
            recording.done.set_result(None)
            return

        if recording.cancel_reminder is not None:
            recording.cancel_reminder()
        if recording.hours_reminded:
            persistent_notification.async_dismiss(self.hass, _reminder_id(recording))
        recording.status = STATUS_SAVING
        recording.reason = reason
        self._saving[recording.id] = recording
        self._notify(EVENT_STOPPING, {"recording": recording.as_dict(), "reason": reason})
        # What was recorded: a stall's silent seconds before it ended are not counted.
        duration = recording.last_data - recording.first_data
        g711 = None
        if (task := recording.g711_task) is not None:
            await asyncio.wait([task])
            # Not raised: `done` and `stopped` must still come. Without the answer the
            # file is saved as go2rtc sent it.
            if task.cancelled():
                pass
            elif (err := task.exception()) is not None:
                _LOGGER.warning(
                    "Could not ask go2rtc about the sound of %s: %s",
                    recording.name,
                    mask_secrets(repr(err)),
                )
            else:
                g711 = task.result()
        size = 0
        try:
            finalized = await finalize_part(self.hass, recording.part_path, g711)
        except OSError as err:
            # Not raised: `done` and `failed` must still come, or stops and the sensor hang.
            finalized = None
            recording.part_kept = await self.hass.async_add_executor_job(recording.part_path.exists)
            if recording.part_kept:
                _LOGGER.error(
                    "Could not save recording %s, keeping %s for the next start: %s",
                    recording.name,
                    recording.part_path.name,
                    err,
                )
            else:
                _LOGGER.error("Could not save recording %s: %s", recording.name, err)
        else:
            if finalized is not None:
                try:
                    size = await self.hass.async_add_executor_job(_file_size, finalized[0])
                except OSError as err:
                    # The file is saved; an unknown size stays 0 rather than hold `done`.
                    _LOGGER.warning("Could not read the size of %s: %s", finalized[0].name, err)
        finally:
            # Only now, with no await before the events below: until then a stop finds it.
            self._saving.pop(recording.id, None)

        if finalized is None:
            # `_copy` creates the part at the first data and always writes what came: an
            # empty or missing part means a write failed or the file was removed meanwhile.
            # A part that could not be read or moved stays (`kept`), and `async_finish_parts`
            # finishes it at the next start.
            recording.done.set_result(None)
            self._notify(
                EVENT_FAILED,
                {"recording": recording.as_dict(), "reason": reason, "kept": recording.part_kept},
            )
            return
        path, remuxed, error = finalized
        result = RecordingResult(
            id=recording.id,
            path=path,
            media_content_id=self._media_content_id(path),
            duration=duration,
            size=size,
            reason=reason,
            finalized=remuxed,
            error=error,
        )
        recording.done.set_result(result)
        self._notify(EVENT_STOPPED, {"recording": recording.as_dict(), "result": result.as_dict()})
        self._fire_finished_event(recording, path, remuxed, error, duration, reason)

    async def _stream(self, recording: Recording, url: str) -> str:
        """Open the stream, waiting for a camera that wakes up; then record it.

        Only the start is retried: once data has come, `_stream_once` no longer
        raises `camera_unavailable`.
        """
        deadline = time.monotonic() + START_RETRY_WINDOW
        while True:
            try:
                return await self._stream_once(recording, url)
            except RecordingError as err:
                if err.code != "camera_unavailable":
                    raise
                if time.monotonic() >= deadline:
                    _LOGGER.warning("Cannot start recording %s: %s", recording.name, err)
                    raise
                _LOGGER.debug("go2rtc cannot open %s yet, retrying: %s", recording.name, err)
            await asyncio.sleep(START_RETRY_PAUSE)

    async def _stream_once(self, recording: Recording, url: str) -> str:
        """Copy the go2rtc stream into the `.part` file; return why it ended.

        A stop request cancels this task. File writes go through the executor and a
        cancel does not cancel them, so a stop never drops a block, and the final write
        follows them.
        """
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=CONNECT_TIMEOUT, sock_read=STALL_TIMEOUT
        )
        try:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 404:
                    raise RecordingError("stream_not_found")
                if response.status >= 500:
                    # go2rtc could not open the camera; its text says why.
                    raise RecordingError("camera_unavailable", await _error_text(response))
                if response.status != 200:
                    raise RecordingError("go2rtc_error", f"HTTP {response.status}")
                if "flac" in response.headers.get("Content-Type", "").lower():
                    # FLAC is how go2rtc passes on PCM audio, G.711 included: find out which.
                    # Asked by the name go2rtc knows the stream by, which may be a camera URL.
                    source = recording.source
                    name = source.record_url or source.url or source.entity or ""
                    recording.g711_task = self.hass.async_create_background_task(
                        source_g711(session, recording.server, name),
                        f"{DOMAIN} audio {recording.id}",
                    )
                return await self._copy(recording, response)
        except (aiohttp.ClientError, TimeoutError) as err:
            if not recording.connected.done():
                raise RecordingError("go2rtc_error", type(err).__name__) from None
            if isinstance(err, TimeoutError):
                return REASON_STALLED
            return REASON_STREAM_ENDED

    async def _copy(self, recording: Recording, response: aiohttp.ClientResponse) -> str:
        add_job = self.hass.async_add_executor_job
        file: IO[bytes] | None = None
        buffer = bytearray()
        write: asyncio.Future[int] | None = None
        try:
            next_disk_check = time.monotonic() + DISK_CHECK_INTERVAL
            while chunk := await response.content.readany():
                recording.last_data = time.monotonic()
                if file is None:
                    recording.first_data = recording.last_data
                    recording.started_at = dt_util.now()
                    recording.started_order = next(self._started_order)
                    stem = f"{camera_slug(recording.source)}_{recording.started_at:%Y%m%d_%H%M%S}"
                    try:
                        created = add_job(_create_part, recording.folder, stem)
                        # `wait` leaves the job running when a stop cancels this, so its
                        # file is not lost; a shield would also report the job's later
                        # error as unhandled, which `_discard_created` retrieves instead.
                        await asyncio.wait([created])
                        recording.part_path, file = created.result()
                    except asyncio.CancelledError:
                        # Stopped while the file was being created: remove it once it is.
                        discard = self.hass.async_create_background_task(
                            self._discard_created(created), f"{DOMAIN} discard {recording.id}"
                        )
                        self._discards.add(discard)
                        discard.add_done_callback(self._discards.discard)
                        raise
                    except OSError as err:
                        # The folder, not go2rtc: say so, or the start reads as a stream error.
                        _LOGGER.error("Cannot create the recording file: %s", err)
                        raise RecordingError("folder_not_writable", str(err)) from None
                    recording.connected.set_result(None)
                    self._on_connected(recording)
                buffer += chunk
                if len(buffer) >= WRITE_BLOCK:
                    write = add_job(file.write, bytes(buffer))
                    buffer.clear()
                    # `wait`, not a shield, as for `created`: the stop's cleanup below
                    # raises the job's later error instead.
                    await asyncio.wait([write])
                    write.result()
                if time.monotonic() >= next_disk_check:
                    next_disk_check = time.monotonic() + DISK_CHECK_INTERVAL
                    if await add_job(_free_bytes, recording.part_path.parent) < self.min_free_bytes:
                        _LOGGER.warning("Low disk space, stopping recording %s", recording.name)
                        return REASON_DISK_LOW
            return REASON_STREAM_ENDED
        finally:
            if file is not None:
                # Over, whatever ended it: a stop that comes now must not cancel the last
                # write, and a start must wait for it instead of joining this recording.
                recording.ending = True
                lost: BaseException | None = None
                if write is not None:
                    # A stop lands on the last block's wait, right after it was taken from
                    # `buffer`: its write may still be running, or have failed unseen.
                    # After an error without a stop, this raises that same error again.
                    if not write.done():
                        await asyncio.wait([write])
                    lost = None if write.cancelled() else write.exception()
                await add_job(_write_and_close, file, bytes(buffer))
                if lost is not None:
                    # Ends like any other write error: logged, reason `error`.
                    raise lost

    async def _discard_created(self, created: asyncio.Future[tuple[Path, IO[bytes]]]) -> None:
        """Close and remove a `.part` file created for a start stopped meanwhile."""
        await asyncio.wait([created])
        if created.cancelled() or created.exception() is not None:
            return
        await self.hass.async_add_executor_job(_close_and_remove, *created.result())

    @callback
    def _on_connected(self, recording: Recording) -> None:
        async def remind(_now: datetime) -> None:
            recording.hours_reminded += 1
            # In the language of Home Assistant at the time of the reminder.
            texts = await async_get_translations(
                self.hass, self.hass.config.language, "exceptions", [DOMAIN]
            )
            prefix = f"component.{DOMAIN}.exceptions"
            persistent_notification.async_create(
                self.hass,
                texts[f"{prefix}.reminder_message.message"].format(
                    name=recording.name,
                    hours=recording.hours_reminded,
                    action=f"{DOMAIN}.record_stop_all",
                ),
                title=texts[f"{prefix}.reminder_title.message"],
                notification_id=_reminder_id(recording),
            )

        recording.cancel_reminder = async_track_time_interval(
            self.hass, remind, REMINDER_INTERVAL, name=f"{DOMAIN} reminder {recording.id}"
        )
        self._notify(EVENT_STARTED, {"recording": recording.as_dict()})

    def _media_content_id(self, path: Path) -> str:
        """Media ID from the real file path: the folder option may change during a recording."""
        media_dir = Path(self.hass.config.media_dirs[MEDIA_SOURCE_KEY])
        return f"media-source://media_source/{MEDIA_SOURCE_KEY}/{path.relative_to(media_dir)}"

    @callback
    def _fire_finished_event(
        self,
        recording: Recording | None,
        path: Path,
        finalized: bool,
        error: str | None,
        duration: float,
        reason: str | None,
    ) -> None:
        data: dict[str, Any] = {
            "path": str(path),
            "filename": path.name,
            "media_content_id": self._media_content_id(path),
            "finalized": finalized,
            "reason": reason,
            "duration": round(duration, 1),
        }
        if error:
            data["error"] = error
        if recording is not None:
            data |= {
                "id": recording.id,
                "name": recording.name,
                "entity_id": recording.source.entity,
                "url": mask_secrets(recording.source.url) if recording.source.url else None,
            }
        self.hass.bus.async_fire(EVENT_RECORDING_FINISHED, data)


@callback
def async_get_manager(hass: HomeAssistant) -> RecordingManager:
    """Manager of the loaded config entry.

    Raises:
        RecordingError: the commands are registered but no entry is loaded: it was
            removed, disabled or unloaded since Home Assistant started, failed to set up,
            or a leftover `speakeasy:` YAML key loaded the integration without an entry.
    """
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise RecordingError("not_configured")


def _write_and_close(file: Any, data: bytes) -> None:
    try:
        if data:
            file.write(data)
    finally:
        file.close()


def _replace_and_remove(temporary: Path, output: Path, part: Path) -> None:
    os.replace(temporary, output)
    # missing_ok: a part removed while ffmpeg read it does not undo the saved file.
    part.unlink(missing_ok=True)


def _close_and_remove(path: Path, file: IO[bytes]) -> None:
    try:
        file.close()
    finally:
        path.unlink(missing_ok=True)


def _existing_parent(path: Path) -> Path:
    """Nearest existing folder, for a free space check before the folder is created."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _reminder_id(recording: Recording) -> str:
    return f"{DOMAIN}_recording_{recording.id}"


async def _error_text(response: aiohttp.ClientResponse) -> str:
    """go2rtc's reason for an error answer, without camera passwords."""
    try:
        text = (await response.text(errors="replace")).strip()
    except aiohttp.ClientError, TimeoutError:
        text = ""
    text = mask_secrets(text.splitlines()[0] if text else "")[:ERROR_TEXT_LIMIT]
    return text or f"HTTP {response.status}"


async def _wait_connected(recording: Recording) -> None:
    """Wait for the first data of a recording; raise why it never came.

    `wait`, not a shield: a caller cancelled meanwhile leaves the future alone, while a
    shield would report the start's later error as unhandled, with no recording named.
    """
    if not recording.connected.done():
        await asyncio.wait([recording.connected])
    recording.connected.result()


def _drop(task: asyncio.Task[Any] | None) -> None:
    """Stop waiting for a task whose answer is no longer needed.

    A finished one has its error read: otherwise asyncio logs it as never retrieved.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()


def _start_error(task: asyncio.Task[str]) -> RecordingError:
    """Error for the callers of a start that never received data."""
    if task.cancelled():
        return RecordingError("stopped")
    if (err := task.exception()) is None:
        return RecordingError(
            "go2rtc_error", "the stream ended before any data", key="go2rtc_no_data"
        )
    if isinstance(err, RecordingError):
        return err
    return RecordingError("go2rtc_error", type(err).__name__)

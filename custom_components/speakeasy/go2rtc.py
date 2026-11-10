"""go2rtc address handling: discovery, check, request URL, secret masking."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntries
from homeassistant.const import CONF_URL
from homeassistant.helpers import config_validation as cv
from yarl import URL

from .const import DEFAULT_URL, WEBRTC_DOMAIN

API_TIMEOUT = 10

_RTSP_URL = re.compile(r"rtsps?://[^\s'\"<>]*", re.IGNORECASE)
# Any other URL with `user:password@`: go2rtc sources also use http, rtmp, onvif and so on.
_USERINFO_URL = re.compile(r"\b([a-z][a-z0-9+.\-]*)://[^\s/'\"<>]*@[^\s'\"<>]*", re.IGNORECASE)
# The same addresses URL-encoded, as a request's `?src=` carries them: masked whole.
_ENCODED_URL = re.compile(r"\b([a-z][a-z0-9+.\-]*)%3A%2F%2F[^\s'\"<>&]*", re.IGNORECASE)
# go2rtc sources that run a program: its arguments may carry a password or a token
# (`exec:ffmpeg -headers "Authorization: …"`), so only the program is kept: the rest
# of the line goes, quotes and all, since an argument may itself be quoted.
_COMMAND = re.compile(r"\b(exec|echo):(\S+)(\s[^\n]*)?", re.IGNORECASE)
_ENCODED_COMMAND = re.compile(r"\b(exec|echo)%3A[^\s'\"<>&]*", re.IGNORECASE)


class Go2rtcError(Exception):
    """go2rtc is unreachable or replied in an unexpected way."""


def mask_secrets(text: str) -> str:
    """Hide camera addresses in a text: they carry the camera login and password.

    A go2rtc source can itself be such an address, so sources go through here
    before they reach a log, an event or a notification.
    """
    text = _ENCODED_COMMAND.sub(r"\1:***", _ENCODED_URL.sub(r"\1://***", text))
    text = _COMMAND.sub(_program_only, text)
    return _USERINFO_URL.sub(r"\1://***", _RTSP_URL.sub("rtsp://***", text))


def _program_only(match: re.Match[str]) -> str:
    # The arguments are not shown, but that there were some is: `exec:ffmpeg ***`.
    return f"{match.group(1)}:{match.group(2)}" + (" ***" if match.group(3) else "")


_ANY_URL = re.compile(r"\b([a-z][a-z0-9+.\-]*)://[^\s'\"<>]*", re.IGNORECASE)


def _origin(match: re.Match[str]) -> str:
    text = match.group(0)
    try:
        url = URL(text)
        # Every `@` must be the one ending `user:password`: otherwise a password with
        # `#`, `/` or `@` in it was read as the host or port (`rtsp://admin:12#3@cam`).
        ats = text.count("@")
        if url.host and ats == (0 if url.raw_user is None else 1):
            return str(URL.build(scheme=url.scheme, host=url.host, port=url.explicit_port))
    except ValueError:
        pass
    return f"{match.group(1)}://***"


def source_name(source: str) -> str:
    """Name a stream source for people: each address in it cut to its scheme, host and port.

    Unlike `mask_secrets`, cameras on different hosts or ports get different names;
    channels of one recorder share one. The rest of an address goes: besides
    `user:password@`, its path or query may carry the password too (`?pwd=`,
    `/user=admin_password=…`). So does everything from the first `?` or `#` of
    the source, since go2rtc sources without `://` keep tokens there too
    (`nest:?client_secret=…`), and the arguments of a program it runs (`exec:`,
    `echo:`). A go2rtc stream name stays as it is.
    """
    named = _ANY_URL.sub(_origin, _COMMAND.sub(r"\1:\2", source))
    return re.split(r"[?#]", named, maxsplit=1)[0]


def clean_url(raw: str) -> str | None:
    """Return the base address of go2rtc without a trailing slash, or `None` if not a URL.

    The query and the fragment are dropped, and so is a page name: an address
    copied from the go2rtc web UI looks like `http://host:1984/streams.html?src=x`.
    """
    try:
        url = URL(cv.url(raw.strip()))
    except vol.Invalid, ValueError:
        return None
    path = url.path.rstrip("/")
    if path.endswith(".html"):
        path = path.rpartition("/")[0]
    return str(url.with_path(path).with_query(None).with_fragment(None)).rstrip("/")


def discover_url(config_entries: ConfigEntries) -> str:
    """Return the go2rtc address WebRTC Camera uses, or the address it falls back to.

    WebRTC Camera keeps a user-set server only in `data["url"]` of its entry;
    without it, it uses (or starts) go2rtc on `localhost:1984`. The go2rtc
    built into Home Assistant cannot serve `stream.mp4` and is not considered.
    """
    for entry in config_entries.async_entries(WEBRTC_DOMAIN):
        if (url := entry.data.get(CONF_URL)) and (cleaned := clean_url(url)):
            return cleaned
    return DEFAULT_URL


async def check_server(session: aiohttp.ClientSession, base_url: str) -> None:
    """Check that go2rtc answers its stream list.

    The `/api/streams` response carries camera logins and passwords, so the
    body is never read into an error, a log or a result.

    Raises:
        Go2rtcError: go2rtc is unreachable or replied with an error.
    """
    try:
        async with session.get(
            f"{base_url}/api/streams", timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
        ) as response:
            if response.status != 200:
                raise Go2rtcError(f"HTTP {response.status}")
    except (TimeoutError, aiohttp.ClientError) as err:
        raise Go2rtcError(type(err).__name__) from None


# go2rtc media line: `audio, recvonly, PCMA/8000`; G.711 names → ffmpeg encoders.
_G711_ENCODERS = {"PCMA": "pcm_alaw", "PCMU": "pcm_mulaw"}


def parse_g711(info: Any) -> str | None:
    """Tell from go2rtc's stream info whether the camera sends G.711 audio.

    go2rtc delivers the first audio track a producer receives (`recvonly`);
    `sendonly` lines are the way back to a camera speaker and are skipped.

    Returns:
        The ffmpeg encoder that restores that audio (`pcm_alaw`, `pcm_mulaw`), or `None`.
    """
    # Any other shape (a broken or future go2rtc) means no G.711: an exception here
    # would escape the recording's save and leave it without an end.
    if not isinstance(info, dict) or not isinstance(producers := info.get("producers"), list):
        return None
    for producer in producers:
        medias = producer.get("medias") if isinstance(producer, dict) else None
        for media in medias if isinstance(medias, list) else []:
            # Only go2rtc's string form: `str()` of a deeply nested item can raise.
            if not isinstance(media, str):
                continue
            parts = [part.strip() for part in media.split(",")]
            if len(parts) < 3 or parts[0] != "audio" or parts[1] != "recvonly":
                continue
            return _G711_ENCODERS.get(parts[2].partition("/")[0].upper())
    return None


async def source_g711(session: aiohttp.ClientSession, base_url: str, name: str) -> str | None:
    """Ask go2rtc whether the stream `name` gets G.711 audio from the camera.

    go2rtc can't put G.711 into MP4 and sends it as FLAC; this tells the recording
    to restore the original. The reply carries camera passwords, so only the
    media lines are read and nothing of it is logged.

    Returns:
        As `parse_g711`; `None` also when go2rtc does not answer.
    """
    try:
        async with session.get(
            f"{base_url}/api/streams",
            params={"src": name},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as response:
            if response.status != 200:
                return None
            info = await response.json(content_type=None)
    # RecursionError: JSON nested too deep for the parser.
    except TimeoutError, aiohttp.ClientError, ValueError, RecursionError:
        return None
    return parse_g711(info)


def stream_url(base_url: str, src: str, name: str | None = None) -> str:
    """Build the `stream.mp4` request for a source.

    `mp4=all` is always set: it lets go2rtc pass AAC, PCM, Opus and MP3 audio
    instead of dropping it. `duration` is never set: the recording ends when
    the request is closed. With `name`, go2rtc reuses or creates the stream of
    that name for a URL source, the same way WebRTC Camera does for an entity.
    """
    query = {"src": src, "mp4": "all"}
    if name:
        query["name"] = name
    return f"{base_url}/api/stream.mp4?{urlencode(query)}"

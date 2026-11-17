"""Integration constants."""

from __future__ import annotations

DOMAIN = "speakeasy"

# WebSocket protocol the card checks before showing its record button.
PROTOCOL_VERSION = 1
FEATURES = ["record"]

# WebRTC Camera falls back to this address when its own entry has no URL.
DEFAULT_URL = "http://localhost:1984"
WEBRTC_DOMAIN = "webrtc"

CONF_FOLDER = "folder"
CONF_MIN_FREE_GB = "min_free_gb"
CONF_KEEP_DAYS = "keep_days"
CONF_G711_AAC = "g711_aac"

DEFAULT_FOLDER = "speakeasy"
DEFAULT_MIN_FREE_GB = 2
DEFAULT_KEEP_DAYS = 0
# Browsers on Chromium do not play MOV, and Apple players drop G.711 from MP4.
DEFAULT_G711_AAC = True

MAX_MIN_FREE_GB = 1000
MAX_KEEP_DAYS = 3650

# Media source key that Home Assistant maps to `/media` in a container install.
MEDIA_SOURCE_KEY = "local"

PART_SUFFIX = ".part"

EVENT_RECORDING_FINISHED = f"{DOMAIN}_recording_finished"

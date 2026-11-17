# Contributing

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests need `ffmpeg` on `PATH`: they generate short fragmented MP4 samples and serve them from a fake go2rtc.

## Card protocol

The card talks to the integration over these Home Assistant WebSocket commands; any logged-in user may call them.

| Command | Reply |
|---|---|
| `speakeasy/info` | `{version, protocol, features, configured}`. `unknown_command`: the integration is not loaded (not installed, no enabled entry since Home Assistant started, or still starting). `configured: false`: the entry was removed, disabled or unloaded, failed to set up, or a leftover `speakeasy:` YAML key loaded the integration without one; start and stop then fail with `not_configured`, the subscription works. |
| `speakeasy/record/start` `{source: {url \| entity, server?, record_url?}}` | `{id, started_at}` at the first data. `entity` comes back lowercased in every reply and event, `url` trimmed, `server` as sent. `server` other than the configured go2rtc address fails with `invalid_source`. |
| `speakeasy/record/stop` `{recording_id}` | `{id, filename, media_content_id, duration, size, reason, finalized}` once the file is saved. `size` is in bytes, `0` when the file could not be read. |
| `speakeasy/record/subscribe` | Recording events, while the integration is loaded, with an entry or not; stays open while the entry is removed, disabled, reloaded or added. |

The subscription sends `{event, ...payload}`. First comes `list`, again whenever `configured` changes, and between
them `started`, `stopping`, `stopped` and `failed`. An unload sends `list` with `configured: false`, then finishes its
recordings: running ones get `stopping` and `stopped` (or `failed`) with reason `shutdown` (`error` when the last write
fails; one already ending keeps its reason), saving ones only `stopped` (or `failed`). A recording that ended while no
subscription was open is in the next `list` with `status` `saving` while its file is saved, followed by its `stopped`
(or `failed`); once saved, it is not listed.

| Event | Payload |
|---|---|
| `list` | `{configured, recordings}`: every recording running or saving, oldest first. Replaces the previous list. |
| `started` | `{recording}`: go2rtc sent the first data. |
| `stopping` | `{recording, reason}`: the recording ended, its file is being saved. |
| `stopped` | `{recording, result}`: the file is saved; `result` is the `speakeasy/record/stop` reply. |
| `failed` | `{recording, reason, kept}`: the file was not saved. `kept: false`: nothing was saved (the file could not be written, `reason` is then usually `error`, or was removed during the recording). `kept: true`: the file stays as `.part` and is saved at the next start, if it is in the recordings folder set then; `speakeasy_recording_finished` fires once it is. |

A recording, in `list` and in every event, is `{id, name, source, started_at, status, reason}`:

| Field | Description |
|---|---|
| `id`, `name` | Recording id and camera name. |
| `source` | `{url}` or `{entity}`, plus `server` when the start had one, as the start sent it (the entity lowercased). |
| `started_at` | ISO time of the first data, with the offset. |
| `status` | `recording`, or `saving` once it ended and ffmpeg is saving the file. |
| `reason` | `null` while `recording`; then why it ended: `user`, `disk_low`, `stalled`, `stream_ended`, `shutdown` or `error`. |

Error codes. `speakeasy/record/start`: `not_configured`, `invalid_source`, `no_stream_source`, `media_dir_missing`,
`disk_low`, `stream_not_found`, `camera_unavailable`, `go2rtc_error`, `folder_not_writable` (the folder or file cannot
be created at the first data), `stopped` (stopped before the first data by an action, an unload or a Home Assistant
stop). `speakeasy/record/stop`: `not_configured`, `not_found`, `no_data` (as `failed` with `kept: false`), `data_kept`
(as `failed` with `kept: true`). A malformed message (both `url` and `entity`, an invalid entity id) gets Home
Assistant's `invalid_format`. Files are downloaded through `media_source/resolve_media`.

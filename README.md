# Speakeasy

A Home Assistant integration that records camera streams from [go2rtc](https://github.com/AlexxIT/go2rtc) to Home
Assistant media. It runs the **Record** button of the [Speakeasy card](https://github.com/petrouv/ha-speakeasy-card)
and offers actions and an event for automations.

Recordings run on the Home Assistant server: closing the page, switching the stream quality or losing the phone's
connection does not stop them.

## Requirements

- Home Assistant 2026.10 or newer.
- go2rtc with your camera streams, reachable over HTTP from Home Assistant, with the API open (username and password
  are not supported yet). Usually this is the go2rtc of the [WebRTC Camera](https://github.com/AlexxIT/WebRTC)
  integration. The go2rtc built into Home Assistant Core does not work: it listens on a private socket and does not
  know your streams.
- The `ffmpeg` binary, included in Home Assistant OS and Container. Without it a recording stays the raw stream
  (`finalized: false`).

## Installation

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=petrouv&repository=ha-speakeasy&category=integration)

Without the button: search HACS for **Speakeasy**, download it and restart Home Assistant. Manually: copy `custom_components/speakeasy` to
`config/custom_components/` and restart.

The **Record** button needs the [Speakeasy card](https://github.com/petrouv/ha-speakeasy-card) 0.2.0 or newer.

## Configuration

**Settings → Devices & services → Add integration → Speakeasy**, then the go2rtc address. The form suggests the server
set in WebRTC Camera, or its fallback `http://localhost:1984`; an address copied from the go2rtc web UI works as is.
Recordings always come from this go2rtc: a card or an action may pass `server` only as this same address (a card
configured with another `server` cannot record), since Home Assistant sends go2rtc the camera's stream source with its
credentials.

**Reconfigure** changes the address. It reloads the integration, which ends recordings in progress.

**Configure** sets:

| Option | Default | Description |
|---|---|---|
| Folder | `speakeasy` | Folder inside the Home Assistant media folder (`/media` on Home Assistant OS); nested folders like `cameras/door` work. Letters, digits, `_` and `-` only. |
| Minimum free space, GB | `2` | Below it a recording does not start, or stops. |
| Keep recordings, days | `0` | Older `.mp4` and `.mov` files directly in the folder are deleted at each integration start and once a day. `0` keeps them forever. |
| Save G.711 sound as AAC | on | G.711 sound is encoded to AAC and the file is MP4, which plays everywhere. Off keeps the camera's own G.711 bytes in MOV: Apple players and VLC play it, browsers on Chromium (Chrome, Edge, Brave) do not. |

Options apply at once and do not interrupt recordings.

## Setting up camera streams

Speakeasy saves a stream as go2rtc delivers it, without transcoding, so the go2rtc stream decides what is in the file.
To see what a camera sends, open the go2rtc web UI or run `curl "http://<go2rtc>:1984/api/streams?src=<stream>"`:
`recvonly` tracks come from the camera (`video, recvonly, H264`, `audio, recvonly, MPEG4-GENERIC/16000` for AAC),
`sendonly` ones go to its speaker.

**G.711 or Opus sound** (browsers play it over WebRTC): use the camera's stream as is.

```yaml
streams:
  living_room:
    - rtsp://user:password@camera:554/stream1
```

**AAC sound.** WebRTC has no AAC, so add Opus made from the same stream. The browser gets Opus, the recording the
original AAC, and the camera serves one connection:

```yaml
streams:
  front_door:
    - rtsp://user:password@camera:554/ch1
    - ffmpeg:front_door#audio=opus
```

Avoid the common `rtsp://…#media=video` plus `ffmpeg:rtsp://…#audio=opus` pair: it opens a second connection to the
camera, the recording gets the transcoded Opus, and cameras with a weak CPU may send damaged frames.

**A separate speaker source** (an `exec:` bridge, for example): add `#backchannel=0` to the RTSP source, so go2rtc does
not also open the camera's own backchannel:

```yaml
streams:
  doorbell:
    - rtsp://user:password@doorbell:8554/ch1#backchannel=0
    - ffmpeg:doorbell#audio=opus
    - exec:/path/to/bridge doorbell#backchannel=1
```

**Quality switch.** One stream per camera channel (`stream1` / `stream2`, `subtype=0` / `subtype=1`), listed in the
card's `streams`. The card records the first one, whichever is shown.

| go2rtc delivers | File |
|---|---|
| H.264 or H.265 video | copied; H.265 is tagged `hvc1`, so QuickTime and iOS play it |
| AAC or Opus sound | `.mp4`, copied |
| G.711 sound (PCMA, PCMU, as Tapo cameras send it) | `.mp4` with AAC (8 kHz, 32 kbit/s); with **Save G.711 sound as AAC** off, `.mov` with the camera's own bytes (go2rtc sends it as FLAC, twice the size; it is turned back) |
| other PCM sound | `.mp4` with FLAC, as go2rtc sends it |

Some cameras gate quiet sound to digital silence when their microphone noise reduction is on. If a recording is silent
while the room is not, turn that setting off.

## How a recording works

- **One recording per source.** A source is `url` (a go2rtc stream name or a source address such as
  `rtsp://…`) or a camera entity, with its `server`. Cards and
  actions naming the same source share one recording on any device, and starting it again returns the running one.
  Spaces around values (in `url` and `record_url` too), trailing slashes in `server` and the case of an entity id are
  ignored; no `server` and the configured address written out are different sources. Any spelling of the configured
  address is accepted as `server`, but only spaces and trailing slashes are ignored when matching, so write it exactly
  as the card does. A start right after the
  recording ends waits for its file to close and begins a new one.
- **What is recorded.** A stream name as is; a camera entity from its stream source, which need not be in go2rtc.
  `record_url` records another source instead.
- **Camera connection.** A recording is one more go2rtc consumer: while the camera is watched, it shares that
  connection.
- **Start.** When go2rtc cannot open the camera (HTTP 5xx: rebooting, waking up, briefly offline), the start is retried
  for 10 seconds, then fails with `camera_unavailable` and go2rtc's reason. A camera in a sleep or privacy mode must be
  woken up first. The start time, the file name and the duration count from the first data.
- **File.** The stream is written to `<camera>_<YYYYMMDD_HHMMSS>.mp4.part` (local time of the first data). When the
  recording ends, ffmpeg turns it into a regular MP4 (MOV for G.711 kept as is) with the index in front. A file ffmpeg fails on
  stays `.mp4` in its streaming form.
- **No time limit.** A recording stops by itself, with the file saved, for these reasons:
  - `disk_low`: free space dropped below the minimum (checked every 10 seconds);
  - `stalled`: go2rtc sent no data for 30 seconds;
  - `stream_ended`: go2rtc ended the stream;
  - `error`: the file cannot be written, or another error (logged);
  - `shutdown`: Home Assistant stops (recordings get up to 20 seconds to save), or the integration is reloaded,
    reconfigured, disabled or removed (the unload waits for the files). A start without data yet fails with `stopped`.

  A stop that arrives while such a recording writes its last data does not change its reason.
- **Reminder.** For each hour a recording runs, a persistent notification names the camera.
- **Leftovers.** `.part` files left by a crash, a power cut, a cut save or a failed save are finished each time the
  integration starts (Home Assistant start, Reload, Reconfigure, re-enabling), if they are in the folder set then. A
  G.711 recording finished this way stays MP4 with FLAC, since the camera's codec is no longer known.

Recordings are in **Media → My media → <folder>**, where they can be played, downloaded and deleted.

## Entities

Device **Speakeasy** with `binary_sensor.speakeasy_recording`: `on` while any recording runs or its file is being
saved. The `recordings` attribute lists them: id, camera name, start time, status (`recording` or `saving`). Sources
are left out, and a `url` source is named only by its scheme, host and port, because attributes go to the history
database.

## Actions

Each returns a response when called with one.

| Action | Fields | Description |
|---|---|---|
| `speakeasy.record_start` | `url` or `entity_id`, optional `server` (only the configured address), `record_url` | Starts a recording, or returns the running one. Replies at the first data: `{id, started_at}`. Fails with `camera_not_found` for an entity Home Assistant does not know. |
| `speakeasy.record_stop` | `url` or `entity_id`, optional `server` (as given at the start; another address finds nothing: `not_found`) | Stops the source's recording and replies at once with `{id}`: the recording whose outcome is pending (running or ending, else the newest one saving). The file is saved in the background, then `speakeasy_recording_finished` fires. A start still waiting for data is cancelled with `stopped`; when that is all the source has, `id` is `null`. Fails with `not_found` when the source has nothing to stop. |
| `speakeasy.record_stop_all` | — | Stops every recording and every start waiting for data. Replies at once with `{ids}`: every recording whose outcome is pending, oldest first, including ones already ending or saving. |

## Event

`speakeasy_recording_finished` fires when a file is saved, whatever ended the recording. A file that fails to save
fires none, unless its data was kept and is saved at a later start.

| Field | Description |
|---|---|
| `id`, `name` | Recording id and camera name: the entity's name, or for a `url` its scheme, host and port without login, path or query (`rtsp://192.168.1.20:554`), so channels of one recorder share a name; an `exec:` or `echo:` source keeps only the program. |
| `entity_id` / `url` | The source; the other one is `null`. An RTSP address, or any address with a login, is replaced whole by `rtsp://***` (`scheme://***`), so use `name` to tell such cameras apart; a stream name is kept as is. The arguments of `exec:` and `echo:` are masked. |
| `path`, `filename` | The file, named after the camera and the start time: `front_door_20261112_093000.mp4` for `camera.front_door`. A `url` gives its stream name, or `camera` for an address. |
| `media_content_id` | The file in Home Assistant media, for `media_player.play_media` or a download link. |
| `duration` | Seconds from the first data to the last, so a stall's silent 30 seconds are not counted. |
| `reason` | `user`, `disk_low`, `stalled`, `stream_ended`, `shutdown` or `error`. |
| `finalized` | `false` if ffmpeg failed or could not run and the file is the raw stream; `error` then says why. |

For a `.part` file finished at startup `id`, `name`, `entity_id` and `url` are absent, `duration` is `0` and `reason`
is `null`.

Example: send the door camera's recordings to Telegram.

```yaml
automation:
  - alias: Door recording to Telegram
    triggers:
      - trigger: event
        event_type: speakeasy_recording_finished
        event_data:
          entity_id: camera.front_door
    actions:
      - action: telegram_bot.send_video
        data:
          file: "{{ trigger.event.data.path }}"
          caption: "{{ trigger.event.data.filename }}"
```

The `media` folder must be in `allowlist_external_dirs` for `telegram_bot` to read the file.

## Limitations

- A recording of a stream nobody watches opens its own connection to the camera. A camera that allows few connections
  may refuse it when other systems hold the rest.
- A file plays only where its codecs do: H.265 does not play in some browsers, Opus may not play in QuickTime or the iOS
  Photos app, and browsers on Chromium (Chrome, Edge, Brave) do not play `.mov` at all, neither in Media nor by a link.

## Security

- Recording only reads the stream: a `stream.mp4` consumer cannot send audio, so a doorbell speaker never plays
  anything.
- Camera addresses (RTSP or with a login, replaced whole) and the arguments of `exec:` and `echo:` sources are masked in the log and the event
  and never written to entity attributes. The card gets back the source it sent, to match recordings to cards.
- Any logged-in user can start and stop recordings and download them through Home Assistant media.

## Development

The development setup and the WebSocket protocol between the card and the integration are in
[CONTRIBUTING.md](CONTRIBUTING.md).

# GoXLR Mini on Linux — Research Notes

This document captures everything discovered about running and controlling the TC-Helicon GoXLR Mini on Linux, including USB audio layout, the control protocol, available tooling, and the integration plan for briclite.

---

## 1. Hardware Overview

The TC-Helicon GoXLR Mini is a broadcast audio mixer with:
- XLR mic input (with phantom power, hardware gate, compressor, EQ, de-esser)
- 3.5mm headphone output
- 3.5mm line input
- USB-C connection to host (power + audio + control)
- 4 manual faders (A, B, C, D) — **not** motorized (the full GoXLR has motorized faders)
- Internal DSP mixing matrix: any input can be routed to any output bus

USB IDs: `VID=1220`, `PID=8fe4` (TC Electronic/TC-Helicon)

---

## 2. Linux Driver Support

**No official Linux support from TC-Helicon.** However:

- The **USB audio class driver** (`snd-usb-audio`) handles the audio interface automatically — no custom driver needed.
- The **control interface** is vendor-specific USB HID/bulk commands, not standard UVC or UAC control. This requires third-party software.
- The GoXLR Mini enumerates cleanly on Linux as soon as it is plugged in (kernel 5.x+).

### ALSA Registration

When plugged in and enumerated successfully:

```
card N: GoXLRMini [GoXLRMini], device 0: USB Audio [USB Audio]
```

Check with `cat /proc/asound/cards`. The card number `N` depends on plug order; use the name form `hw:GoXLRMini,0` in config to be stable across reboots.

### Common Enumeration Issue

On first plug the device may fail to enumerate (`error -71`, `unable to enumerate USB device`). This appears to be a USB hub power/negotiation issue. Unplug and replug to reset — the device then enumerates cleanly. Confirmed on PSA300 hardware.

---

## 3. USB Audio Channel Layout

The device presents a **single multichannel USB audio interface** at 48000 Hz, 24-bit (S32_LE interleaved).

### Playback (host → GoXLR): 10 channels

| ALSA channels | GoXLR virtual input | Notes |
|---|---|---|
| 0–1 | System | Windows system audio |
| 2–3 | Game | Game audio bus |
| 4–5 | Chat | Chat/comms bus |
| 6–7 | Music | Music/media bus |
| 8–9 | Sample | Sampler/SFX bus |

Each pair is a stereo input that appears on the GoXLR's internal routing matrix and can be assigned to a fader.

### Capture (GoXLR → host): 21 channels (possibly 23 with newer firmware)

| ALSA channels | GoXLR output | Notes |
|---|---|---|
| 0–1 | **Broadcast Mix** | The hardware mix used as the base of codec TX. In clean-news mode it excludes Game, which the PSA adds separately. |
| 2–3 | Chat Mic | Processed mic signal (gate, comp, EQ applied) |
| 4–15 | Mixed outputs | Headphone mix and other monitoring buses |
| 16–17 | Sample Input | Sampler input |
| 18–20 | Additional | Firmware-dependent |

**Key fact:** Channels 0–1 of capture are the Broadcast Mix. Normally this is
the codec TX source; in clean-news mode it is the base source and the PSA
adds the decoded RX-right signal before encoding.

### GStreamer caps (confirmed on PSA300)

```
audio/x-raw, format=S32LE, layout=interleaved, rate=48000, channels=21, channel-mask=0x0
```

The channel-mask is `0x0` because the GoXLR assigns no standard speaker positions to its bus outputs.

---

## 4. Routing the Broadcast Mix to GStreamer (TX) — implemented

Because the device presents 21 channels, you cannot simply use `alsasrc device=hw:GoXLRMini,0` and get stereo — you get 21 channels.

**Implemented approach: ALSA `route` virtual device**

`GoXLRInterface.start()` writes the following to `~/.asoundrc`:

```
pcm.goxlr_broadcast {
    type route
    slave {
        pcm "hw:GoXLRMini,0"
        channels 21
    }
    ttable {
        0.0 1.0
        1.1 1.0
    }
}
```

GStreamer then uses `alsasrc device=goxlr_broadcast` and sees a normal stereo 48 kHz S32LE source. **Confirmed working on PSA300.**

---

## 5. Routing Codec RX and Second Mic to GoXLR Inputs (RX) — implemented

The PSA300 writes two independent audio sources into the GoXLR's 10-channel USB playback stream simultaneously. GStreamer's `audiomixer` element combines them before the single `alsasink`.

### USB playback channel assignment

| ALSA ch | GoXLR bus | Source | Fader | Routing |
|---|---|---|---|---|
| 0–1 | System | — | — | unused |
| 2–3 | Game | Codec RX Right (News feed) | C | LineOut, Headphones; clean PSA branch for TX |
| 4–5 | Chat | Behringer capture (Guest mic) | B | BroadcastMix, LineOut, Headphones |
| 6–7 | Music | Codec RX Left (Studio return) | — | none (PFL-only via Bleep button) |
| 8–9 | Sample | — | — | unused |

The studio return (RX Left) is on the Music bus with no fader — it cannot be mixed into the broadcast by accident. It is only audible in headphones when the operator activates Pre-Fade Listen via the Bleep button (see section 10).

### ALSA route plugin does NOT work for 10-channel playback

The intuitive approach — an ALSA `route` plugin mapping 2 channels to `hw:GoXLRMini,0` at 10 channels — silently fails. The device presents this to ALSA:

```
FORMAT:   S32_LE   (only)
CHANNELS: [1 89478485]   (anomalous range — route plugin constraint propagation issue)
```

Even with explicit `format=S32LE` in GStreamer, audio does not reach the hardware. Root cause: the ALSA `route` plugin does not correctly handle the 2→10 channel asymmetry on this device.

### GStreamer audiomixmatrix + audiomixer — confirmed working, but audiomixer needs explicit latency tuning

Two `audiomixmatrix` elements (one per source) each expand their 2-channel input to 10 channels, mapping only to their assigned GoXLR bus slots. GStreamer's `audiomixer` then sums the two 10-channel streams before writing to the GoXLR.

**Update 2026-09-09:** `audiomixer`'s automatic latency query always fails on this pipeline (`WARN aggregator: <goxlr_mix> Latency query failed`) — combining a manually-fed `appsrc` branch (codec RX, PTS assigned in Python) with a live `alsasrc` branch (Behringer) never lets it negotiate a value, so it assumes 0. Left untuned, this caused an audible echo, a pitch-stretching artifact, and — confusingly — complete silent stalls of the mixer's output that reproduced even with the Behringer *unplugged* (i.e. with `audiomixer` present but only one pad connected), while the RX `level` meter upstream kept reporting correctly and nothing was posted to the GStreamer bus. None of `lost`/`late`/`jitter` in briclite's own telemetry moved during these stalls — this is local to the element, not RTP loss.

**Fix, now implemented in `briclite/interfaces/goxlr.py`:**
1. `GoXLRInterface.rx_sink_bin()` only inserts `audiomixer` at all when `extra_rx_source_bins()` will actually return something (both key off the same `GoXLRInterface.behringer_available()` check, so they always agree) — with the Behringer absent, the pipeline goes straight from the RX matrix to `alsasink`, no aggregator in the path at all.
2. When the Behringer *is* present, the mixer gets an explicit latency budget instead of relying on the broken auto-negotiation: `audiomixer name=goxlr_mix ignore-inactive-pads=true min-upstream-latency=200000000 latency=200000000` (200ms, matching the jitter buffer). See `_GOXLR_MIX_PROPS` in `goxlr.py`.

Verified live on the PSA300 with the Behringer both absent and present after this fix — echo and stretching gone in both cases. A separate residual glitch persisted and has since been isolated to native ALSA/USB GoXLR playback, not clock drift or the application. See `CURRENT-STATUS.md` and the section below.

**Codec RX matrix** (2-in → 10-out, rows = output ch, cols = RX L/R):
```
ch 0–1  System  <0.0,0.0> <0.0,0.0>  — silent
ch 2–3  Game    <0.0,1.0> <0.0,1.0>  — RX Right (News feed)
ch 4–5  Chat    <0.0,0.0> <0.0,0.0>  — silent (Behringer fills these)
ch 6–7  Music   <1.0,0.0> <1.0,0.0>  — RX Left (Studio return)
ch 8–9  Sample  <0.0,0.0> <0.0,0.0>  — silent
```

**Behringer matrix** (2-in → 10-out, rows = output ch, cols = Behringer L/R):
```
ch 0–1  System  <0.0,0.0> <0.0,0.0>  — silent
ch 2–3  Game    <0.0,0.0> <0.0,0.0>  — silent
ch 4–5  Chat    <1.0,0.0> <0.0,1.0>  — Behringer L→Chat L, R→Chat R
ch 6–7  Music   <0.0,0.0> <0.0,0.0>  — silent
ch 8–9  Sample  <0.0,0.0> <0.0,0.0>  — silent
```

Full GStreamer pipeline fragment:
```
# Codec RX path (fed by jitter buffer)
... ! audiomixmatrix in-channels=2 out-channels=10 matrix="<RX matrix>" !
audiomixer name=goxlr_mix !
audioconvert ! audio/x-raw,format=S32LE !
alsasink device=hw:GoXLRMini,0 sync=false

# Behringer capture (second mic), connects to the same audiomixer
alsasrc device=hw:CODEC,0 ! audioconvert !
audioresample ! audio/x-raw,rate=48000,channels=2 !
audiomixmatrix in-channels=2 out-channels=10 matrix="<Behringer matrix>" !
goxlr_mix.
```

The `audioconvert ! audio/x-raw,format=S32LE` step is required — the GoXLR playback interface accepts **S32LE only** and will silently discard audio in any other format.

---

## 6. Control Protocol

### What it is

The GoXLR Mini's mixing matrix, fader assignments, mic settings, effects, and routing are controlled via **vendor-specific USB commands** — not standard audio class controls. These have been fully reverse-engineered by the [GoXLR-on-Linux project](https://github.com/GoXLR-on-Linux/goxlr-utility).

Message format (from the Wireshark dissector):
- 16-byte header: command ID (12 bits) + subcommand ID (12 bits) + body length (16 bits) + command index (16 bits)
- Body: command-specific data

A [Wireshark plugin](https://github.com/GoXLR-on-Linux/goxlr-utility/blob/main/goxlr-wireshark-plugin.lua) is available for protocol analysis.

### Do not implement USB control directly

The USB protocol is complex and fully implemented in Rust by `goxlr-utility`. Use the daemon's IPC instead.

---

## 7. goxlr-utility Daemon

**Repository:** https://github.com/GoXLR-on-Linux/goxlr-utility  
**Language:** Rust  
**Licence:** MIT

The daemon handles all USB communication and exposes a clean IPC API. It must be running for any control commands to work. Audio I/O (ALSA) works independently of the daemon.

### Installation on Ubuntu 24.04

Pre-built binaries are available from the GitHub releases page. Alternatively, build from source with `cargo build --release`. A systemd unit should be installed to start the daemon at boot.

```bash
# Check if running
systemctl status goxlr-daemon

# View daemon logs
journalctl -u goxlr-daemon -f
```

The daemon auto-detects the GoXLR Mini on USB. It saves profiles to `~/.config/goxlr-utility/`.

### Headless PSA300 web UI (deployed 2026-09-11)

The daemon serves the GoXLR Utility UI as well as its API.  On the headless
PSA300, the operator opens it remotely from the authorised Lenovo at:

```
http://172.16.10.213:14564/
```

The daemon normally binds HTTP to `localhost`.  On the PSA it is overridden by
`/etc/systemd/system/goxlr-daemon.service.d/network-ui.conf` to add
`--http-bind-address 0.0.0.0`; UFW allows TCP/14564 on `codec0` only from
`172.16.10.212`.  Binding specifically to `172.16.10.213` is **wrong** for
this application: Briclite's event subscriber uses
`ws://localhost:14564/api/websocket`, so it must retain a localhost-reachable
listener.  Keep the all-interface bind and firewall source restriction as a
pair.  The HTTP control interface has no suitable public-network protection;
never expose it outside the management LAN.

---

## 8. Daemon IPC API

The daemon exposes three equivalent interfaces:

| Method | Address | Notes |
|---|---|---|
| Unix socket | `/tmp/goxlr.socket` | Preferred for local use |
| HTTP | `POST http://localhost:14564/api/command` | REST-style |
| WebSocket | `ws://localhost:14564/api/websocket` | Real-time event stream |

### Message format (socket/HTTP)

```
[4 bytes, big-endian uint32: message length][JSON payload]
```

### Request structure

```json
{
  "Command": [
    "<device_serial>",
    { "<CommandName>": <args> }
  ]
}
```

The device serial is returned in the status response on connection. With a single device, it can be retrieved once at startup and cached.

### Key commands

**Get status (returns device serial and full state):**
```json
{ "GetStatus": null }
```

**Assign a fader:**
```json
{ "Command": ["<serial>", { "SetFader": ["A", "Mic"] }] }
```
Fader positions: `A`, `B`, `C`, `D`  
Channel sources: `Mic`, `Chat`, `Music`, `Game`, `System`, `Sample`, `LineIn`, `Headphones`

**Set routing matrix entry (route a source to an output bus):**
```json
{ "Command": ["<serial>", { "SetRouter": ["Game", "BroadcastMix", true] }] }
```
This enables/disables a crosspoint in the routing matrix.

**Set channel volume:**
```json
{ "Command": ["<serial>", { "SetVolume": ["Game", 127] }] }
```
Volume range: 0–255. Applies globally to that channel (affects all output buses it is routed to).

**Set fader mute function:**
```json
{ "Command": ["<serial>", { "SetFaderMuteFunction": ["A", "All"] }] }
```
Valid values: `All`, `ToStream`, `ToVoiceChat`, `ToPhones`, `ToLineOut`, `ToStream2`, `ToStreams`.  
`ToStream2` is useful as a no-op target (mutes to an unused bus) when the mute button is being repurposed in software.

**Set fader LED colour:**
```json
{ "Command": ["<serial>", { "SetFaderDisplayStyle": ["A", "Gradient"] }] }
{ "Command": ["<serial>", { "SetFaderColours": ["A", "00FFFF", "000000"] }] }
```

**Set button LED colour:**
```json
{ "Command": ["<serial>", { "SetButtonColours": ["Bleep", "FF8800", "000000"] }] }
```
Button names with LEDs on the Mini: `Fader1Mute`, `Fader2Mute`, `Fader3Mute`, `Fader4Mute`, `Bleep`, `Cough`.

### Real-time events (WebSocket)

The daemon emits JSON Patch messages whenever state changes (button press, fader move, USB hotplug). Subscribe to `ws://localhost:14564/api/websocket` to receive live updates.

Example — mute button press and release:
```json
{ "data": { "Patch": [{ "op": "replace", "path": "/mixers/<serial>/button_down/Fader3Mute", "value": true }] } }
{ "data": { "Patch": [{ "op": "replace", "path": "/mixers/<serial>/button_down/Fader3Mute", "value": false },
                       { "op": "replace", "path": "/mixers/<serial>/fader_status/C/mute_state", "value": "MutedToX" }] } }
```
The `button_down` patch fires on press; the `mute_state` change fires on release. React to `button_down: true` for immediate response.

---

## 9. Python Integration

### goxlr-py

**PyPI:** `pip install goxlr`  
**Repository:** https://github.com/samcarsonx/goxlr-py  
**Documentation:** https://goxlr.readthedocs.io/

An async Python wrapper around the daemon IPC. Example:

```python
from goxlr import GoXLR
from goxlr.types import Fader, Channel

async with GoXLR() as xlr:
    await xlr.set_fader(Fader.A, Channel.Mic)
    await xlr.set_router(Channel.Game, "BroadcastMix", True)
```

This requires the daemon to be running. Does not handle USB directly.

### Direct socket communication (alternative)

If goxlr-py proves insufficient, the IPC is simple enough to implement directly:

```python
import socket, struct, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/tmp/goxlr.socket")

def send_command(sock, payload: dict):
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)

def recv_response(sock) -> dict:
    length = struct.unpack(">I", sock.recv(4))[0]
    return json.loads(sock.recv(length))
```

---

## 10. briclite Implementation — as built

`briclite/interfaces/goxlr.py` implements `AudioInterface` (see `interfaces/base.py`).

### Auto-detection and hotplug

`GoXLRInterface.is_available()` returns `True` if `/tmp/goxlr.socket` exists, is connectable, and `GetStatus` returns at least one mixer. `main.py` calls this at startup and every 5 seconds from a background task. If presence changes, the interface is hot-swapped and any active pipeline is stopped. The web UI badge updates via WebSocket telemetry.

Config key `system.audio_interface` accepts `"auto"` (default), `"goxlr"`, or `"behringer"`. In auto mode, GoXLR takes priority.

### Fader layout (fixed, applied on every `start()`)

| Fader | GoXLR ch | Source | BroadcastMix | LineOut | Headphones | Notes |
|---|---|---|---|---|---|---|
| A | Mic | Main microphone (XLR) | ✓ | ✓ | ✓ | |
| B | Chat | Guest microphone (Behringer `hw:CODEC,0`) | ✓ | ✓ | ✓ | USB capture from Behringer |
| C | Game | News feed (codec RX Right) | PSA clean branch | ✓ | ✓ | GoXLR copy is monitor-only |
| D | LineIn | Music player (GoXLR 3.5mm line in) | ✓ | ✓ | ✓ | |
| — | Music | Studio return (codec RX Left) | — | PFL only | PFL only | No fader; Bleep PFL only |

The studio return is on the Music bus with no fader assigned. It is absent from both monitor outputs during normal operation and is never routed to BroadcastMix; Bleep/PFL solos it to Headphones and Line Out together.

### Clean news return mix (implemented 2026-09-11)

The Linux GoXLR path now deliberately routes `Game` to local `Headphones` and
`LineOut`, but **not** to the GoXLR `BroadcastMix`. `PipelineController` taps
RX Right immediately after AAC decode, before the GoXLR USB playback path,
and hands that dual-mono clean copy to an `interaudiosink`. A paired,
timestamped `interaudiosrc` feeds the PSA TX `audiomixer` alongside the
Game-excluded GoXLR BroadcastMix before AAC/RTP TX. This is deliberately not a
direct `appsrc` bridge or ALSA loopback: both introduced their own underflow or
aggregator stalls. Thus a GoXLR playback dropout remains audible locally but
cannot be encoded into the return feed.

Fader C/Game volume and mute WebSocket events also drive the PSA-side `volume`
element, so the physical fader and mute button retain control of both the
local monitor copy and the clean return copy. The clean branch has a fixed
`interaudiosrc` 200 ms latency plus the timestamp offset configured by
`goxlr.clean_news_return.alignment_delay_ms` (200 ms on PSA300).  On PSA300, an 801 Hz
intermittent-tone test against the known GoXLR headphone capture pair measured
the returned mix at -1 to +4 ms relative to the local headphone timing, so
the deployed 200 ms value is aligned. Re-measure after material changes to
the hardware/pipeline; the supported configuration range is 0--2000 ms. Set
`goxlr.clean_news_return.enabled` to `false` to roll back to the historical
all-GoXLR BroadcastMix path.

### Clean-return validation and diagnostic mode

The pre-change outgoing RTP capture had exact zero gaps approximately
100--123 ms long. A 34.86-second outgoing capture from the final inter-audio
design had no exact-zero interval of 20 ms or longer. This confirms that the
GoXLR playback glitches are not entering the encoded return, while they may
still be heard locally.

`goxlr.clean_news_return.timing_probe` is an opt-in, test-only facility. It
replaces the normal base capture with one 21-channel GoXLR capture handle,
extracts both Broadcast Mix and the confirmed Headphones channel pair 10/11,
and writes the latter as S32 raw audio to
`/tmp/briclite-headphone-timing.raw`. Leave it `false` in service. It exists
only to correlate a controlled tone with an outgoing RTP capture.

Do not substitute the earlier experimental handoffs: direct `appsrc` into the
TX mixer produced recurring roughly 400 ms silences; ALSA `snd-aloop` paths
underflowed or created periodic silent sections; and an `adder` variant did
not negotiate on PSA300. The timestamped inter-audio handoff is the accepted
implementation.

### Mute buttons

All four fader mute buttons use `SetFaderMuteFunction: "All"` — standard GoXLR hardware mute behaviour, muting the channel to all output buses. Cough is the exception: its mute target is `SetCoughMuteFunction: "ToStream2"` (an unused bus), since it's repurposed as the Studio Monitor Cut toggle rather than a real mute button — see below.

### Bleep button — Studio Return Pre-Fade Listen

The Bleep button is repurposed as a **PFL toggle** for the studio return (codec RX Left):

- **Press once:** Bleep LED goes orange. All fader sources (A–D) are removed from both Headphones and Line Out via `SetRouter`. Music bus is routed to both monitor outputs. Music channel volume is overridden to 255 via `SetVolume` (true pre-fade listen — audible regardless of internal volume state).
- **Press again:** Bleep LED returns to cyan. Headphones and Line Out are restored to the normal fader mix. Music volume is restored to its pre-PFL value.

The studio return (RX Left / Music bus) is never in the broadcast mix, so activating PFL has no on-air effect.

Implementation: `monitor_pfl()` subscribes to `ws://localhost:14564/api/websocket` as a background asyncio task. It watches for `button_down/Bleep: true` patches. IPC commands triggered by button presses run in a thread pool (`asyncio.to_thread`) to avoid blocking the FastAPI event loop.

### Cough button — Studio Monitor Cut (added 2026-09-13)

The Cough button is repurposed as an **arm/disarm toggle** for a feedback-safety feature, the same pattern as Bleep/PFL: while armed, Line Out is muted whenever fader A (Mic) or B (Chat) is open, so speakers set up near the mics can't feed back. Headphones are untouched.

- **Press to arm:** Cough LED goes green (or straight to red if a mic is already open). Nothing changes yet if both mics are closed.
- **A mic opens while armed:** `SetRouter` removes every source (A–D and, if PFL happens to be active, Music) from Line Out only. Cough LED goes red.
- **Both mics close:** Line Out is restored via `SetRouter`. Cough LED returns to green.
- **Press to disarm:** Line Out is restored if it was cut, and the LED returns to cyan.

"Open" is fader volume above a small threshold (5/255), not a strict >0 — a fader resting at the bottom of its travel was observed reading 1/255 rather than a clean 0, which caused an immediate false cut on arming before the threshold was added.

Cough normally has a real native function (hold to mute the mic to all outputs), unlike Bleep which had nothing to lose. `start()` now also sends `SetCoughMuteFunction: "ToStream2"` (an unused bus — the same no-op-target trick already used for the repurposed fader-mute buttons), so a quick press/toggle no longer also blips the mic. A genuine hold still forces a real mute regardless of this setting; that's GoXLR firmware behaviour, not something this code can override.

Both PFL and Studio Monitor Cut affect Line Out, so `_apply_monitor_routing()` computes one final desired routing state from both flags together rather than each toggling `SetRouter` independently — Studio Monitor Cut always wins on Line Out, Headphones always follow PFL alone regardless of the mic cut. Armed/disarmed state persists across a restart via the desired-link record (`monitor_cut_enabled`), exactly like `studio_pfl`.

### Bleep/Cough LED brightness (fixed 2026-09-13)

Both buttons looked very dim on hardware regardless of the colour sent via `SetButtonColours`. Root cause: a two-colour button's *off* appearance (native `mute_state: "Unmuted"`) is governed by a separate `SetButtonOffStyle` setting, independently of the colours themselves. Its default, `"Dimmed"`, dims colour_one whenever the button is "off" — and since neither Bleep nor Cough is ever actually put into a `"Muted"` state by this code (Bleep never was; Cough's native mute is neutralised to `ToStream2` above), both sat in the dimmed "off" appearance permanently.

Confirmed the enum's valid values empirically against the live daemon (`{"Command": [serial, {"SetButtonOffStyle": ["Cough", "..."]}]}`): `Dimmed`, `Colour2`, `DimmedColour2`. `"Colour2"` shows colour_two at full brightness in the off state instead of a dimmed colour_one. Fix, in `_apply_colours()` (once per `start()`) and `_set_bleep_colour()`/`_set_cough_colour()`: set `SetButtonOffStyle: [button, "Colour2"]`, and send the *same* colour for both `SetButtonColours` slots (previously colour_two was hardcoded `"000000"`, which — combined with the default off_style — was the actual source of the dimness, not the colour choice itself).

### Headphone volume

`POST /api/headphone_volume` with `{"pct": 0–100}` calls `SetVolume ["Headphones", N]` (N = pct × 255 / 100). The web UI shows a slider in GoXLR mode only. The current volume is read from `GetStatus` at startup and broadcast via WebSocket telemetry so the slider initialises to the actual hardware state.

### Speaker volume

In GoXLR mode, `POST /api/rx_volume` with `{"pct": 0–100}` calls `SetVolume ["LineOut", N]` (N = pct × 255 / 100), so the web UI's Speaker Volume slider controls the complete physical Line Out mix. The current hardware value is read back after connection and reported in WebSocket telemetry. In Behringer-only mode the same endpoint instead controls the receive pipeline's `rx_vol` software gain (`100%` is unity).

### `start()`
1. Reset the routing cache; preserve/reassert an in-process PFL state if present
2. Write `~/.asoundrc` with the `goxlr_broadcast` virtual capture device
3. Connect to daemon socket, retrieve device serial
4. Apply fader assignments via `SetFader`
5. Apply full routing matrix via `SetRouter` (all 8 sources × 5 outputs)
6. Restore mute buttons to `All` via `SetFaderMuteFunction`; retarget Cough to `ToStream2` via `SetCoughMuteFunction`
7. Apply cyan gradient lighting (faders + Bleep button) via `SetFaderColours` / `SetButtonColours`
8. Reconcile Headphones/Line Out routing against PFL and Studio Monitor Cut together, then set the Cough LED to match

**Config source of truth (discovered 2026-09-09):** none of this comes from a saved `.goxlr` profile — it's asserted in code and reapplied idempotently every time `start()` runs. `start()` is called by `PipelineController.start()` (`core/pipeline_manager.py:196`), which only runs on an actual connect: `POST /api/connect`, `_full_reconnect()` (RX channel-mode switch, Behringer hotplug), or the RX watchdog's auto-reconnect — **never** merely by `briclite.service`/the host starting. `main.py`'s `lifespan()` only constructs the `PipelineController`; it does not start it. So right after a boot or `systemctl restart`, before any connect has happened, the physical GoXLR reflects whatever the `goxlr-utility` daemon's on-disk profile last held — on the PSA300 this is a profile literally named `Default`, confirmed unchanged since 1 Jun on every boot, with a different fader mapping (A=Mic, B=Music, C=Chat, D=System) and Music volume 0. That's expected pre-`start()` state, not a bug — don't try to fix an unexpected fader layout/colours by loading a different saved profile file; just call `POST /api/connect` (or trigger any reconnect) and the correct layout above is reasserted from code. Inspect live state with `goxlr-client --status-json` (shows `fader_status`, `router`, and `levels.volumes` for the running mixer).

### `tx_source_bin()`
```python
# Clean mode: hardware Broadcast Mix (Game excluded) plus the timestamped
# pre-playback RX-Right branch into tx_program_mix/audiomixer.
# Disabled clean mode: historical goxlr_broadcast ALSA source only.
```

### `rx_sink_bin()` and `extra_rx_source_bins()`

`rx_sink_bin()` returns a GStreamer fragment that maps codec RX to Game (ch 2–3) and Music (ch 6–7) and writes to `hw:GoXLRMini,0`. **Updated 2026-09-09:** it only inserts a named `audiomixer` when `GoXLRInterface.behringer_available()` is true — see §5's "audiomixer needs explicit latency tuning" above for why. `extra_rx_source_bins()` (same `behringer_available()` check, so the two methods always agree) returns a second fragment that captures from the Behringer (`hw:CODEC,0`), maps it to Chat (ch 4–5), and connects to the mixer:

```python
# rx_sink_bin() — Behringer absent: no mixer, straight to alsasink
'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
'audiomixmatrix in-channels=2 out-channels=10 matrix="<RX matrix>" ! '
'audioconvert ! audio/x-raw,format=S32LE ! '
'alsasink device=hw:GoXLRMini,0 sync=false'

# rx_sink_bin() — Behringer present: mixer with explicit latency budget
'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
'audiomixmatrix in-channels=2 out-channels=10 matrix="<RX matrix>" ! '
'audiomixer name=goxlr_mix ignore-inactive-pads=true min-upstream-latency=200000000 latency=200000000 ! '
'audioconvert ! audio/x-raw,format=S32LE ! '
'alsasink device=hw:GoXLRMini,0 sync=false'

# extra_rx_source_bins()[0] — separate source, appended as a second pipeline branch, only when present
'alsasrc device=hw:CODEC,0 ! audioconvert ! '
'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
'audiomixmatrix in-channels=2 out-channels=10 matrix="<Behringer matrix>" ! '
'goxlr_mix.'
```

`pipeline_manager.py` appends each string from `extra_rx_source_bins()` to the RX pipeline string before calling `Gst.parse_launch()`. The Behringer source starts and stops with the codec pipeline, and is hot-pluggable — see `ARCHITECTURE.md` §9 "Behringer hotplug."

### IPC — direct socket, no third-party library

All daemon communication uses raw Unix socket with `[uint32 length][JSON]` framing. `goxlr-py` was not used — the raw approach has no dependencies and the protocol is simple. A new socket is opened per command; no persistent connection is maintained.

---

## 11. Routing Matrix Reference and Caveats

The GoXLR Mini's internal matrix connects inputs (rows) to output buses (columns). The daemon's `SetRouter` command toggles individual crosspoints.

**Input sources (IPC names):** `Microphone`, `Chat`, `Music`, `Game`, `Console`, `LineIn`, `System`, `Samples`  
**Output buses:** `Headphones`, `BroadcastMix`, `ChatMic`, `Sampler`, `LineOut`, `StreamMix2`

Note the naming difference: fader assignment uses `Mic` (short), but routing uses `Microphone` (full).

Each crosspoint is boolean (on/off). Volume is set separately with `SetVolume`. The full matrix state is returned by `GetStatus`.

### Mini-specific routing restrictions

The GoXLR **Mini** has a reduced routing matrix compared to the full GoXLR. Some crosspoints that appear in `GetStatus` cannot be set via `SetRouter` — the daemon returns `Invalid Route` if you try. Confirmed invalid crosspoints on the Mini:

- `Chat → ChatMic` — always fixed, cannot be toggled

When applying a routing matrix, **omit `ChatMic` from all rows** to avoid these errors. The Sampler output also has restrictions on some Mini firmware versions.

---

## 12. USB Topology on the PSA300

**Discovered 2026-09-09 on the PSA300.** The GoXLR Mini is a USB **high-speed** (480 Mbps) device. The Behringer UCA202 is **full-speed** (12 Mbps), and typical USB keyboard/mouse dongles are **low-speed** (1.5 Mbps). Putting all three behind the same external hub caused the Behringer and the keyboard/mouse to repeatedly reset (`dmesg`: `usb 1-1.1.1: reset full-speed USB device`, `usb 1-1.1.4: reset low-speed USB device`, recurring every few minutes) while the GoXLR itself stayed completely stable — a known class of problem with hub chipsets that struggle to reliably bridge a mix of speed classes on the same hub. Confirmed via `journalctl -u briclite`: the Behringer's ALSA capture threw `SNDRV_PCM_IOCTL_DELAY failed (-19): No such device` **886 times in 10 minutes** while wedged.

Later `lsusb -v` evidence superseded the initial physical-port interpretation: both rear sockets lead through the same internal 8-port, **single-TT** high-speed hub and the board has only one EHCI controller. The high-speed GoXLR does not itself consume the hub's Transaction Translator; full/low-speed devices do. The mouse and keyboard were therefore removed and the Behringer connected without the external hub, leaving it as the only TT client.

That improved the topology but did **not** eliminate Behringer resets: three spontaneous resets followed at 10:40:33, 11:28:27, and 16:38:05 UTC on 2026-09-10. Mixed-speed TT contention was therefore not the complete cause. The current experiment has transiently unbound the Behringer PCM2902's unused HID consumer-control interface (`1-1.1:1.3`) while leaving its three audio interfaces active; see `USB-AUDIO-GLITCH.md` for the hypothesis, limitations, and next steps.

## 13. Residual Native-ALSA Playback Glitch

**Investigated live 2026-09-09.** Occasional brief glitches/repeats persist in GoXLR playback even after removing the Behringer, `audiomixer`, `audiorate`, redundant sample-rate conversion, GStreamer, Python, AAC, and RTP. A native `aplay` tone to `hw:GoXLRMini,0` reproduces the fault while ALSA's 400 ms buffer remains nearly full and neither ALSA nor the kernel reports an xrun/USB error.

The GoXLR exposes asynchronous 10-channel playback on endpoint `0x08` with implicit feedback from its 21-channel capture endpoint `0x88`. This matches upstream [kernel bug 211211](https://bugzilla.kernel.org/show_bug.cgi?id=211211) and [alsa-lib issue 113](https://github.com/alsa-project/alsa-lib/issues/113): GoXLR output stutters under direct ALSA while capture stays clean, with implicit-feedback handling identified as the relevant kernel area.

The glitch is now objectively visible in a duplex hardware-loopback capture, not just by ear. On kernel `6.8.0-139`, a patched daemon polling every 50 ms produced 5 exact-zero gaps in five minutes (106.7–137.3 ms); with the daemon stopped, the same test produced 4 (118.9–132.1 ms). Slower polling therefore did not fix it and the daemon is not required. A newer GoXLR firmware or newer HWE kernel is a better next experiment than a 100 ms polling build. Exact method, artifacts, and live state are in `USB-AUDIO-GLITCH.md` and `CURRENT-STATUS.md`.

## 14. Useful References

| Resource | URL |
|---|---|
| goxlr-utility (Rust daemon) | https://github.com/GoXLR-on-Linux/goxlr-utility |
| goxlr-utility Wiki / API docs | https://github.com/GoXLR-on-Linux/goxlr-utility/wiki |
| goxlr-py (Python wrapper) | https://github.com/samcarsonx/goxlr-py |
| goxlr-py documentation | https://goxlr.readthedocs.io/ |
| ALSA UCM config (channel layout source) | https://github.com/alsa-project/alsa-ucm-conf |
| Wireshark protocol dissector | https://github.com/GoXLR-on-Linux/goxlr-utility/blob/main/goxlr-wireshark-plugin.lua |
| GoXLR-on-Linux org (broader project) | https://github.com/GoXLR-on-Linux |

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
| 0–1 | **Broadcast Mix** | The stream-ready mix (Stream Mix A). **Use this for codec TX.** |
| 2–3 | Chat Mic | Processed mic signal (gate, comp, EQ applied) |
| 4–15 | Mixed outputs | Headphone mix and other monitoring buses |
| 16–17 | Sample Input | Sampler input |
| 18–20 | Additional | Firmware-dependent |

**Key fact:** Channels 0–1 of capture are the Broadcast Mix — the output of whatever the operator has configured as their broadcast-ready mix. This is the correct source for the codec TX path.

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
| 2–3 | Game | Codec RX Right (News feed) | C | BroadcastMix, LineOut, Headphones |
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

Verified live on the PSA300 with the Behringer both absent and present after this fix — echo and stretching gone in both cases. A separate, smaller residual issue (a ~once-a-minute brief dropout, present even with this fixed) is tracked in `ARCHITECTURE.md` §10/§12 as clock-drift correction — the next planned fix.

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
| C | Game | News feed (codec RX Right) | ✓ | ✓ | ✓ | |
| D | LineIn | Music player (GoXLR 3.5mm line in) | ✓ | ✓ | ✓ | |
| — | Music | Studio return (codec RX Left) | — | — | PFL only | No fader; Bleep PFL only |

The studio return is on the Music bus with no fader assigned, so it is invisible to the routing matrix during normal operation. It is **not** routed to BroadcastMix or LineOut under any circumstances.

### Mute buttons

All four fader mute buttons use `SetFaderMuteFunction: "All"` — standard GoXLR hardware mute behaviour, muting the channel to all output buses.

### Bleep button — Studio Return Pre-Fade Listen

The Bleep button is repurposed as a **PFL toggle** for the studio return (codec RX Left):

- **Press once:** Bleep LED goes orange. All fader sources (A–D) are removed from headphones via `SetRouter`. Music bus is routed to headphones via `SetRouter`. Music channel volume is overridden to 255 via `SetVolume` (true pre-fade listen — audible regardless of internal volume state).
- **Press again:** Bleep LED returns to cyan. Headphone routing is restored to normal (all fader sources). Music volume is restored to its pre-PFL value.

The studio return (RX Left / Music bus) is never in the broadcast mix, so activating PFL has no on-air effect.

Implementation: `monitor_pfl()` subscribes to `ws://localhost:14564/api/websocket` as a background asyncio task. It watches for `button_down/Bleep: true` patches. IPC commands triggered by button presses run in a thread pool (`asyncio.to_thread`) to avoid blocking the FastAPI event loop.

### Headphone volume

`POST /api/headphone_volume` with `{"pct": 0–100}` calls `SetVolume ["Headphones", N]` (N = pct × 255 / 100). The web UI shows a slider in GoXLR mode only. The current volume is read from `GetStatus` at startup and broadcast via WebSocket telemetry so the slider initialises to the actual hardware state.

### `start()`
1. Reset PFL state
2. Write `~/.asoundrc` with the `goxlr_broadcast` virtual capture device
3. Connect to daemon socket, retrieve device serial
4. Apply fader assignments via `SetFader`
5. Apply full routing matrix via `SetRouter` (all 8 sources × 5 outputs)
6. Restore mute buttons to `All` via `SetFaderMuteFunction`
7. Apply cyan gradient lighting (faders + Bleep button) via `SetFaderColours` / `SetButtonColours`

### `tx_source_bin()`
```python
return "alsasrc device=goxlr_broadcast ! audioconvert ! audio/x-raw,rate=48000,channels=2"
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

## 12. USB Topology — Keep the GoXLR Off Mixed-Speed Hubs

**Discovered 2026-09-09 on the PSA300.** The GoXLR Mini is a USB **high-speed** (480 Mbps) device. The Behringer UCA202 is **full-speed** (12 Mbps), and typical USB keyboard/mouse dongles are **low-speed** (1.5 Mbps). Putting all three behind the same external hub caused the Behringer and the keyboard/mouse to repeatedly reset (`dmesg`: `usb 1-1.1.1: reset full-speed USB device`, `usb 1-1.1.4: reset low-speed USB device`, recurring every few minutes) while the GoXLR itself stayed completely stable — a known class of problem with hub chipsets that struggle to reliably bridge a mix of speed classes on the same hub. Confirmed via `journalctl -u briclite`: the Behringer's ALSA capture threw `SNDRV_PCM_IOCTL_DELAY failed (-19): No such device` **886 times in 10 minutes** while wedged.

**Fix:** plug the GoXLR directly into a host USB port (bypassing any hub entirely), and put the Behringer + any low-speed peripherals on a separate hub/port. On the PSA300 (only two physical ports: one USB3, one standard USB2), this means: GoXLR → USB3 port direct; external hub (Behringer + keyboard/mouse) → the other port. Confirmed with `lsusb -t` (GoXLR shows as a direct child of the root hub, no longer sharing a downstream hub with anything) and zero `No such device` errors over a full minute of monitoring afterward, versus hundreds per 10 minutes before.

This was a genuine hardware/wiring issue, not a code bug — but it directly caused the audiomixer instability in §5 to manifest far more severely when combined with the Behringer's second-mic branch (the flaky capture source was one of the aggregator's two input pads). Even after the audiomixer latency fix, keep the GoXLR isolated from mixed-speed devices as a matter of course on any new deployment.

## 13. Useful References

| Resource | URL |
|---|---|
| goxlr-utility (Rust daemon) | https://github.com/GoXLR-on-Linux/goxlr-utility |
| goxlr-utility Wiki / API docs | https://github.com/GoXLR-on-Linux/goxlr-utility/wiki |
| goxlr-py (Python wrapper) | https://github.com/samcarsonx/goxlr-py |
| goxlr-py documentation | https://goxlr.readthedocs.io/ |
| ALSA UCM config (channel layout source) | https://github.com/alsa-project/alsa-ucm-conf |
| Wireshark protocol dissector | https://github.com/GoXLR-on-Linux/goxlr-utility/blob/main/goxlr-wireshark-plugin.lua |
| GoXLR-on-Linux org (broader project) | https://github.com/GoXLR-on-Linux |

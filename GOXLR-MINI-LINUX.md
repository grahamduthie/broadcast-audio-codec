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

## 5. Routing Codec RX to GoXLR Inputs (RX) — implemented

The codec receives decoded stereo audio from the remote end. Each channel is routed to a separate GoXLR input bus so they appear on independent faders.

**Implemented assignment:**
- Codec RX Right → Game bus (playback ch 2–3) → Fader C → Broadcast Mix + LineOut + Headphones
- Codec RX Left  → Chat bus (playback ch 4–5) → Fader D → LineOut + Headphones only

### ALSA route plugin does NOT work for 10-channel playback

The intuitive approach — an ALSA `route` plugin mapping 2 channels to `hw:GoXLRMini,0` at 10 channels — silently fails. The device presents this to ALSA:

```
FORMAT:   S32_LE   (only)
CHANNELS: [1 89478485]   (anomalous range — route plugin constraint propagation issue)
```

Even with explicit `format=S32LE` in GStreamer, audio does not reach the hardware. Root cause: the ALSA `route` plugin does not correctly handle the 2→10 channel asymmetry on this device.

### GStreamer audiomixmatrix — confirmed working

Use GStreamer's `audiomixmatrix` to expand the 2-channel decoded stream to 10 channels before writing directly to `hw:GoXLRMini,0`:

```
audioresample ! audio/x-raw,rate=48000,channels=2 !
audiomixmatrix in-channels=2 out-channels=10 matrix="<matrix>" !
audioconvert ! audio/x-raw,format=S32LE !
alsasink device=hw:GoXLRMini,0 sync=false
```

The 10×2 matrix (rows = GoXLR output channels, cols = codec RX channels):

```
<<0.0,0.0>,   # ch 0  System L  — silent
 <0.0,0.0>,   # ch 1  System R  — silent
 <0.0,1.0>,   # ch 2  Game L    — RX Right
 <0.0,1.0>,   # ch 3  Game R    — RX Right
 <1.0,0.0>,   # ch 4  Chat L    — RX Left
 <1.0,0.0>,   # ch 5  Chat R    — RX Left
 <0.0,0.0>,   # ch 6  Music L   — silent
 <0.0,0.0>,   # ch 7  Music R   — silent
 <0.0,0.0>,   # ch 8  Sample L  — silent
 <0.0,0.0>>   # ch 9  Sample R  — silent
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
Volume range: 0–255.

### Real-time events (WebSocket)

The daemon emits JSON Patch messages whenever state changes (button press, fader move, USB hotplug). Subscribe to `ws://localhost:14564/api/websocket` to receive live updates.

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

| Fader | Source | Broadcast Mix | LineOut | Headphones |
|---|---|---|---|---|
| A | Mic (XLR) | ✓ | ✓ | ✓ |
| B | LineIn | ✓ | ✓ | ✓ |
| C | Game (codec RX Right) | ✓ | ✓ | ✓ |
| D | Chat (codec RX Left) | — | ✓ | ✓ |

All other GoXLR input buses (Music, Console, System, Samples) are fully unrouted.

### `start()`
1. Write `~/.asoundrc` with the `goxlr_broadcast` virtual capture device
2. Connect to daemon socket, retrieve device serial
3. Apply fader assignments via `SetFader`
4. Apply full routing matrix via `SetRouter` (all 8 sources × 5 outputs)
5. Apply cyan gradient lighting via `SetFaderDisplayStyle` + `SetFaderColours`

### `tx_source_bin()`
```python
return "alsasrc device=goxlr_broadcast ! audioconvert ! audio/x-raw,rate=48000,channels=2"
```

### `rx_sink_bin()`
```python
# Expands decoded stereo to 10-channel GoXLR playback using GStreamer audiomixmatrix.
# GoXLR requires S32LE — audioconvert must be applied before alsasink.
return (
    'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
    'audiomixmatrix in-channels=2 out-channels=10 matrix="<...>" ! '
    'audioconvert ! audio/x-raw,format=S32LE ! '
    'alsasink device=hw:GoXLRMini,0 sync=false'
)
```

### IPC — direct socket, no third-party library

All daemon communication uses raw Unix socket with the `[uint32 length][JSON]` framing. `goxlr-py` was not used — the raw approach has no dependencies and the protocol is simple enough. Open a new socket per command (no persistent connection needed).

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

## 12. Useful References

| Resource | URL |
|---|---|
| goxlr-utility (Rust daemon) | https://github.com/GoXLR-on-Linux/goxlr-utility |
| goxlr-utility Wiki / API docs | https://github.com/GoXLR-on-Linux/goxlr-utility/wiki |
| goxlr-py (Python wrapper) | https://github.com/samcarsonx/goxlr-py |
| goxlr-py documentation | https://goxlr.readthedocs.io/ |
| ALSA UCM config (channel layout source) | https://github.com/alsa-project/alsa-ucm-conf |
| Wireshark protocol dissector | https://github.com/GoXLR-on-Linux/goxlr-utility/blob/main/goxlr-wireshark-plugin.lua |
| GoXLR-on-Linux org (broader project) | https://github.com/GoXLR-on-Linux |

# GoXLR Mini on Linux — Research Notes

This document captures everything discovered about running and controlling the TC-Helicon GoXLR Mini on Linux, including USB audio layout, the control protocol, available tooling, and the integration plan for briclite.

---

## 1. Hardware Overview

The TC-Helicon GoXLR Mini is a broadcast audio mixer with:
- XLR mic input (with phantom power, hardware gate, compressor, EQ, de-esser)
- 3.5mm headphone output
- 3.5mm line input
- USB-C connection to host (power + audio + control)
- 4 motorized faders (A, B, C, D)
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

## 4. Routing the Broadcast Mix to GStreamer (TX)

Because the device presents 21 channels, you cannot simply use `alsasrc device=hw:GoXLRMini,0` and get stereo — you get 21 channels. Two approaches:

### Option A — ALSA virtual device (recommended for briclite)

Define a `route` plugin device in `/etc/asound.conf` that extracts channels 0–1 as stereo:

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

Then use `alsasrc device=goxlr_broadcast` in GStreamer. This is transparent to the pipeline — it sees a normal stereo 48 kHz source. The `GoXLRInterface.start()` method should write this config and verify it before the pipeline launches.

### Option B — GStreamer deinterleave/interleave

```
alsasrc device=hw:GoXLRMini,0 ! deinterleave name=d
d.src_0 ! queue ! interleave name=i
d.src_1 ! queue ! i.
i. ! audioconvert ! audio/x-raw,rate=44100,channels=2 ! ...
```

Cannot be expressed as a linear `parse_launch` string — requires programmatic pad linking. More complex, no advantage over Option A.

---

## 5. Routing Codec RX to GoXLR Inputs (RX)

The codec receives decoded stereo audio from the remote end. With the GoXLR, you can route the left and right channels to **separate GoXLR input buses**, so they appear on independent faders and can be independently mixed into headphones, broadcast, etc.

Example assignment (configurable):
- Codec RX Left → Game bus (playback channels 2–3)
- Codec RX Right → Chat bus (playback channels 4–5)

Implementation: the RX pipeline outputs 2-channel audio, then an ALSA virtual device (or direct channel addressing) places each channel into the correct pair of the 10-channel playback stream.

ALSA `route` example for RX:

```
pcm.goxlr_rx {
    type route
    slave {
        pcm "hw:GoXLRMini,0"
        channels 10
    }
    ttable {
        2.0 1.0   # codec RX L → Game L (ch 2)
        3.1 1.0   # codec RX R → Chat L (ch 4 — note: adjust indices per config)
    }
}
```

The specific channel pairs are user-configurable and stored under `"goxlr"` in `config.json`.

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

## 10. briclite Integration Plan

### Interface module

`briclite/interfaces/goxlr.py` implements `AudioInterface` (see `interfaces/base.py`).

**`start()`:**
1. Connect to goxlr-utility daemon via Unix socket
2. Retrieve device serial
3. Apply routing matrix config from `config["goxlr"]` (fader assignments, broadcast mix membership, line out sources)
4. Write ALSA virtual device config to `/etc/asound.conf` (or `~/.asoundrc`) for `goxlr_broadcast` (capture ch 0–1) and `goxlr_rx` (playback channel pairs)

**`tx_source_bin()`:**
```python
return "alsasrc device=goxlr_broadcast ! audioconvert ! audio/x-raw,rate=44100,channels=2"
```

**`rx_sink_bin()`:**
```python
return "alsasink device=goxlr_rx sync=false"
```

**`stop()`:**
- Optionally reset fader assignments or routing to a safe default
- Close daemon socket

### Config structure (`config.json` with GoXLR)

```json
{
  "system": {
    "audio_interface": "goxlr",
    "web_port": 8080,
    "bind_address": "0.0.0.0"
  },
  "audio_network": {
    "target_ip": "...",
    "tx_port": 5004,
    "rx_port": 5004,
    "buffer_ms": 200
  },
  "goxlr": {
    "alsa_device": "hw:GoXLRMini,0",
    "broadcast_mix_channels": [0, 1],
    "rx_left_channels": [2, 3],
    "rx_right_channels": [4, 5],
    "fader_assignments": {
      "A": "Mic",
      "B": "Game",
      "C": "Chat",
      "D": "Music"
    },
    "broadcast_mix_sources": ["Mic", "Game", "Music"],
    "line_out_sources": ["Mic", "Chat"]
  }
}
```

### ALSA note: device name stability

Use `hw:GoXLRMini,0` (name-based) rather than `hw:2,0` (index-based) in config. Card indices change if devices are plugged in a different order or if other USB audio devices are present. Name-based addressing is stable.

---

## 11. Routing Matrix Reference

The GoXLR Mini's internal matrix connects inputs (rows) to output buses (columns). The daemon's `SetRouter` command toggles individual crosspoints.

**Input sources:** Mic, Chat, Music, Game, System, Sample, LineIn, Headphones  
**Output buses:** Headphones, BroadcastMix, ChatMic, Sampler, LineOut

Each crosspoint is a boolean (on/off). Volume for each source is set separately with `SetVolume`.

The full matrix state is returned by `GetStatus` and can be applied in bulk at startup from config.

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

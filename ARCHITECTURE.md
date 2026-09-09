# System Architecture

This document explains the internal design of the broadcast audio codec, key design decisions, and how all the pieces work together.

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────┐
│ Ubuntu Server with Behringer UCA202 USB Audio Interface     │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ FastAPI Web Application (main.py)                      │ │
│  │ - HTTP API: /api/connect, /api/disconnect             │ │
│  │ - WebSocket: /ws/telemetry (100ms updates)            │ │
│  │ - Serves: HTML dashboard on /                          │ │
│  └────────────────────────────────────────────────────────┘ │
│                          │                                    │
│       ┌──────────────────┼──────────────────┐               │
│       ▼                  ▼                  ▼               │
│  ┌──────────┐     ┌─────────────┐    ┌──────────────┐     │
│  │TX Pipeline   │  │ App State   │    │RX Pipeline   │     │
│  │(GStreamer)   │  │(DataBroker) │    │(GStreamer)   │     │
│  └──────────┘     └─────────────┘    └──────────────┘     │
│       │                  ▲                  │               │
│       └──────────────┬───┼───┬──────────────┘               │
│                      │   │   │                              │
│      ┌───────────────┴─┐ │ ┌─┴──────────────┐             │
│      │ UDP Socket      │ │ │ Jitter Buffer  │             │
│      │ Port 5004       │ │ │ 200ms window   │             │
│      └─────────────────┘ │ └────────────────┘             │
│             │◄───────────┴───────────────────►│             │
│             │                                  │             │
│      ┌──────▼──────────────────────────────────▼──┐        │
│      │ Network (UDP RTP/ADTS) ← → Remote Studio   │        │
│      └─────────────────────────────────────────────┘        │
│             │                                  │             │
│      ┌──────▼──────────────────────────────────▼──┐        │
│      │ Behringer UCA202 (hw:0,0)                   │        │
│      │ 44.1 kHz stereo analog I/O                 │        │
│      └─────────────────────────────────────────────┘        │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. TX Path (Audio Capture & Transmission)

**GStreamer Pipeline:**
```
alsasrc (hw:0,0) 
  → audioconvert 
  → level (tx_meter) 
  → audioresample (44.1 kHz → 24 kHz)
  → avenc_aac (AAC-LC encoder) 
  → aacparse (ADTS framing) 
  → appsink (emit-signals)
```

**Flow:**
1. `alsasrc` captures audio from Behringer at 44.1 kHz stereo
2. `level` element measures peak dBFS and emits to DataBroker
3. `audioresample` converts to 24 kHz (codec requirement)
4. `avenc_aac` encodes raw PCM to AAC-LC bitstream
5. `aacparse` wraps each frame in ADTS (AAC Data Transport Stream) sync word + metadata
6. `appsink` emits each buffer as a signal, caught by `_on_tx_sample()` callback

**RTP Framing (in Python):**
```
[12-byte RTP header] + [4-byte RFC2250 header] + [ADTS payload]

RTP Header (12 bytes):
  - V=2, PT=14 (MPEG Audio), flags
  - Sequence number (incremented per frame)
  - RTP timestamp (90000 Hz clock, +3840 per 1024-sample frame @ 24 kHz)
  - SSRC (random at startup)

RFC2250 Header (4 bytes):
  - MBZ (Must Be Zero) = 0x00000000
  - Used by standard MPEG-over-RTP, Comrex BRIC-Link expects this format

ADTS Payload:
  - Sync word: 0xFFF (11 bits of 1s)
  - Frame metadata: profile (AAC-LC), sample rate (24 kHz), channels (2), frame length
  - Compressed audio frame
```

**Why this design:**
- **No GStreamer payloader:** The standard `rtpmpapay` element doesn't handle PT=14 + RFC2250 correctly. Python RTP header construction gives us full control.
- **Shared socket:** Both TX and RX use the same UDP socket bound to port 5004. This ensures TX packets originate from port 5004, so the remote device replies to the same port (not an ephemeral OS-assigned port).
- **ADTS:** Required by the Comrex BRIC-Link for codec auto-detection. The 0xFFF sync word is a magic byte pattern the Comrex recognizes as AAC.

---

## 2. RX Path (Reception & Playback)

**Network receive (`_rx_loop()` thread):**
1. `socket.recvfrom(65535)` — blocks until UDP packet arrives on port 5004
2. Parse RTP header: extract sequence number, RTP timestamp
3. Strip RFC2250 header (if present; remote might not send it)
4. Validate ADTS sync word `0xFFF`
5. Push to jitter buffer: `jitter_buf.push(seq, rtp_ts, payload)`

**Jitter Buffer (`_playout_loop()` thread):**
1. Pop next sequence number in order: `jitter_buf.pop(last_good)`
2. If packet arrived: return it
3. If packet is late/missing but a future packet arrived: declare it lost, repeat last good frame (loss concealment)
4. If deadline (200ms) expires waiting: declare lost, repeat last good frame
5. Wrap in Gst.Buffer, set PTS (presentation timestamp) and duration
6. Emit to `rx_appsrc` (GStreamer pipeline input)

**GStreamer Pipeline:**
```
appsrc (fed by Python playout loop)
  → queue (buffer bursts)
  → avdec_aac (AAC decoder)
  → audioconvert
  → level (rx_meter)
  → audioresample (24 kHz → 44.1 kHz)
  → audiorate (handle clock drift)
  → alsasink (output to Behringer)
```

**Why this design:**
- **Decoupled threads:** RX network thread is not blocked by audio playback. Jitter buffer sits between them.
- **Packet repetition concealment:** Simple but effective for single-packet losses. Stutters on burst losses but better than silence.
- **EWMA jitter tracking:** RFC 3550 algorithm — measures variance in inter-arrival times, exponentially weighted to recent packets.
- **Fast loss detection:** If a future packet arrives out of order, we immediately know the gap is lost (don't wait full latency timeout).
- **`audiorate` element:** Inserts/drops samples to keep playback in sync with wall clock. Handles minor drift between remote and local clocks.

---

## 3. Jitter Buffer

**Purpose:** Reorder out-of-sequence packets, detect loss, conceal loss, track jitter.

**State:**
- `_buf: dict` — Stores packets by RTP sequence number
- `_next_seq: int` — Next sequence number expected for playout
- `_last_arrival, _last_rtp_ts` — Timestamps of last packet for jitter calculation

**Operations:**

`push(seq, rtp_ts, payload)`:
- If this is the first packet, initialize `_next_seq = seq`
- Reject late packets (already played or skipped)
- Calculate inter-arrival jitter: difference between wall-clock arrival time and RTP timestamp clock time
- Update EWMA: `jitter += (delta - jitter) / 16.0` (weight recent samples more heavily)
- Store packet in `_buf[seq]`

`pop(last_good)`:
- If `_next_seq` is in buffer: return it, increment `_next_seq`
- Else if any later sequence is in buffer: packet is lost, return last good frame as concealment
- Else if deadline (200ms) expires: packet is lost, return last good frame
- Else: wait (with timeout), try again

`reset()`:
- Clear state on disconnect. Resets jitter metric and packet counters.

**Jitter metric:**
- RFC 3550 defines: `J = J + (|D(i)-D(i-1)| - J) / 16`
- D(i) is the difference between consecutive inter-arrival intervals
- Displayed in milliseconds (rounded to int)
- Used to detect network congestion or packet reordering

---

## 4. Application State & Telemetry

**DataBroker (`data_broker.py`):**
- Thread-safe dataclass with `asyncio.Lock`
- Holds: connection status, peak dBFS (TX/RX L+R), jitter, packet loss counters
- Updated by: GStreamer level messages, jitter buffer stats, pipeline state

**Update Flow:**
1. GStreamer `level` element emits message on bus
2. `_on_bus_message()` parses peak dBFS values
3. `asyncio.run_coroutine_threadsafe()` queues state update to event loop
4. DataBroker acquires lock, updates metrics
5. WebSocket handler polls state every 100ms, sends JSON to browser

**Why this design:**
- GStreamer runs in its own GLib main loop (different thread)
- FastAPI runs in asyncio event loop (main thread)
- Lock prevents race conditions when GStreamer and async handlers access state simultaneously

---

## 5. RX Channel Routing

The incoming AAC stream is dual-mono: the left and right channels carry independent audio. A channel routing selector allows monitoring either channel independently or both together.

**GStreamer element:** `audiomixmatrix` (gst-plugins-bad) with a 2×2 matrix:

| Mode | Matrix | Effect |
|---|---|---|
| Stereo | `[[1,0],[0,1]]` | Passthrough |
| Left only | `[[1,0],[1,0]]` | Left channel → both outputs |
| Right only | `[[0,1],[0,1]]` | Right channel → both outputs |

**Runtime mode changes:** The matrix is baked into the pipeline string at creation. Changing mode tears down the RX pipeline and rebuilds it with a new string. TX pipeline and jitter buffer are unaffected — **for the Behringer interface only** (see below for GoXLR).

**Why not set the matrix property at runtime?**
`Gst.util_set_object_arg` on a playing `audioconvert` element caused caps renegotiation that destabilised the pipeline. Rebuilding is simpler and more reliable.

**Race condition protection (Behringer interface):** Two measures prevent ALSA from getting stuck when modes are changed rapidly:
1. **Debounce (250ms):** rapid calls cancel and reschedule — only the final mode gets applied.
2. **`get_state(Gst.SECOND)`:** blocks until the old pipeline has fully released the ALSA device before the new one opens it. Without this, overlapping ALSA opens caused the playout buffer to loop.
3. **`PipelineController._rx_rebuild_lock`:** serialises `_do_rx_rebuild()` against `_playout_loop()`'s `push-buffer` calls and against overlapping rebuild triggers, so two threads never manipulate `self.rx_pipeline`/`self.rx_appsrc` concurrently.

**GoXLR interface uses a full reconnect instead** (`main.py`'s `_full_reconnect()`): stop, build a brand-new `PipelineController` (passing the target `rx_channel_mode` into its constructor so the right pipeline is built from the start), start. Discovered 2026-09-09: the GoXLR's TX (capture) and RX (playback) are two directions of the *same* USB audio device, and tearing down/reopening only the RX side in place while TX stays open reliably wedges the device (deterministic — reproduced with a single isolated mode switch, not just rapid clicking). A full stop/start cycles both directions together and has proven reliable under repeated stress-testing. This same full-reconnect path is also used for the Behringer hotplug rebuild (see §9) and for restoring a saved channel mode after `/api/connect`. The RX watchdog's auto-reconnect (§9/troubleshooting) also preserves the current channel mode across a reconnect, though it still resets other GoXLR-specific state (Bleep/PFL, fader colours) — see Known Limitations.

**API:** `POST /api/rx_mode` with `{"mode": "stereo"|"left"|"right"}`. Mode is stored in `global_state` and restored on reconnect.

---

## 6. Web Dashboard

**Real-time Updates:**
- WebSocket at `/ws/telemetry` — 100ms polling interval
- Sends: connection status, peak levels, jitter, lost/late packet counts, `rx_channel_mode`

**Meter styles (toggle between):**

*Digital:* Vertical bar meters with dBFS scale ruler. TX Input (blue gradient), RX Output (green/amber/red gradient). -18 dBFS alignment line at 70% height.

*Analogue:* SVG needle meters, one per channel (TX-L, TX-R, RX-L, RX-R). Styled after a broadcast VU meter: beige face, colour-coded arc zones (green -60→-10, yellow -10→-1, red -1→+6), layered needle with cubic-bezier spring animation (130ms), cyan peak-hold dot (3-second hold). SVG paths computed from the same constants and math as the reference implementation at `grahamduthie/mfm-meter`. Meter type preference is saved in `localStorage`.

**Controls:**
- Connect button — starts both pipelines, opens UDP socket
- Disconnect button — halts pipelines, closes socket
- Optional IP override field — allows changing `target_ip` at runtime
- RX Channel Routing — L+R Stereo / Left Only / Right Only

---

## 7. Key Design Decisions

### Why PT=14 + RFC2250 instead of OPUS?

The Comrex BRIC-Link supports OPUS but only via SIP negotiation. OPUS on PT=111 without SIP falls back to G.722. AAC PT=14 with ADTS sync word auto-detects (remote device recognizes the 0xFFF pattern). This was discovered via packet capture from Luci Live.

**Future work:** Implement SIP using `python-sipsimple` or `pjsua2` to negotiate OPUS. Benefits: 20ms frames, in-band FEC, better quality at low bitrates. Trade-off: significant added complexity.

### Why no GStreamer RTP payloader?

GStreamer's `rtpmpapay` element is designed for standard MPEG-audio-over-RTP (RFC 2250), but it has quirks:
- Doesn't reliably negotiate with all payloaders
- Adds complexity to the pipeline
- Requires specific caps strings to match

Manual RTP framing in Python:
- Full control over header construction
- Simple, testable, debuggable
- Suitable for fixed PT=14 codec

### Why shared UDP socket?

Original design had separate TX and RX sockets. Issue: TX packets originated from an ephemeral OS-assigned port (e.g., 40350), so the Comrex sent replies to that port, not 5004. This broke the jitter buffer.

**Solution:** Single socket bound to port 5004. TX packets automatically originate from port 5004, RX packets arrive there. Comrex knows to reply to port 5004.

### Why 200ms jitter buffer?

- Suitable for broadband (typical RTT 5-50ms, jitter <20ms)
- Too short: packet loss on Starlink/satellite (RTT 500ms+)
- Too long: playback delay becomes noticeable in live broadcast
- **Configurable:** Set `buffer_ms` in `config.json` for different links

### Why `sync=false` on alsasink?

With `sync=true`, the alsasink element checks if incoming buffer timestamps match wall-clock time. On live audio streams with jitter, timestamps drift, and late buffers are dropped silently. With `sync=false`, buffers are played regardless, and `audiorate` handles minor timing adjustments.

### Why `async=false` on audio paths?

Ensures immediate startup without waiting for PREROLL state. Important for live broadcast where latency matters.

---

## 8. Threading Model

| Thread | Purpose | Notes |
|---|---|---|
| Main (FastAPI) | HTTP/WebSocket server | Asyncio event loop |
| GLib | GStreamer elements | Runs pipelines, emits messages |
| RX loop | Network receive | Blocks on `recvfrom()`, pushes to jitter buffer |
| Playout loop | Jitter buffer drain | Pops at codec framerate, pushes to GStreamer |

**Synchronization:**
- Jitter buffer uses `threading.Lock` for thread-safe access
- GStreamer state updates go through `asyncio.run_coroutine_threadsafe()`
- No shared mutable state except through locks/async primitives

---

## 9. AudioInterface Plugin System

The audio I/O layer is abstracted behind an `AudioInterface` ABC (`interfaces/base.py`). This allows Behringer UCA202 and GoXLR Mini deployments to share one codebase, with the active interface selected at runtime.

### Interface contract

```python
class AudioInterface(ABC):
    def tx_source_bin(self) -> str: ...          # GStreamer bin: produces audio/x-raw,rate≥44100,channels=2
    def rx_sink_bin(self)  -> str: ...           # GStreamer bin: accepts audio/x-raw,rate=44100,channels=2
    def extra_rx_source_bins(self) -> list[str]: # Additional sources to mix into rx_sink_bin (default: [])
    def start(self) -> None: ...                  # Called when codec pipeline starts
    def stop(self)  -> None: ...                  # Called when codec pipeline stops
```

`PipelineController` slots `tx_source_bin()` and `rx_sink_bin()` into the pipeline strings, then appends each string from `extra_rx_source_bins()` as additional branches before calling `Gst.parse_launch()`. Each interface owns its own hardware setup, ALSA config, and daemon communication.

### BehringerInterface

Wraps the original behaviour exactly. TX source: `alsasrc device=hw:0,0 ! audioconvert ! audio/x-raw,rate=44100,channels=2`. RX sink: `alsasink device=hw:0,0 sync=false`. Returns `[]` from `extra_rx_source_bins()`.

### GoXLRInterface

Used when a GoXLR Mini is present. On `start()`:
1. Writes an ALSA `route` virtual device (`goxlr_broadcast`) to `~/.asoundrc` to extract the 2-channel Broadcast Mix from the 21-channel USB capture stream
2. Connects to the goxlr-utility daemon (`/tmp/goxlr.socket`) and retrieves the device serial
3. Applies fader assignments, routing matrix, and mute functions via daemon IPC
4. Applies cyan gradient lighting to all faders and the Bleep button

**TX:** `alsasrc device=goxlr_broadcast ! audioconvert ! audio/x-raw,rate=48000,channels=2`

**RX — two-source pipeline:** The ALSA `route` plugin cannot reliably expand 2 channels to the GoXLR's 10-channel playback stream (silent failure; S32LE-only device, broken ALSA constraint propagation). Instead, GStreamer's `audiomixmatrix` maps each source to its assigned GoXLR channels, and `audiomixer` combines them before writing to `hw:GoXLRMini,0` — **but only when there actually is a second source to combine:**

```
# Behringer absent — no audiomixer at all, straight to alsasink
... ! audiomixmatrix in-channels=2 out-channels=10 matrix="<RX>" !
audioconvert ! audio/x-raw,format=S32LE !
alsasink device=hw:GoXLRMini,0 sync=false

# Behringer present — audiomixer combines both branches
... ! audiomixmatrix in-channels=2 out-channels=10 matrix="<RX>" !
audiomixer name=goxlr_mix ignore-inactive-pads=true min-upstream-latency=200000000 latency=200000000 !
audioconvert ! audio/x-raw,format=S32LE !
alsasink device=hw:GoXLRMini,0 sync=false

# Behringer capture → Chat (ch 4-5)   [from extra_rx_source_bins(), appended when present]
alsasrc device=hw:CODEC,0 ! audioconvert !
audioresample ! audio/x-raw,rate=48000,channels=2 !
audiomixmatrix in-channels=2 out-channels=10 matrix="<Behringer>" !
goxlr_mix.
```

**Why the conditional mixer (discovered 2026-09-09):** `audiomixer`'s automatic latency query always fails on this pipeline (`WARN aggregator: <goxlr_mix> Latency query failed`) — it never negotiates a value because it's combining a manually-fed `appsrc` branch (the decoded RX stream, PTS assigned by `_playout_loop()`) with a live `alsasrc` branch (Behringer capture), and defaults to assuming 0 latency. Symptoms observed: an audible echo, a "stretched cassette tape" pitch artifact, and — most confusingly — **complete, silent stalls of the mixer's entire output** (both Game and Music go silent) that happened even with only *one* input pad connected (Behringer physically unplugged), while everything upstream of the mixer (decode, `level` meter) kept working normally and nothing was posted to the GStreamer bus. `lost`/`late`/`jitter` in telemetry stayed at 0 throughout — this is not RTP packet loss, it's local to this element. Fix: skip `audiomixer` entirely in the single-source case (§9's `GoXLRInterface.rx_sink_bin()`/`extra_rx_source_bins()` both key off `GoXLRInterface.behringer_available()`, so they always agree), and when a second source is genuinely present, give the mixer an explicit latency budget (`min-upstream-latency`, `latency`, both matching the jitter buffer's 200ms) plus `ignore-inactive-pads=true` instead of relying on the broken auto-negotiation. Verified live on the PSA300 with the Behringer both absent and present — echo and stretching gone in both cases.

**Fader layout:**

| Fader | Channel | Signal | BroadcastMix |
|---|---|---|---|
| A | Mic | Main microphone (XLR) | ✓ |
| B | Chat | Guest mic (Behringer `hw:CODEC,0`) | ✓ |
| C | Game | News feed (codec RX Right) | ✓ |
| D | LineIn | Music player (GoXLR line in) | ✓ |
| — | Music | Studio return (codec RX Left) | — |

The studio return (Music bus) has no fader and is never in the broadcast mix. It is only audible in headphones via the Bleep PFL button.

**Bleep button — Studio Return PFL:**

`monitor_pfl()` runs as a background asyncio task, subscribing to the goxlr-utility WebSocket (`ws://localhost:14564/api/websocket`). On each `button_down/Bleep: true` event, it toggles studio return PFL:

- *PFL on:* All fader sources removed from headphones (`SetRouter`). Music bus routed to headphones. Music volume set to 255 (`SetVolume`) for true pre-fade monitoring regardless of internal volume. Bleep LED → orange.
- *PFL off:* Headphone routing restored. Music volume restored. Bleep LED → cyan.

IPC calls triggered by button events run in a thread pool (`asyncio.to_thread`) to avoid blocking the FastAPI event loop.

**Headphone volume control:**

`POST /api/headphone_volume {"pct": 0–100}` calls `SetVolume ["Headphones", N]`. Visible in the web UI as a slider (GoXLR mode only). Current volume is read from `GetStatus` at startup and included in WebSocket telemetry.

### Auto-detection and hotplug

`main.py` selects the interface at startup (`audio_interface: auto` in config — default) and monitors for changes every 5 seconds. If GoXLR presence changes, the interface is hot-swapped; any active pipeline is stopped first. The web UI badge (`GoXLR` / `Behringer`) reflects the current mode via WebSocket telemetry. The PFL monitor task is started and cancelled alongside the GoXLR interface.

The goxlr-utility daemon (`goxlr-daemon.service`) must be running for GoXLR mode to activate. The daemon is detected by probing `/tmp/goxlr.socket`. See `GOXLR-MINI-LINUX.md` for full daemon setup.

**Behringer hotplug (added 2026-09-09):** the same 5-second poll also calls `GoXLRInterface.behringer_available()` (checks `/proc/asound/cards` for `CODEC`) while running GoXLR, independent of the auto/fixed interface-mode setting above. On a change, it logs it and calls `_full_reconnect()` — the same full stop/start used for RX channel-mode changes (§5), not the lighter in-place RX rebuild, for the same ALSA-wedging reason. This means the Behringer can be plugged in or unplugged at any time without operator action.

**Known gap:** any full reconnect (channel-mode switch, Behringer hotplug, or the RX watchdog's auto-reconnect — see Troubleshooting) calls `GoXLRInterface.start()` fresh, which silently resets Bleep/PFL state (`_studio_pfl = False`) and re-applies default fader colours/routing, with no indication to the operator that it happened. If PFL was engaged, the operator is silently dropped back to normal fader monitoring. Not yet fixed — would need PFL state to be captured before `stop()` and reapplied after the new interface's `start()` completes, in whichever of `main.py`'s several reconnect call sites triggers.

---

## 10. Current Limitations

**Codec auto-detection delay:** When connecting, the remote device may show a fallback codec (G.722 VoIP) for 2-3 seconds before recognizing AAC. Disconnect/reconnect resets this.

**Loss concealment:** Packet repetition only. Produces stuttering on burst losses. No interpolation or algorithmic PLC.

**Residual brief glitch (~once/minute), root cause revised 2026-09-09:** With the audiomixer echo/stretching bug (§9) fixed, an occasional brief audio glitch remained (heard on both Game and Music, i.e. upstream of the per-bus split), with `lost`/`late`/`jitter` staying at exactly 0 and no xrun/underrun/error logged. This was initially attributed to remote/local clock drift, with a fix planned to derive RX playout PTS from the RTP timestamp instead of the local frame counter (`rx_pts += FRAME_DUR` in `_playout_loop()`). **That diagnosis doesn't hold up:** `_on_tx_sample()` increments `rtp_ts` by the fixed nominal `_TS_INC` on every frame, with no reference to a real capture clock — so in the loss-free case `rtp_ts` is mathematically identical to the sequence number, and deriving PTS from it computes the exact same number the local counter already does. Worse, every `rx_sink_bin()` sets `sync=false` on a direct `hw:` ALSA device (no `dmix`), so PTS isn't even consulted for playback pacing — buffers are pushed to the sink as fast as they arrive and drained purely by the sink's own hardware clock. Given that, and given this runs on weak embedded hardware (Celeron J1900 PSA300, Python + GIL, several threads — `_rx_loop`, `_playout_loop`, GLib bus-watch posting 10Hz level messages per meter, a 10Hz telemetry websocket), a transient scheduling stall causing a direct-hw ALSA underrun is a far more likely cause than genuine oscillator drift (which would trend with buffer depth, not repeat cleanly). **Fix applied 2026-09-09:** widened the RX `queue` from 10 to 50 max buffers (`_RX_QUEUE_BUFFERS`, `pipeline_manager.py`), doubled the ALSA/CoreAudio sink's `buffer-time`/`latency-time` (`ALSA_BUFFER_TIME_US`/`ALSA_LATENCY_TIME_US`, `interfaces/base.py`), and `_playout_loop()` now attempts to raise itself to `SCHED_FIFO` priority on Linux (falls back to a logged warning without `CAP_SYS_NICE` — see BUILD.md §7 for the systemd grant, not yet applied to the running PSA300 unit). If the glitch persists after this, the remaining candidate is genuine independent-oscillator clock drift between the two boxes, which would need a self-referential fix — an adaptive playout corrector that skips/inserts a frame when jitter-buffer occupancy trends off target — since there is no real cross-host clock reference available on the wire to derive PTS from.

**No SIP:** OPUS unsupported without SIP negotiation.

**No FEC:** No forward error correction. Useful for Starlink/satellite OB.

---

## 10. Testing & Debugging

**Check live state:**
```bash
ssh codec@<server-ip>
sudo systemctl status briclite
journalctl -u briclite -f
```

**Packet capture:**
```bash
sudo tcpdump -i eth0 -n 'port 5004' -c 50
```

**Check ALSA device:**
```bash
aplay -l
arecord -D hw:0,0 -f cd /tmp/test.wav  # record 5 seconds, Ctrl+C to stop
aplay -D hw:0,0 /tmp/test.wav
```

**Manual GStreamer test:**
```bash
timeout 10 gst-launch-1.0 alsasrc device=hw:0,0 num-buffers=200 ! fakesink
```

**Web dashboard:**
```
http://codec.local
```

Check meters for input/output levels. If RX meters are frozen, jitter buffer is stuck (likely receiving incompatible RTP payload — check Comrex codec, do disconnect/reconnect).

---

## 11. Performance Characteristics

| Metric | Value | Notes |
|---|---|---|
| Audio latency | ~200ms | Jitter buffer + codec + network |
| Jitter buffer depth | 200ms | Configurable, increase for poor links |
| RTP timestamp clock | 90000 Hz | Standard, yields 3840 ticks/frame @ 24 kHz |
| AAC frame duration | ~43 ms | 1024 samples @ 24 kHz = 42.67 ms |
| CPU usage | <15% | Intel Celeron J1900 |
| Memory | <100 MB | Python + GStreamer + venv |

---

## 12. Future Roadmap

1. **OPUS with SIP negotiation** — Better for poor links, requires SIP library
2. **Improved loss concealment** — Interpolation or algorithmic PLC in C
3. **Forward error correction** — RFC 5109 ULPFEC or custom XOR FEC
4. **Playout buffering/scheduling hardening — IMPLEMENTED 2026-09-09.** The residual ~once-a-minute brief glitch (§10) was originally attributed to clock drift with a fix planned around RTP-timestamp-derived PTS; that plan turned out to be a dead end (`rtp_ts` is a purely nominal counter here, mathematically identical to the sequence number in the loss-free case, and every RX sink runs `sync=false` so PTS isn't even consulted for playback pacing). The revised, applied fix instead targets the actual suspect — a transient scheduling stall starving the direct `hw:` ALSA sink on weak embedded hardware:
   - RX `queue` widened from 10 to 50 max buffers (`_RX_QUEUE_BUFFERS`, `pipeline_manager.py`) — pure headroom, costs no steady-state latency since the queue sits near-empty.
   - ALSA/CoreAudio sink `buffer-time`/`latency-time` doubled (`ALSA_BUFFER_TIME_US`=400000, `ALSA_LATENCY_TIME_US`=20000, `interfaces/base.py`) — this does add ~200ms real output latency, traded for underrun headroom.
   - `_playout_loop()` now attempts `SCHED_FIFO` priority on Linux (`_PLAYOUT_RT_PRIORITY`) to protect it from GIL/CPU scheduling contention; requires `CAP_SYS_NICE`/`LimitRTPRIO` (BUILD.md §7, not yet granted on the running PSA300 unit — falls back to a logged warning otherwise, no functional impact).
   - **Not yet validated on-air** — needs a multi-hour soak listen on the PSA300 to confirm the glitch is gone (or reduced), since there's no error/counter to compare against, only audible confirmation.
   - **If it persists:** genuine independent-oscillator drift between the two boxes is the remaining candidate. There is no real cross-host clock reference on the wire in this protocol to derive PTS from, so the correct fix would be a self-referential adaptive playout corrector — skip/insert a frame when jitter-buffer occupancy trends persistently off target — not a PTS-labeling change. Bigger change, own artifact risk (occasional frame skip/insert click); not built yet, pending confirmation the buffering fix above is insufficient.
5. **Hardware display** — LCD/OLED status panel (header defined in config.json but not implemented)
6. **OpenVPN auto-connect** — For secure outside broadcast links (config placeholder, not implemented)

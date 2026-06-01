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

**Runtime mode changes:** The matrix is baked into the pipeline string at creation. Changing mode tears down the RX pipeline and rebuilds it with a new string. TX pipeline and jitter buffer are unaffected.

**Why not set the matrix property at runtime?**
`Gst.util_set_object_arg` on a playing `audioconvert` element caused caps renegotiation that destabilised the pipeline. Rebuilding is simpler and more reliable.

**Race condition protection:** Two measures prevent ALSA from getting stuck when modes are changed rapidly:
1. **Debounce (250ms):** rapid calls cancel and reschedule — only the final mode gets applied.
2. **`get_state(Gst.SECOND)`:** blocks until the old pipeline has fully released the ALSA device before the new one opens it. Without this, overlapping ALSA opens caused the playout buffer to loop.

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

## 9. Current Limitations

**Codec auto-detection delay:** When connecting, the remote device may show a fallback codec (G.722 VoIP) for 2-3 seconds before recognizing AAC. Disconnect/reconnect resets this.

**Loss concealment:** Packet repetition only. Produces stuttering on burst losses. No interpolation or algorithmic PLC.

**Clock drift:** Not corrected. On multi-hour sessions, remote and local clocks drift tens of milliseconds. `audiorate` adapts but works blind (doesn't use RTP timestamps for reference).

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
http://<server-ip>:8080
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
4. **Clock drift correction** — Use RTP timestamps for PTS, let `audiorate` measure and compensate drift
5. **Hardware display** — LCD/OLED status panel (header defined in config.json but not implemented)
6. **OpenVPN auto-connect** — For secure outside broadcast links (config placeholder, not implemented)

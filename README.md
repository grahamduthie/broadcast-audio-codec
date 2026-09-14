# Broadcast Audio Codec

A software-defined broadcast audio codec for remote radio stations, providing bidirectional AAC-LC audio streaming to studio mixing consoles and broadcast infrastructure.

## Overview

This project enables standalone broadcast-grade audio transport from a headless Ubuntu server:

- **Bidirectional RTP/UDP** audio at 24 kHz stereo, AAC-LC codec
- **Jitter buffer** with packet reordering, loss detection, and packet-repetition concealment
- **RX channel routing** — select Left only, Right only, or L+R stereo output (for dual-mono sources); handled in the backend, no longer exposed in the dashboard UI
- **Live web dashboard** — console-strip layout with per-GoXLR-channel fader/gain controls, a Broadcast Mix master meter, jitter statistics, and connection controls (redesigned 2026-09-13, see `CURRENT-STATUS.md`)
- **Systemd auto-start** on boot

## Quick Start

See **[BUILD.md](BUILD.md)** for the complete from-scratch build and deployment guide.
For the current live investigation and exact PSA300 state, read **[CURRENT-STATUS.md](CURRENT-STATUS.md)** first.

## Project Structure

```
briclite/
├── config.json              # Deployment configuration (IP, ports, ALSA device)
├── main.py                  # FastAPI application, web UI, API endpoints
├── core/
│   ├── data_broker.py       # Thread-safe state management
│   └── pipeline_manager.py  # GStreamer pipelines, RTP framing, jitter buffer
└── web/
    └── templates/
        └── index.html       # Real-time dashboard (dBFS meters, jitter chart)
```

## Key Components

- **GStreamer 1.0** — Audio capture (ALSA), encoding (AAC-LC), decoding, playback
- **Python jitter buffer** — RFC 3550 EWMA jitter tracking, 200ms playout window
- **FastAPI / uvicorn** — REST API for connect/disconnect, WebSocket telemetry
- **Behringer UCA202** — USB audio interface (no drivers needed, class-compliant)

## Connection Details

| Parameter | Value |
|---|---|
| Remote endpoint | Configurable IP and port (default 5004) |
| RTP payload type | 14 (MPEG Audio) |
| Codec | AAC-LC, 24 kHz stereo |
| RTP framing | 12-byte RTP header + 4-byte RFC2250 header + ADTS payload |
| Jitter buffer | 200 ms (configurable for poor links) |

## Deployment

### On Ubuntu server (headless):
1. Install Ubuntu Server 24.04 LTS
2. Follow deployment steps in BUILD.md
3. Unit auto-starts on boot via systemd

### Local development (on this Mac):
```bash
# Create venv (Python 3.10+)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Note: GStreamer and PyGObject must be installed system-wide (macOS/Linux)
```

## Web Dashboard

**Default URL:** `http://192.168.1.100:8080` (adjust IP as needed)

Console-strip layout (redesigned 2026-09-13 — see `ARCHITECTURE.md` §6 and `CURRENT-STATUS.md` for the full rationale, and before changing it again note that the GoXLR Mini has **no live per-channel audio metering** for Mic/LineIn/Console, confirmed by direct testing):

- **Mic / LineIn / Console / Game(News)** strips — live GoXLR fader-position bars; Mic also has a real hardware preamp gain control
- **Incoming Network** module — the two genuinely real-time meters on the page: Studio Return and News, tapped before the signal reaches the GoXLR; also hosts the Studio Return PFL baseline level control
- **Broadcast Mix** — the master, post-everything on-air meter, with the -18 dBFS reference line. Shows real levels regardless of connection status (2026-09-14) — it's a local GoXLR signal, unlike Incoming Network above which needs an actual network link
- Jitter/lost/late shown as compact numbers; the rolling graph auto-expands only on real trouble
- Connect/Disconnect controls with optional target IP override
- Headphone/Speaker volume (GoXLR mode)

## Known Limitations

- **OPUS codec** — Supported by some devices but requires SIP negotiation (not implemented)
- **Loss concealment** — Packet repetition only (no interpolation or algorithmic PLC)
- **GoXLR Linux USB playback glitch** — Reproduces with native ALSA and is strongly aggravated by `goxlr-daemon`; the newer kernel is under test. See `CURRENT-STATUS.md`.

See Section 12 of BUILD.md for future work roadmap.

## License

Internal use only.

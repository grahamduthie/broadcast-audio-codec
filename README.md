# PSA300 Broadcast Codec

A software-defined broadcast audio codec running on a Pulse Secure PSA300 appliance, integrating bidirectional AAC-LC audio streaming with the Marlow FM Comrex BRIC-Link.

## Overview

This project enables standalone broadcast-grade audio transport from a headless PSA300 unit:

- **Bidirectional RTP/UDP** audio at 24 kHz stereo, AAC-LC codec
- **Jitter buffer** with packet reordering, loss detection, and packet-repetition concealment
- **Live web dashboard** with dBFS metering, jitter statistics, and connection controls
- **Systemd auto-start** on boot

## Quick Start

See **[PSA300-Codec-Spec.md](PSA300-Codec-Spec.md)** for the complete from-scratch build and deployment guide.

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
| Remote endpoint | Comrex BRIC-Link at `217.36.229.106:5004` |
| RTP payload type | 14 (MPEG Audio) |
| Codec | AAC-LC, 24 kHz stereo |
| RTP framing | 12-byte RTP header + 4-byte RFC2250 header + ADTS payload |
| Jitter buffer | 200 ms (configurable for poor links) |

## Deployment

### On PSA300 (headless appliance):
1. Install Ubuntu Server 24.04 LTS
2. Follow deployment steps in PSA300-Codec-Spec.md
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

**Default URL:** `http://172.16.10.213:8080`

- TX Input meters (blue) — microphone/line input from Behringer
- RX Output meters (green/amber/red) — audio from Comrex
- -18 dBFS reference line (broadcast standard)
- Jitter and packet-loss statistics
- Connect/Disconnect controls with optional target IP override

## Known Limitations

- **OPUS codec** — Supported by Comrex but requires SIP negotiation (not implemented)
- **Loss concealment** — Packet repetition only (no interpolation or algorithmic PLC)
- **Clock drift** — Not corrected on long sessions (monitored by `audiorate` element)

See Section 12 of PSA300-Codec-Spec.md for future work roadmap.

## License

Internal use only.

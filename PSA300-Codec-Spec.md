# PSA300 Broadcast Codec — Build & Deployment Guide
### Marlow FM / Comrex BRIC-Link Integration

This document is the authoritative from-scratch build guide for turning a **Pulse Secure PSA300** appliance and a **Behringer UCA202** USB audio interface into a standalone bidirectional broadcast audio codec that connects to the Marlow FM studio **Comrex BRIC-Link** at `217.36.229.106`.

**Current working state (as of 2026-06-01):**
- Bidirectional AAC-LC audio over RTP, port 5004
- Python jitter buffer with 200ms playout window and packet-repetition loss concealment
- Web dashboard with calibrated dBFS meters at `http://172.16.10.213:8080`
- Auto-start on boot, managed by systemd

---

## 1. Hardware

| Component | Detail |
|---|---|
| Host | Pulse Secure PSA300 (Intel Celeron J1900, 8 GB RAM) |
| Audio interface | Behringer UCA202 (or UCA222 — identical chip). Texas Instruments PCM2902, USB class-compliant, no driver needed |
| Management NIC | `eno1` — LAN, carries `172.16.10.213/24`, default gateway `172.16.10.1` |
| WAN NIC | `eth0` — currently DOWN, reserved for future direct internet feeds |

> **NIC naming note:** On this PSA300, the NIC naming is the opposite of the original spec assumption. `eno1` is the LAN/management interface; `eth0` is the WAN interface. Verify with `ip -brief addr` on any new unit before configuring firewall rules.

---

## 2. OS Installation

Install **Ubuntu Server 24.04 LTS** via the hidden internal HDMI port on the PSA300 mainboard.

1. Flash a USB drive with Ubuntu Server 24.04 LTS minimal image.
2. Connect monitor, keyboard, and USB hub to the PSA300. Plug the bootable USB into the hub.
3. Power on → `Del` or `F2` → BIOS → enable integrated graphics, set USB as primary boot device.
4. Run the Ubuntu installer:
   - Set a static IP on the management NIC (e.g. `172.16.10.213/24`, gateway `172.16.10.1`)
   - Create user `marlowfm`
   - Enable **OpenSSH Server**
5. After first boot: `sudo poweroff`, remove peripherals, rack the unit.

---

## 3. System Configuration

### 3.1 Add user to audio group

```bash
sudo usermod -aG audio marlowfm
```

Log out and back in (or start a new SSH session) for the change to take effect.

### 3.2 CPU performance governor

Create `/etc/systemd/system/cpu-governor.service`:

```ini
[Unit]
Description=Set CPU Governor to Performance
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cpu-governor.service
```

Verify: `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` should print `performance`.

### 3.3 Network socket buffer sizes

Append to `/etc/sysctl.conf` (add once only):

```ini
net.core.rmem_max = 4194304
net.core.wmem_max = 4194304
```

Apply immediately:

```bash
sudo sysctl -p
```

---

## 4. Dependencies

```bash
sudo add-apt-repository universe
sudo apt update
sudo apt install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    gstreamer1.0-alsa \
    gstreamer1.0-fdkaac \
    alsa-utils \
    libfdk-aac2 \
    python3-gi \
    python3-gst-1.0 \
    python3-pip \
    python3-venv
```

> **Important:** `gstreamer1.0-alsa` and `gstreamer1.0-fdkaac` are separate packages on Ubuntu 24.04 — they are NOT bundled with `plugins-good` or `plugins-bad` despite what the package names imply. Both are required.

---

## 5. Verify the Behringer ALSA Device Index

Plug in the Behringer UCA202, then run:

```bash
aplay -l
```

On the PSA300, the Behringer enumerates as **card 0** because it registers before the onboard Intel HDA:

```
card 0: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
card 1: PCH [HDA Intel PCH], device 0: ...
```

The correct ALSA path is therefore **`hw:0,0`**. If the card index differs on your unit, update `alsa_device` in `config.json` accordingly.

Verify GStreamer can open the device:

```bash
timeout 5 gst-launch-1.0 alsasrc device=hw:0,0 num-buffers=20 ! fakesink
```

Expected: `Pipeline is PREROLLED ... Setting pipeline to PLAYING ... Got EOS`

---

## 6. Application Deployment

### 6.1 Directory structure

```
/opt/briclite/
├── config.json
├── main.py
├── core/
│   ├── __init__.py
│   ├── data_broker.py
│   └── pipeline_manager.py
└── web/
    └── templates/
        └── index.html
```

Create directories and set ownership:

```bash
sudo mkdir -p /opt/briclite/core /opt/briclite/web/templates
sudo chown -R marlowfm:marlowfm /opt/briclite
```

Create the Python virtual environment. The `--system-site-packages` flag is required so the venv can access `python3-gi` (PyGObject), which is an apt package and is not pip-installable:

```bash
python3 -m venv --system-site-packages /opt/briclite/venv
/opt/briclite/venv/bin/pip install fastapi 'uvicorn[standard]' pydantic
```

### 6.2 `config.json`

```json
{
  "system": {
    "device_mode": "STUDIO_RECEIVER",
    "web_port": 8080,
    "bind_address": "0.0.0.0"
  },
  "audio_network": {
    "target_ip": "217.36.229.106",
    "tx_port": 5004,
    "rx_port": 5004,
    "buffer_ms": 200,
    "alsa_device": "hw:0,0"
  },
  "expansion_future": {
    "hardware_display_enabled": false,
    "serial_port": "/dev/ttyACM0",
    "serial_baud": 115200,
    "openvpn_auto_connect": false,
    "openvpn_config_path": "/etc/openvpn/client/studio.conf"
  }
}
```

**`buffer_ms`** is the jitter buffer playout window. 200ms is suitable for broadband. Increase to 400–500ms for Starlink or satellite OB links.

**`target_ip`** is the remote end the PSA300 sends audio *to*. In Studio Receiver mode this is the Comrex BRIC-Link. In Remote Sender mode it would be the studio IP.

### 6.3 `core/__init__.py`

Empty file (marks `core` as a Python package):

```bash
touch /opt/briclite/core/__init__.py
```

### 6.4 `core/data_broker.py`

```python
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass
class ApplianceState:
    connection_status: str = "DISCONNECTED"
    runtime_seconds: int = 0
    tx_peak_l: float = -60.0
    tx_peak_r: float = -60.0
    rx_peak_l: float = -60.0
    rx_peak_r: float = -60.0
    jitter_avg: int = 0
    packets_lost: int = 0
    packets_late: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def update_metrics(self, data: Dict[str, Any]):
        async with self.lock:
            for key, val in data.items():
                if hasattr(self, key):
                    setattr(self, key, val)

    async def get_snapshot(self) -> Dict[str, Any]:
        async with self.lock:
            return {
                "status": self.connection_status,
                "runtime": self.runtime_seconds,
                "tx_peak_l": self.tx_peak_l,
                "tx_peak_r": self.tx_peak_r,
                "rx_peak_l": self.rx_peak_l,
                "rx_peak_r": self.rx_peak_r,
                "jitter": self.jitter_avg,
                "lost": self.packets_lost,
                "late": self.packets_late,
            }


global_state = ApplianceState()
```

### 6.5 `core/pipeline_manager.py`

```python
import gi
import threading
import asyncio
import logging
import socket
import struct
import random
import time

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
from .data_broker import global_state

logger = logging.getLogger("pipeline")

_WRAP      = 0x10000
_RTP_CLOCK = 90000
_AAC_SAMP  = 24000
_AAC_FRAME = 1024
_FRAME_S   = _AAC_FRAME / _AAC_SAMP
_TS_INC    = _AAC_FRAME * _RTP_CLOCK // _AAC_SAMP   # 3840 ticks/frame


def _seq_after(a: int, b: int) -> bool:
    """True if RTP sequence number b comes strictly after a (handles 16-bit wrap)."""
    return 0 < ((b - a) % _WRAP) < _WRAP // 2


class JitterBuffer:
    """
    Reorder buffer for incoming RTP AAC-ADTS streams.

    Thread-safe: push() from the network receive thread,
    pop() from the playout thread.

    - Holds packets for up to latency_ms before declaring a gap lost.
    - If a future packet arrives before the expected one, that packet is
      declared lost immediately (fast path for burst loss).
    - Lost packets are concealed by repeating the last good frame.
    - Tracks inter-arrival jitter (RFC 3550 EWMA method).
    """

    def __init__(self, latency_ms: int = 200):
        self.latency_s     = latency_ms / 1000.0
        self._buf          = {}
        self._next_seq     = None
        self._lock         = threading.Lock()
        self._event        = threading.Event()
        self.packets_lost  = 0
        self.packets_late  = 0
        self.jitter_ms     = 0.0
        self._last_arrival = None
        self._last_rtp_ts  = None

    def push(self, seq: int, rtp_ts: int, payload: bytes):
        now = time.monotonic()
        with self._lock:
            if self._next_seq is None:
                self._next_seq     = seq
                self._last_arrival = now
                self._last_rtp_ts  = rtp_ts
            # Discard late packets (already played or skipped)
            if self._next_seq is not None \
                    and not _seq_after(self._next_seq - 1, seq) \
                    and seq != self._next_seq:
                self.packets_late += 1
                return
            # Update inter-arrival jitter (RFC 3550 §A.8)
            if self._last_arrival is not None and self._last_rtp_ts is not None:
                d = abs(
                    (now - self._last_arrival)
                    - ((rtp_ts - self._last_rtp_ts) % (2**32)) / _RTP_CLOCK
                ) * 1000
                self.jitter_ms += (d - self.jitter_ms) / 16.0
            self._last_arrival = now
            self._last_rtp_ts  = rtp_ts
            self._buf[seq]     = payload
        self._event.set()

    def pop(self, last_good):
        """
        Blocking. Returns the next ADTS payload in sequence.
        Returns None  — not started yet, caller sleeps briefly.
        Returns b""   — packet lost, no concealment available.
        Returns bytes — real payload or last_good (concealment).
        """
        deadline = None
        while True:
            with self._lock:
                if self._next_seq is None:
                    pass
                elif self._next_seq in self._buf:
                    payload = self._buf.pop(self._next_seq)
                    self._next_seq = (self._next_seq + 1) % _WRAP
                    return payload
                else:
                    # Fast loss: a future packet has arrived, this one is gone
                    if any(_seq_after(self._next_seq, s) for s in self._buf):
                        self.packets_lost += 1
                        self._next_seq = (self._next_seq + 1) % _WRAP
                        return last_good or b""
                    # Slow loss: wait up to latency_s then declare lost
                    if deadline is None:
                        deadline = time.monotonic() + self.latency_s
                    if time.monotonic() >= deadline:
                        self.packets_lost += 1
                        self._next_seq = (self._next_seq + 1) % _WRAP
                        return last_good or b""
            self._event.wait(timeout=0.010)
            self._event.clear()

    def reset(self):
        with self._lock:
            self._buf.clear()
            self._next_seq     = None
            self._last_arrival = None
            self._last_rtp_ts  = None
            self.packets_lost  = 0
            self.packets_late  = 0
            self.jitter_ms     = 0.0
        self._event.set()


class PipelineController:

    def __init__(self, config: dict):
        Gst.init(None)
        net = config["audio_network"]
        self.alsa_dev   = net["alsa_device"]
        self.target_ip  = net["target_ip"]
        self.tx_port    = net["tx_port"]
        self.rx_port    = net["rx_port"]
        self.latency_ms = net.get("buffer_ms", 200)

        self.event_loop = None
        self.is_active  = False
        self.rtp_seq    = random.randint(0, 65535)
        self.rtp_ts     = random.randint(0, 0xFFFFFFFF)
        self.rtp_ssrc   = random.randint(0, 0xFFFFFFFF)

        self.jitter_buf  = JitterBuffer(latency_ms=self.latency_ms)
        self.tx_pipeline = None
        self.rx_pipeline = None
        self.rx_appsrc   = None
        self.sock        = None
        self.glib_loop   = GLib.MainLoop()

        self._build_pipelines()

    def _build_pipelines(self):
        # TX: capture from Behringer → resample to 24 kHz → AAC-LC ADTS → appsink
        # Python wraps each buffer in RTP PT=14 + RFC2250 and sends via shared socket.
        tx_str = (
            f"alsasrc device={self.alsa_dev} ! audioconvert ! "
            f"audio/x-raw,rate=44100,channels=2 ! "
            f"level name=tx_meter ! "
            f"audioresample ! audio/x-raw,rate=24000,channels=2 ! "
            f"avenc_aac ! aacparse ! "
            f"audio/mpeg,mpegversion=4,stream-format=adts ! "
            f"appsink name=tx_sink emit-signals=true sync=false"
        )
        # RX: appsrc receives ADTS frames from Python jitter buffer
        # → decode AAC → play to Behringer headphone/line output.
        rx_str = (
            f"appsrc name=rx_src is-live=true format=time block=false ! "
            f"queue max-size-buffers=10 ! "
            f"audio/mpeg,mpegversion=4,stream-format=adts ! "
            f"avdec_aac ! audioconvert ! "
            f"level name=rx_meter ! audioresample ! audiorate ! "
            f"audio/x-raw,rate=44100,channels=2 ! "
            f"alsasink device={self.alsa_dev} sync=false"
        )
        logger.info(f"TX: {tx_str}")
        logger.info(f"RX: {rx_str}")

        self.tx_pipeline = Gst.parse_launch(tx_str)
        self.rx_pipeline = Gst.parse_launch(rx_str)

        self.tx_pipeline.get_by_name("tx_sink").connect(
            "new-sample", self._on_tx_sample
        )
        self.rx_appsrc = self.rx_pipeline.get_by_name("rx_src")
        self.rx_appsrc.set_property(
            "caps",
            Gst.Caps.from_string("audio/mpeg,mpegversion=4,stream-format=adts")
        )

        for p in (self.tx_pipeline, self.rx_pipeline):
            bus = p.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message)

    def start(self, event_loop):
        self.event_loop = event_loop
        self.jitter_buf.reset()

        # One socket bound to rx_port. Outgoing packets carry this as source port
        # so the Comrex replies to the same port we are listening on.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)
        self.sock.settimeout(0.5)
        self.sock.bind(("0.0.0.0", self.rx_port))

        self.tx_pipeline.set_state(Gst.State.PLAYING)
        self.rx_pipeline.set_state(Gst.State.PLAYING)
        self.is_active = True

        threading.Thread(target=self.glib_loop.run,   daemon=True).start()
        threading.Thread(target=self._rx_loop,        daemon=True).start()
        threading.Thread(target=self._playout_loop,   daemon=True).start()

        asyncio.run_coroutine_threadsafe(
            global_state.update_metrics({"connection_status": "CONNECTED"}),
            event_loop
        )
        asyncio.run_coroutine_threadsafe(self._poll_stats_loop(), event_loop)
        logger.info(
            f"Started (jitter buf {self.latency_ms} ms): "
            f"TX→{self.target_ip}:{self.tx_port}  RX←:{self.rx_port}"
        )

    def stop(self, event_loop):
        self.is_active = False
        self.tx_pipeline.set_state(Gst.State.NULL)
        self.rx_pipeline.set_state(Gst.State.NULL)
        self.glib_loop.quit()
        self.jitter_buf.reset()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(
            global_state.update_metrics({
                "connection_status": "DISCONNECTED",
                "tx_peak_l": -60.0, "tx_peak_r": -60.0,
                "rx_peak_l": -60.0, "rx_peak_r": -60.0,
                "jitter": 0, "lost": 0,
            }),
            event_loop
        )

    # ---------------------------------------------------------------- TX
    def _on_tx_sample(self, appsink):
        """GStreamer callback: one AAC-ADTS buffer ready. Wrap in RTP and send."""
        sample = appsink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf  = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())

        # Standard RTP header (12 bytes) + RFC2250 4-byte header (MBZ=0, offset=0).
        # The Comrex BRIC-Link expects RFC2250 framing on incoming PT=14 streams.
        header = struct.pack("!BBHII",
            0x80, 14,                      # V=2, PT=14 (MPEG Audio)
            self.rtp_seq & 0xFFFF,
            self.rtp_ts  & 0xFFFFFFFF,
            self.rtp_ssrc
        ) + b"\x00\x00\x00\x00"

        try:
            self.sock.sendto(header + data, (self.target_ip, self.tx_port))
        except Exception as e:
            logger.warning(f"TX send: {e}")

        self.rtp_seq += 1
        self.rtp_ts  = (self.rtp_ts + _TS_INC) & 0xFFFFFFFF
        return Gst.FlowReturn.OK

    # ---------------------------------------------------------------- RX receive thread
    def _rx_loop(self):
        """Receive UDP packets, strip RTP header, push ADTS to jitter buffer."""
        while self.is_active:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                break

            if len(data) < 12:
                continue

            cc      = data[0] & 0x0F
            has_ext = (data[0] >> 4) & 0x1
            seq     = struct.unpack("!H", data[2:4])[0]
            rtp_ts  = struct.unpack("!I", data[4:8])[0]
            off     = 12 + cc * 4
            if has_ext and len(data) >= off + 4:
                off += 4 + struct.unpack("!H", data[off + 2: off + 4])[0] * 4

            if len(data) <= off:
                continue
            payload = data[off:]

            # Strip RFC2250 header if present (first two bytes are MBZ=0x0000)
            if len(payload) >= 4 and payload[0] == 0x00 and payload[1] == 0x00:
                payload = payload[4:]

            # Accept only valid ADTS frames (sync word 0xFFF)
            if len(payload) < 2 or payload[0] != 0xFF or (payload[1] & 0xF0) != 0xF0:
                continue

            self.jitter_buf.push(seq, rtp_ts, bytes(payload))

    # ---------------------------------------------------------------- RX playout thread
    def _playout_loop(self):
        """Pop from jitter buffer at codec rate and push to GStreamer appsrc."""
        FRAME_DUR = Gst.SECOND * _AAC_FRAME // _AAC_SAMP   # ~42 666 667 ns
        rx_pts    = 0
        last_good = None

        while self.is_active:
            payload = self.jitter_buf.pop(last_good)

            if payload is None:
                time.sleep(0.010)
                continue

            if payload:
                last_good = payload   # keep for concealment on next loss

            if not payload:
                # Lost packet with no concealment available yet — advance clock
                rx_pts += FRAME_DUR
                continue

            buf          = Gst.Buffer.new_wrapped(payload)
            buf.pts      = rx_pts
            buf.duration = FRAME_DUR
            rx_pts      += FRAME_DUR

            ret = self.rx_appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                logger.warning(f"appsrc push: {ret}")

    # ---------------------------------------------------------------- bus messages
    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            logger.error(f"GST ERROR: {err} | {dbg}")
        elif t == Gst.MessageType.WARNING:
            w, _ = message.parse_warning()
            logger.warning(f"GST WARN: {w}")
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src in (self.tx_pipeline, self.rx_pipeline):
                old, new, _ = message.parse_state_changed()
                label = "TX" if message.src == self.tx_pipeline else "RX"
                logger.info(f"{label}: {old.value_nick}→{new.value_nick}")
        elif t == Gst.MessageType.ELEMENT:
            s = message.get_structure()
            if s and s.get_name() == "level":
                peaks = s.get_value("peak")
                if peaks and len(peaks) >= 2:
                    db_l = max(-60.0, round(float(peaks[0]), 1))
                    db_r = max(-60.0, round(float(peaks[1]), 1))
                    src  = message.src.get_name() if message.src else ""
                    kl, kr = ("tx_peak_l", "tx_peak_r") if src == "tx_meter" \
                             else ("rx_peak_l", "rx_peak_r")
                    asyncio.run_coroutine_threadsafe(
                        global_state.update_metrics({kl: db_l, kr: db_r}),
                        self.event_loop
                    )

    async def _poll_stats_loop(self):
        while self.is_active:
            await asyncio.sleep(1.0)
            await global_state.update_metrics({
                "jitter": int(self.jitter_buf.jitter_ms),
                "lost":   self.jitter_buf.packets_lost,
                "late":   self.jitter_buf.packets_late,
            })
```

### 6.6 `main.py`

```python
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.data_broker import global_state
from core.pipeline_manager import PipelineController

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

with open("/opt/briclite/config.json") as f:
    config = json.load(f)

controller: Optional[PipelineController] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller
    controller = PipelineController(config)
    yield


app = FastAPI(title="PSA300 Broadcast Codec Core", lifespan=lifespan)


class ConnectRequest(BaseModel):
    target_ip: Optional[str] = None


@app.post("/api/connect")
async def connect_codec(body: ConnectRequest = ConnectRequest()):
    global controller
    loop = asyncio.get_event_loop()
    if controller.is_active:
        return {"status": "error", "message": "Already running"}
    if body.target_ip:
        config["audio_network"]["target_ip"] = body.target_ip
    controller = PipelineController(config)
    controller.start(loop)
    return {"status": "success", "message": "Pipeline active"}


@app.post("/api/disconnect")
async def disconnect_codec():
    loop = asyncio.get_event_loop()
    if not controller.is_active:
        return {"status": "error", "message": "Pipeline inactive"}
    controller.stop(loop)
    return {"status": "success", "message": "Pipeline halted"}


@app.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await asyncio.sleep(0.1)
            snapshot = await global_state.get_snapshot()
            await websocket.send_json(snapshot)
    except WebSocketDisconnect:
        pass


@app.get("/")
async def get_dashboard():
    with open("/opt/briclite/web/templates/index.html") as f:
        return HTMLResponse(content=f.read(), status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app",
                host=config["system"]["bind_address"],
                port=config["system"]["web_port"])
```

### 6.7 `web/templates/index.html`

See the deployed file at `/opt/briclite/web/templates/index.html` on the running unit, or copy from the reference unit at `172.16.10.213`. The dashboard provides:
- TX Input level meters (blue, dBFS scale)
- RX Output level meters (green/amber/red, dBFS scale)
- -18 dBFS alignment line (broadcast reference = 0 PPM)
- Numeric peak readout per pair
- Rolling jitter chart
- Connect/Disconnect controls with optional field unit IP field

---

## 7. Systemd Service

Create `/etc/systemd/system/briclite.service`:

```ini
[Unit]
Description=Marlow FM PSA300 Broadcast Codec
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/briclite/venv/bin/python /opt/briclite/main.py
WorkingDirectory=/opt/briclite
Restart=on-failure
RestartSec=5
User=marlowfm
Environment=GST_DEBUG=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now briclite.service
```

`GST_DEBUG=2` keeps GStreamer error and warning messages visible in `journalctl`. Remove it once the unit is stable in production.

---

## 8. Firewall

On this PSA300, `eno1` is the LAN/management interface and `eth0` is the WAN. Adjust interface names if they differ on your unit.

```bash
sudo ufw allow in on eno1 to any port 22   proto tcp   # SSH
sudo ufw allow in on eno1 to any port 8080 proto tcp   # Web UI
sudo ufw allow in on eno1 to any port 5004 proto udp   # RTP (LAN connections)
sudo ufw allow in on eth0 to any port 5004 proto udp   # RTP (WAN/internet connections)
sudo ufw --force enable
```

> **Warning:** Always add all rules before running `ufw enable`. Enabling first with incomplete rules will lock you out of SSH. If locked out: attach a keyboard/monitor and run `sudo ufw disable`.

---

## 9. Testing

### 9.1 Verify GStreamer pipeline manually (before relying on the service)

Stop the service first so ALSA is free:

```bash
sudo systemctl stop briclite.service
```

TX path — encode and transmit (replace IP with your target):
```bash
timeout 10 gst-launch-1.0 \
    alsasrc device=hw:0,0 num-buffers=200 ! audioconvert ! \
    audio/x-raw,rate=44100,channels=2 ! audioresample ! \
    audio/x-raw,rate=24000,channels=2 ! \
    avenc_aac ! aacparse ! \
    audio/mpeg,mpegversion=4,stream-format=adts ! fakesink
```

Expected: `Pipeline is PREROLLED ... Setting pipeline to PLAYING`

RX path — loopback test (encode, transmit UDP, receive, decode):
```bash
timeout 10 gst-launch-1.0 \
    alsasrc device=hw:0,0 num-buffers=200 ! audioconvert ! \
    audio/x-raw,rate=44100,channels=2 ! audioresample ! \
    audio/x-raw,rate=24000,channels=2 ! \
    avenc_aac ! aacparse ! audio/mpeg,mpegversion=4,stream-format=adts ! \
    rtpmpapay pt=14 ! udpsink host=127.0.0.1 port=5099 \
    udpsrc port=5099 buffer-size=4194304 \
        caps="application/x-rtp,media=audio,clock-rate=90000,encoding-name=MPA,payload=14" ! \
    rtpjitterbuffer latency=300 ! rtpmpadepay ! \
    avdec_aac ! audioconvert ! fakesink
```

### 9.2 Live connection test

Start the service, open the web UI, click Connect:

```bash
sudo systemctl start briclite.service
```

Web UI: `http://172.16.10.213:8080`

Verify with tcpdump:
```bash
sudo tcpdump -i eno1 -n 'host 217.36.229.106 and port 5004' -c 20
```

Expected: alternating packets PSA300→Comrex and Comrex→PSA300. Comrex packets are variable-size (~490–580 bytes) when AAC is correctly detected. 172-byte fixed packets indicate the Comrex has fallen back to G.722 — do a Disconnect/Connect cycle to reset.

---

## 10. Connection Protocol Reference

The Comrex BRIC-Link uses a **proprietary BRIC protocol** on TCP port 3020 (TLS-encrypted) for device management and a **raw UDP RTP stream** on port 5004 for audio. There is no SIP negotiation for the audio path.

The PSA300 codec replicates the protocol used by **Luci Live**, confirmed by packet capture analysis:

| Parameter | Value |
|---|---|
| Transport | UDP, port 5004 both directions |
| RTP payload type | 14 (nominally "MPEG Audio") |
| RTP clock rate | 90000 Hz |
| Codec | AAC-LC in ADTS format |
| Sample rate | 24000 Hz stereo |
| RTP framing | 12-byte RTP header + 4-byte RFC2250 header (MBZ=0, offset=0) + ADTS frame |
| RTP timestamp increment | 3840 ticks per frame (1024 samples × 90000/24000) |
| Comrex → PSA300 | AAC-ADTS, PT=14, no RFC2250 header (raw ADTS follows RTP header directly) |

**Comrex codec auto-detection:** The BRIC-Link auto-detects incoming codec from the payload bytes (ADTS sync word `0xFFF`). It does NOT auto-detect OPUS — OPUS requires SIP negotiation (see Section 12).

**Shared socket:** Both TX (outgoing) and RX (incoming) use the same UDP socket bound to port 5004. This ensures the Comrex always replies to port 5004 rather than the OS-assigned ephemeral source port the TX socket would otherwise use.

---

## 11. Operational Notes

**First connection after power-on:** The Comrex will show G.722 VoIP briefly while it detects the AAC stream. Within 2–3 seconds it switches to showing the correct AAC codec. If it stays on G.722, click Disconnect then Connect again.

**Jitter buffer depth:** Default 200ms is correct for broadband. For Starlink outside broadcasts, change `buffer_ms` in `config.json` to 400–500 and restart the service. No code change needed.

**Headphone volume:** The Behringer UCA202's headphone output volume is controlled by the physical knob on the front panel. ALSA software volume is at 0dB (maximum). Correct reference level for broadcast is -18dBFS (marked in red on the RX meters).

**ALSA device:** The Behringer enumerates as `hw:0,0` on this PSA300 because USB audio devices register before the onboard Intel HDA. Verify with `aplay -l` on any new unit.

**Duplicate sysctl entries:** During the initial build session, sysctl buffer entries were appended twice to `/etc/sysctl.conf`. This is harmless but can be cleaned up with `sudo nano /etc/sysctl.conf`.

---

## 12. Known Limitations and Future Work

### OPUS codec

The Comrex BRIC-Link II firmware 5.2-p2 supports OPUS, but only via SIP negotiation. When the PSA300 sends RTP PT=111 (standard OPUS) without a prior SIP INVITE containing an SDP `a=rtpmap:111 opus/48000/2` line, the BRIC-Link falls back to G.722 VoIP mode and does not decode the OPUS stream.

OPUS is significantly better than AAC for outside broadcast use:
- Built-in in-band FEC: lost packets reconstructed from the next packet
- 20ms frames (vs 42ms for AAC at 24kHz) — finer-grained jitter buffer operation
- Better quality at constrained bitrates (64kbps+)
- Native PLC at the decoder

**To implement OPUS:** Add a SIP layer using `python-sipsimple` or `pjsua2`. The SIP INVITE negotiates the codec; RTP audio then flows on port 5004 as usual. This is a separate phase of development.

### Packet loss concealment

Current concealment is packet repetition (repeating the last good frame). For single-packet losses this is acceptable; for burst losses it produces a stuttering artefact. Better approaches in order of complexity:
1. Longer concealment (repeat for up to N frames, then fade to silence)
2. Simple linear interpolation between last and next good frame
3. Full algorithmic PLC (requires codec-specific implementation in C)

### Forward Error Correction

Not implemented. Would require either:
- OPUS in-band FEC (via the SIP path above)
- RFC 5109 ULPFEC (requires Comrex support for the same scheme)
- Custom XOR FEC in a C module (works for PSA-to-PSA links)

### Clock drift

On long sessions (several hours), the Comrex clock and the Behringer ALSA clock will drift by tens of milliseconds. `audiorate` in the RX pipeline inserts or drops samples to maintain continuity, but it is working blind because RTP timestamps are not used for PTS derivation. A future improvement is to derive buffer PTS from RTP timestamps, which allows `audiorate` to detect and compensate drift correctly.

---

## 13. Deployment Checklist (fresh unit)

- [ ] Ubuntu Server 24.04 LTS installed, user `marlowfm` created
- [ ] SSH working: `ssh marlowfm@<ip>`
- [ ] Passwordless sudo: `sudo -n whoami` returns `root`
- [ ] `marlowfm` added to `audio` group
- [ ] Behringer plugged in; `aplay -l` confirms card index (update `config.json` if not `hw:0,0`)
- [ ] All GStreamer packages installed including `gstreamer1.0-alsa` and `gstreamer1.0-fdkaac`
- [ ] Python venv created with `--system-site-packages`
- [ ] FastAPI/uvicorn installed in venv
- [ ] All application files deployed to `/opt/briclite/`
- [ ] Manual GStreamer TX test passes (no errors)
- [ ] CPU governor service enabled and running (`cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` = `performance`)
- [ ] `sysctl.conf` buffer entries added once (not duplicated)
- [ ] UFW rules applied with correct interface names for this unit
- [ ] `briclite.service` enabled and starts cleanly
- [ ] Web UI loads at `http://<management-ip>:8080`
- [ ] Connect button establishes bidirectional flow (tcpdump shows variable-size AAC packets from Comrex)
- [ ] TX Input meters respond to audio on Behringer inputs
- [ ] RX Output meters show Comrex audio; headphones produce audio at -18dBFS reference level

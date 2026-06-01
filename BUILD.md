# Broadcast Audio Codec — Build & Deployment Guide

This document provides the complete from-scratch build guide for turning an Ubuntu server and a Behringer UCA202 USB audio interface into a standalone bidirectional broadcast audio codec.

**Current working state:**
- Bidirectional AAC-LC audio over RTP, port 5004
- Python jitter buffer with 200ms playout window and packet-repetition loss concealment
- Web dashboard with calibrated dBFS meters
- Auto-start on boot, managed by systemd

---

## 1. Hardware Requirements

| Component | Specification |
|---|---|
| Host | Ubuntu server with Intel Celeron J1900 or equivalent (2 cores, 1.6–2.2 GHz) |
| Memory | 8 GB RAM |
| Network | Two Ethernet ports recommended (one for studio LAN, one for WAN/satellite) |
| Audio interface | Behringer UCA202 (or UCA222). Texas Instruments PCM2902, USB class-compliant, no drivers needed |

---

## 2. OS Installation

Install **Ubuntu Server 24.04 LTS** on the target hardware.

1. Flash a USB drive with Ubuntu Server 24.04 LTS minimal image.
2. Connect monitor, keyboard, and USB hub. Plug the bootable USB into the hub.
3. Boot to BIOS, enable integrated graphics if needed, set USB as primary boot device.
4. Run the Ubuntu installer:
   - Set a static IP on the primary NIC (example: `10.0.0.50/24`)
   - Create a service user (example: `codec`)
   - Enable **OpenSSH Server**
5. After first boot: `sudo poweroff`, remove peripherals, rack the unit.

---

## 3. System Configuration

### 3.1 Add user to audio group

```bash
sudo usermod -aG audio codec
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

The Behringer should enumerate as **card 0** if it's the first audio device. Verify the ALSA path is `hw:0,0`. If the card index differs on your unit, update `alsa_device` in `config.json` accordingly.

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
sudo chown -R codec:codec /opt/briclite
```

Create the Python virtual environment. The `--system-site-packages` flag is required so the venv can access `python3-gi` (PyGObject), which is an apt package and is not pip-installable:

```bash
python3 -m venv --system-site-packages /opt/briclite/venv
/opt/briclite/venv/bin/pip install fastapi 'uvicorn[standard]' pydantic
```

### 6.2 `config.json` (from template)

Copy the template and customize with your network details:

```bash
cp /opt/briclite/briclite/config.example.json /opt/briclite/config.json
nano /opt/briclite/config.json
```

Edit these fields:

```json
{
  "system": {
    "device_mode": "STUDIO_RECEIVER",
    "web_port": 8080,
    "bind_address": "0.0.0.0"
  },
  "audio_network": {
    "target_ip": "YOUR_STUDIO_IP",
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

**`target_ip`** — Replace `YOUR_STUDIO_IP` with the actual IP of your studio console or distribution system.

**`buffer_ms`** — Jitter buffer playout window. 200ms is suitable for broadband. Increase to 400–500ms for Starlink or satellite OB links.

**`alsa_device`** — Should be `hw:0,0` if Behringer is the first USB audio device. Verify with `aplay -l` and adjust if needed.

### 6.3 Deploy code from repository

Copy the application files to `/opt/briclite/`:

```bash
# Assuming you've cloned the repo to /home/codec/broadcast-audio-codec
cp -r /home/codec/broadcast-audio-codec/briclite/* /opt/briclite/
chmod +x /opt/briclite/main.py
```

Ensure the `core` directory is marked as a Python package:

```bash
touch /opt/briclite/core/__init__.py
```

### 6.4 `core/data_broker.py`

See `briclite/core/data_broker.py` in the repository.

### 6.5 `core/pipeline_manager.py`

See `briclite/core/pipeline_manager.py` in the repository.

### 6.6 `main.py`

See `briclite/main.py` in the repository.

### 6.7 `web/templates/index.html`

See `briclite/web/templates/index.html` in the repository. The dashboard provides:
- TX Input level meters (blue, dBFS scale)
- RX Output level meters (green/amber/red, dBFS scale)
- -18 dBFS alignment line (broadcast reference = 0 PPM)
- Numeric peak readout per channel pair
- Rolling jitter chart
- Connect/Disconnect controls with optional remote IP field

---

## 7. Systemd Service

Create `/etc/systemd/system/briclite.service`:

```ini
[Unit]
Description=Broadcast Audio Codec
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/briclite/venv/bin/python /opt/briclite/main.py
WorkingDirectory=/opt/briclite
Restart=on-failure
RestartSec=5
User=codec
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

Configure UFW to allow SSH, web UI, and RTP audio. Adjust interface names and IP ranges to match your network topology.

Example for a studio with management LAN on `eno1` and internet feed on `eth0`:

```bash
sudo ufw allow in on eno1 to any port 22   proto tcp   # SSH from LAN
sudo ufw allow in on eno1 to any port 8080 proto tcp   # Web UI from LAN
sudo ufw allow in on eno1 to any port 5004 proto udp   # RTP from LAN
sudo ufw allow in on eth0 to any port 5004 proto udp   # RTP from internet/satellite
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

TX path — encode and transmit:
```bash
timeout 10 gst-launch-1.0 \
    alsasrc device=hw:0,0 num-buffers=200 ! audioconvert ! \
    audio/x-raw,rate=44100,channels=2 ! audioresample ! \
    audio/x-raw,rate=24000,channels=2 ! \
    avenc_aac ! aacparse ! \
    audio/mpeg,mpegversion=4,stream-format=adts ! fakesink
```

Expected: `Pipeline is PREROLLED ... Setting pipeline to PLAYING`

### 9.2 Live connection test

Start the service, open the web UI, click Connect:

```bash
sudo systemctl start briclite.service
```

Web UI: `http://10.0.0.50:8080` (adjust IP as needed)

Verify with tcpdump:
```bash
sudo tcpdump -i eno1 -n 'port 5004' -c 20
```

Expected: alternating packets from the server and the remote end. If the remote device supports codec auto-detection, it will show acknowledgment packets of varying sizes once it detects the incoming AAC stream.

---

## 10. Connection Protocol Reference

The codec uses a **raw UDP RTP stream** on port 5004 for audio transport. There is no SIP negotiation for the audio path (see Section 12 for OPUS considerations).

| Parameter | Value |
|---|---|
| Transport | UDP, port 5004 both directions |
| RTP payload type | 14 (nominally "MPEG Audio") |
| RTP clock rate | 90000 Hz |
| Codec | AAC-LC in ADTS format |
| Sample rate | 24000 Hz stereo |
| RTP framing | 12-byte RTP header + 4-byte RFC2250 header (MBZ=0, offset=0) + ADTS frame |
| RTP timestamp increment | 3840 ticks per frame (1024 samples × 90000/24000) |

**Codec auto-detection:** Remote devices that support codec auto-detection will identify the incoming codec from the payload bytes (ADTS sync word `0xFFF`). They do NOT auto-detect OPUS — OPUS requires SIP negotiation (see Section 12).

**Shared socket:** Both TX (outgoing) and RX (incoming) use the same UDP socket bound to port 5004. This ensures the remote device always replies to port 5004 rather than the OS-assigned ephemeral source port the TX socket would otherwise use.

---

## 11. Operational Notes

**First connection:** The remote device may briefly show a fallback codec (e.g., G.722 VoIP) while it detects the AAC stream. Within 2–3 seconds it should switch to showing the correct AAC codec. If it stays on the fallback, click Disconnect then Connect again.

**Jitter buffer depth:** Default 200ms is correct for broadband. For Starlink outside broadcasts, change `buffer_ms` in `config.json` to 400–500 and restart the service. No code change needed.

**Headphone/line output volume:** The Behringer UCA202's output volume is controlled by the physical knob on the front panel. ALSA software volume is at 0dB (maximum). Correct reference level for broadcast is -18dBFS (marked on the RX meters).

**ALSA device enumeration:** USB audio devices typically enumerate as `hw:0,0` if they're the first audio device. Verify with `aplay -l` on your unit.

---

## 12. Known Limitations and Future Work

### OPUS codec

Remote devices may support OPUS, but typically only via SIP negotiation. When sending RTP PT=111 (standard OPUS) without a prior SIP INVITE containing an SDP line, the remote device may fall back to a default codec and not decode the OPUS stream.

OPUS would be significantly better than AAC for outside broadcast use:
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
- RFC 5109 ULPFEC (requires remote device support for the same scheme)
- Custom XOR FEC in a C module (works for peer-to-peer links)

### Clock drift

On long sessions (several hours), the remote device clock and the Behringer ALSA clock will drift by tens of milliseconds. `audiorate` in the RX pipeline inserts or drops samples to maintain continuity, but it is working blind because RTP timestamps are not used for PTS derivation. A future improvement is to derive buffer PTS from RTP timestamps, which allows `audiorate` to detect and compensate drift correctly.

---

## 13. Deployment Checklist (fresh unit)

- [ ] Ubuntu Server 24.04 LTS installed, service user created (e.g., `codec`)
- [ ] SSH working and passwordless sudo configured
- [ ] Service user added to `audio` group
- [ ] Behringer plugged in; `aplay -l` confirms device enumeration
- [ ] All GStreamer packages installed including `gstreamer1.0-alsa` and `gstreamer1.0-fdkaac`
- [ ] Python venv created with `--system-site-packages`
- [ ] FastAPI/uvicorn installed in venv
- [ ] All application files deployed to `/opt/briclite/`
- [ ] `config.json` created from `config.example.json` and customized with your remote IP
- [ ] `config.json` is NOT tracked in git (kept local to this server)
- [ ] Manual GStreamer TX test passes (no errors)
- [ ] CPU governor service enabled and running
- [ ] `sysctl.conf` buffer entries added
- [ ] UFW rules applied with correct interface names for your network
- [ ] `briclite.service` enabled and starts cleanly
- [ ] Web UI loads at `http://<server-ip>:8080`
- [ ] Connect button establishes bidirectional flow
- [ ] TX Input meters respond to audio on Behringer inputs
- [ ] RX Output meters show remote audio; output at correct level

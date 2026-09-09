# Troubleshooting Guide

## Issue: Audio not flowing (no sound at remote, TX/RX meters frozen)

### Step 1: Check service status
```bash
ssh codec@<server-ip>
sudo systemctl status briclite
sudo journalctl -u briclite -n 50
```

Look for errors like:
- `GST ERROR` — GStreamer pipeline failed
- `TX send: [Errno X]` — Network issue
- `avenc_aac` — Encoder issue

### Step 2: Test ALSA device
```bash
aplay -l  # Verify Behringer is hw:0,0
arecord -D hw:0,0 -f cd /tmp/test.wav &  # Record 3 seconds
sleep 3 && pkill -f arecord
aplay -D hw:0,0 /tmp/test.wav  # Listen back
```

If no sound: Behringer is not enumerated correctly. Check USB connection, run `aplay -l` again, update `config.json` if card index differs.

### Step 3: Test GStreamer pipeline manually
```bash
sudo systemctl stop briclite  # Release ALSA

# Simple capture test
timeout 5 gst-launch-1.0 alsasrc device=hw:0,0 num-buffers=100 ! fakesink

# Capture + encode test
timeout 5 gst-launch-1.0 \
    alsasrc device=hw:0,0 num-buffers=100 ! audioconvert ! \
    audio/x-raw,rate=44100,channels=2 ! audioresample ! \
    audio/x-raw,rate=24000,channels=2 ! \
    avenc_aac ! aacparse ! \
    audio/mpeg,mpegversion=4,stream-format=adts ! fakesink
```

Expected: `Pipeline is PREROLLED ... Setting pipeline to PLAYING ... Got EOS`

If it hangs or errors: GStreamer/codec issue. Check `GST_DEBUG=3` output for details.

### Step 4: Check network
```bash
# Can you reach the remote?
ping -c 3 192.0.2.100  # Use your actual studio IP

# Are packets flowing?
sudo tcpdump -i eth0 -n 'port 5004' -c 10
```

Look for:
- `<server> > 192.0.2.100.5004` — TX packets going out
- `192.0.2.100.5004 > <server>` — RX packets coming in

If only TX, no RX: Remote device may not be sending, or port is wrong.

---

## Issue: RX meters frozen, no audio from remote

**Note (2026-09-09):** if the RX meter shows a *frozen non-zero* value (not decaying to -60 dBFS) and `jitter`/`lost`/`late` in telemetry are also frozen/unmoving, the whole playout loop has stalled — this is different from genuine silence and has several possible causes found this session: an ALSA/socket race on reconnect (fixed — see `stop()`'s `get_state(Gst.SECOND)` calls and `SO_REUSEADDR` in `pipeline_manager.py`), or (GoXLR only) the `audiomixer` issue described further down this file. Check those first if the symptom below (non-ADTS packets, meter pinned at exactly -60) doesn't match.

### Common cause: Jitter buffer received non-ADTS packets

**Symptom:** RX meters show -60 dBFS (minimum) and don't respond to remote audio.

**Reason:** The jitter buffer expects ADTS frames (sync word 0xFFF). If the remote device is sending a different codec or RTP payload format, the buffer discards all packets.

**Diagnosis:**
```bash
sudo tcpdump -i eth0 -nn 'port 5004' -X -c 5
```

Look at the hex dump of incoming packets. After the 12-byte RTP header, the payload should start with `FF Fx` (where x is any hex digit). This is the ADTS sync word. If not, the remote is sending something else.

**Solution:**
1. Check the remote device's RTP settings. Ensure it's set to:
   - Codec: AAC or MPEG Audio
   - RTP payload type: 14
   - Sample rate: 24 kHz (if configurable)

2. If the remote fell back to a different codec, do a disconnect/reconnect:
   ```bash
   curl -X POST http://localhost:8080/api/disconnect
   sleep 3
   curl -X POST http://localhost:8080/api/connect
   sleep 2
   ```

3. If still frozen, check the web dashboard at `http://<server-ip>:8080`. The "Connection Status" should show "CONNECTED". If it says "DISCONNECTED", the API call failed.

---

## Issue: Remote device shows G.722 VoIP instead of AAC

### On first connection:
Expected behavior. The remote device auto-detects the incoming codec. It may show G.722 or unknown codec for 2-3 seconds, then switch to AAC once it receives the first valid ADTS frame.

**Solution:** Wait 3 seconds. If it still shows G.722 after 10 seconds, do a disconnect/reconnect.

### After previously working:
The remote device's session state cached the wrong codec. Do a full disconnect/reconnect:

```bash
# From the Mac
ssh codec@<server-ip>
curl -X POST http://localhost:8080/api/disconnect
sleep 5
curl -X POST http://localhost:8080/api/connect
```

Wait 3 seconds, check the remote device's status.

---

## Issue: High jitter or packet loss spikes

### Step 1: Check network conditions
```bash
# Measure latency and loss to the remote
ping -i 0.1 -c 50 192.0.2.100  # Replace with your studio IP
```

High latency (RTT > 100ms) or packet loss (>1%) indicates network problems, not codec issues.

### Step 2: Increase jitter buffer depth
If the network is slow but stable:
```bash
ssh codec@<server-ip>
nano /opt/briclite/config.json
# Change "buffer_ms": 200 to 400 or 500
sudo systemctl restart briclite
```

Larger buffer = more latency tolerated, but also more playback delay (noticeable if >500ms).

### Step 3: Check CPU/system load
```bash
ssh codec@<server-ip>
top -b -n 1 | head -20
```

If CPU is at 100% or load average is high (>2 on dual-core), the system is struggling. Check:
- If any other services are running
- If the venv is in a bad state (try `sudo systemctl restart briclite`)
- If disk is full (`df -h`)

---

## Issue: Cannot SSH to server

### Check connectivity
```bash
ping <server-ip>
# If fails, server is offline or unreachable
```

### Check SSH service
From the server itself (keyboard + monitor):
```bash
sudo systemctl status ssh
sudo journalctl -u ssh -n 20
```

### Check firewall
```bash
sudo ufw status
# Ensure port 22 is allowed on the management interface
```

---

## Issue: Behringer not recognized on new deployment

### Check ALSA
```bash
aplay -l
```

If Behringer doesn't appear:
1. Check USB cable connection
2. Plug into a different USB port
3. Reboot the system
4. Check if it shows up as `CODEC` or `USB Audio CODEC`

### Update config.json
Once it appears in `aplay -l`, note the card index:
```
card 0: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
```

Edit `config.json`:
```json
"alsa_device": "hw:0,0"  # Replace 0,0 with your card,device
```

Restart the service:
```bash
sudo systemctl restart briclite
```

---

## Issue: Web dashboard won't load

### Check if FastAPI is running
```bash
ps aux | grep main.py
```

If no `main.py` process, the service failed to start:
```bash
sudo systemctl status briclite
sudo journalctl -u briclite
```

### Check port
```bash
sudo netstat -tlnp | grep 8080
# Should show: tcp 0.0.0.0:8080 LISTEN
```

If not listening:
```bash
curl http://localhost:8080
# If connection refused, FastAPI is down
```

### Check firewall
```bash
sudo ufw status
# Ensure 8080/tcp is allowed on the management interface
```

### Try direct access
From the server:
```bash
curl -v http://localhost:8080/
```

Should return HTML. If connection refused, restart the service.

---

## Issue: "Already running" error when clicking Connect

### Symptoms:
Web dashboard shows "Pipeline active" but audio isn't flowing, clicking Connect returns "Already running".

### Diagnosis:
The pipeline is stuck in a bad state. Restart the service:

```bash
ssh codec@<server-ip>
sudo systemctl restart briclite
sleep 2
# Try connecting again
```

If it persists, check logs:
```bash
sudo journalctl -u briclite -n 50 | grep ERROR
```

---

## Issue: Audio loops/repeats after switching RX channel routing

### Symptom
After clicking Left Only / Right Only / L+R Stereo, the same half-second of audio repeats continuously.

### Cause
Switching the channel routing mode rebuilds the RX GStreamer pipeline. If the old pipeline has not fully released the ALSA device before the new one opens it, ALSA gets stuck replaying buffered audio. This can be triggered by clicking rapidly between modes.

### Solution
Restart the service to clear the ALSA state:
```bash
sudo systemctl restart briclite
```

The code uses a 250ms debounce and blocks on `get_state()` after `set_state(NULL)` to prevent this race. If it recurs, increase the debounce delay in `set_rx_channel_mode` in `pipeline_manager.py`.

**Updated 2026-09-09:** this in-place RX-only rebuild (`_do_rx_rebuild`) turned out to be fundamentally unsafe for the **GoXLR** interface specifically — the GoXLR's TX (capture) and RX (playback) share one physical USB device, and reopening only the RX side while TX stays open reliably wedges it (reproduced deterministically with a single mode switch, not just rapid clicking). GoXLR channel-mode changes now go through `main.py`'s `_full_reconnect()` (full stop + new `PipelineController` + start) instead — see `ARCHITECTURE.md` §5. The in-place rebuild described above is still used, and still fine, for the plain **Behringer** interface, which has no such shared-device constraint.

### Prevention
Avoid clicking channel routing buttons in rapid succession. Wait for the brief audio interruption (≈0.5s for Behringer, up to ~1-2s for GoXLR's full reconnect) to complete before clicking again.

---

## Issue: Audio crackling or dropping out

### Possible causes:
1. **Jitter buffer overflow** — Remote packets arriving too fast, buffer full
2. **Jitter buffer underrun** — Packets late, playout buffer runs dry
3. **ALSA buffer underrun** — System can't keep up

### Diagnosis:
Check the web dashboard's jitter metric and packet loss counter. If:
- Jitter is steady (5-30ms) — probably normal
- Jitter spikes >100ms — network issue
- Packet loss counter increasing — packets dropped en route

### Solutions:
1. Increase jitter buffer: `"buffer_ms": 400`
2. Check network quality: `ping -c 50 <remote-ip>`
3. Check CPU usage: `top`
4. Restart service: `sudo systemctl restart briclite`

---

## Issue: RX audio has an echo, sounds "stretched" like a warped cassette tape, or cuts to complete silence intermittently (GoXLR only)

### Root cause (found and fixed 2026-09-09)
`audiomixer` (used to combine codec RX audio with the optional Behringer second-mic branch before writing to the GoXLR) fails its automatic latency negotiation on this pipeline every time (`WARN aggregator: <goxlr_mix> Latency query failed`) and assumes 0 latency. This produced an audible echo, pitch-stretching, and — most confusingly — complete stalls of the mixer's entire output (both Game and Music bus go silent) that reproduced even with the Behringer physically unplugged. The RX `level` meter (upstream of the mixer) kept showing correct signal throughout, and `lost`/`late`/`jitter` in telemetry stayed at exactly 0 — this is not RTP packet loss, so don't chase the network for this specific symptom.

**This is fixed** in `briclite/interfaces/goxlr.py`: the mixer is skipped entirely when there's no second source, and given an explicit latency budget (`ignore-inactive-pads=true min-upstream-latency=200000000 latency=200000000`) when the Behringer is present. If you see this again:
1. Confirm you're running the fixed code: `grep GOXLR_MIX_PROPS /opt/briclite/interfaces/goxlr.py` should show the properties above.
2. Check `journalctl -u briclite | grep "RX (stereo)"` for the actual deployed pipeline string — with the Behringer absent it should go straight from the RX matrix to `alsasink`, no `audiomixer` at all.
3. If it recurs with the fix in place, the latency values (currently 200ms, matching the jitter buffer) may need tuning, or GStreamer's `audiomixer`/`aggregator` implementation may have changed behaviour in a newer version — check `gst-inspect-1.0 audiomixer`'s Element Properties for what's available.

### If you still hear occasional brief (~1 second or less) dropouts after the above
This is a **different** issue, produces zero signal in `lost`/`late`/`jitter` or any log warning — confirmed via a 2.5-minute live watch on 2026-09-09 with dropouts audible throughout and all three counters staying at 0. It was initially attributed to clock drift with a fix planned around RTP-timestamp-derived PTS, but that diagnosis didn't hold up (see `ARCHITECTURE.md` §10/§12.4 for why) — every RX sink runs `sync=false` on a direct `hw:` ALSA device, so the more likely cause is a transient scheduling stall (GIL/CPU contention on the weak embedded hardware) starving that sink, not clock drift. **Mitigation applied and deployed 2026-09-09** (`ARCHITECTURE.md` §12.4): larger RX queue/ALSA buffers, and `SCHED_FIFO` priority for the playout thread — the `CAP_SYS_NICE` grant is live on the PSA300 unit and confirmed active (`journalctl` shows `Playout thread: SCHED_FIFO priority 10`, not the earlier permission-denied warning). **Still not confirmed whether the glitch itself is actually gone** — needs a multi-hour soak listen; if it recurs, see §12.4's follow-up note on adaptive playout correction for genuine oscillator drift.

## Issue: deploying a code change causes an extended outage and/or resets GoXLR PFL

### What happens if you just `systemctl restart briclite.service` and move on
Confirmed live 2026-09-09. The restart kills the whole process — `main.py`'s `lifespan()` shutdown never calls `PipelineController.stop()`, so no `DISCONNECTED` telemetry is ever pushed, and the dashboard's telemetry websocket has no "disconnected" visual state (`ws.onclose` in `index.html` just retries silently), so the web UI can look unchanged even though the codec is fully down. The codec also does **not** auto-reconnect on boot — `/api/connect` is a separate, deliberate call. If you leave more than ~10s between the restart finishing and calling `/api/connect`, two things compound:
1. The remote (Comrex-like device) drops its own session on the TX gap it sees (see `_rx_watchdog()`'s docstring in `main.py`), so your manual reconnect doesn't immediately restore full audio.
2. Our own RX watchdog then notices no RX packets for >10s and does its *own* full auto-reconnect a bit later — a second, fully automatic outage on top of the first.

One real deploy this way produced ~76s of cumulative disruption from a single restart, including two silent GoXLR PFL resets (`Studio return PFL active` logged twice in the journal, ~50s apart — see `ARCHITECTURE.md` §9 "Bleep/PFL reset on reconnect").

### How to deploy without triggering the cascade
Restart and reconnect in one shell round-trip, with no human-turn delay in between — poll until the server is actually up, then reconnect immediately:
```bash
sudo systemctl restart briclite.service
for i in $(seq 1 50); do
  curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/ | grep -q 200 && break
  sleep 0.1
done
curl -s -X POST http://127.0.0.1/api/connect -H "Content-Type: application/json" -d "{}"
```
Done this way, total outage is ~4-5 seconds and the watchdog never fires.

### GoXLR PFL comes back off after a deploy no matter what
Expected, not (yet) fixed — see `ARCHITECTURE.md` §12 roadmap item 5. The 2026-09-09 fix only preserves PFL across in-process reconnects (mode switch, hotplug, watchdog auto-reconnect, manual disconnect/reconnect); a full `systemctl restart` wipes the whole process's memory including `GoXLRInterface._studio_pfl`, and there's nowhere outside the process that remembers PFL was engaged. Just re-press Bleep after a deploy.

## Issue: Behringer (or other USB peripherals) repeatedly disconnect, reset, or throw ALSA "No such device" errors

### Root cause
Mixing USB speed classes (the GoXLR is high-speed/480Mbps, the Behringer is full-speed/12Mbps, keyboard/mouse dongles are typically low-speed/1.5Mbps) behind the **same** external hub can cause the slower devices to repeatedly reset, even though the GoXLR itself stays completely stable. Confirmed on the PSA300: the Behringer's ALSA capture threw `SNDRV_PCM_IOCTL_DELAY failed (-19): No such device` 886 times in a 10-minute window while sharing a hub with the GoXLR.

### Diagnosis
```bash
journalctl -u briclite --since "-10 min" | grep -c "No such device"
lsusb -t          # look for the GoXLR sharing a downstream hub with other devices
sudo dmesg | grep -iE "usb.*reset"
```

### Fix
Plug the GoXLR directly into a host USB port, bypassing any hub. Put the Behringer and any other peripherals on a separate hub/port. See `GOXLR-MINI-LINUX.md` §12 for the full writeup and the PSA300's specific two-port wiring. Verify with `lsusb -t` (GoXLR should be a direct child of the root hub) and confirm 0 "No such device" errors over a minute of `journalctl` monitoring afterward.

## Debug Logging

### Enable verbose GStreamer logging
Edit `/etc/systemd/system/briclite.service`:
```ini
Environment=GST_DEBUG=3
```

Restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart briclite
```

Check logs:
```bash
sudo journalctl -u briclite -f
```

GST_DEBUG levels:
- 0 = none
- 1 = error
- 2 = warning
- 3 = info
- 4 = debug
- 5 = log

### Enable Python logging
Modify `main.py` to set logging level to DEBUG (only for troubleshooting):
```python
logging.basicConfig(level=logging.DEBUG, ...)
```

Then restart and check logs.

### Full packet capture
```bash
sudo tcpdump -i eth0 -w /tmp/capture.pcap 'port 5004'
# Let it run for 10 seconds, Ctrl+C
file /tmp/capture.pcap
# Copy to Mac for analysis with Wireshark
scp codec@<server-ip>:/tmp/capture.pcap ~/Downloads/
```

---

## When All Else Fails

1. **Restart the service:**
   ```bash
   sudo systemctl restart briclite
   sleep 2
   ```

2. **Check system health:**
   ```bash
   df -h              # disk space
   free -h            # memory
   uptime             # load average
   journalctl -b -p err -q  # recent errors
   ```

3. **Review recent changes:**
   ```bash
   cd /opt/briclite
   git log --oneline -5
   git diff HEAD~1
   ```

4. **Revert to last known-good state:**
   ```bash
   git checkout HEAD~1 briclite/main.py  # or whichever file
   sudo systemctl restart briclite
   ```

5. **Contact support with logs:**
   ```bash
   # Collect diagnostics
   journalctl -u briclite -n 100 > /tmp/briclite.log
   sudo tcpdump -i eth0 -n 'port 5004' -c 50 > /tmp/packets.txt
   aplay -l > /tmp/alsa.txt
   ps aux | grep -E 'main|gst' > /tmp/processes.txt
   
   # Copy to Mac
   scp codec@<server-ip>:/tmp/*.{log,txt} ~/Desktop/codec-debug/
   ```

   Attach these files to any issue report.

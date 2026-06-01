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

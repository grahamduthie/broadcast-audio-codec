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
This is a **different, lower-level GoXLR USB playback issue**. Live isolation on 2026-09-09 proved it is not RTP loss, source audio, clock drift, GIL scheduling, the jitter buffer, `audiorate`, `audiomixer`, resampling, or GStreamer: a native 10-channel S32LE/48 kHz `aplay` tone reproduced it while ALSA's buffer remained nearly full and no xrun/kernel USB error was logged. `goxlr-daemon` greatly increases its frequency but is not the sole cause. Do not keep increasing application buffers or implement adaptive RTP correction for this symptom. Follow `CURRENT-STATUS.md` for the current kernel A/B test and exact diagnostic command.

## Issue: boot waits for networking or loses SSH after a kernel/reboot change

On the PSA300 X10SBA-L, both Intel I210 ports incorrectly advertise the same firmware onboard name (`eno1`). udev can log `Failed to rename ... to 'eno1': File exists`, leaving the second NIC with an order-dependent fallback name such as `eth1`. A Netplan file keyed only to `eno1` may therefore configure the unplugged socket while the connected socket remains unmanaged. Symptoms are a long `systemd-networkd-wait-online` job, only an IPv6 link-local address, no default route, and UFW dropping SSH because its rules name the other interface.

The live PSA300 was repaired on 2026-09-09 by matching the connected port's permanent MAC and assigning a collision-proof name:

```yaml
network:
  version: 2
  ethernets:
    codec0:
      match:
        macaddress: 0c:c4:7a:b0:c9:d1
      set-name: codec0
      dhcp4: true
      optional: true
```

UFW must allow TCP 22/80 and UDP 5004 on `codec0`. If recovering from the console, a temporary `ip addr add 172.16.10.213/24 dev <connected-interface>` restores LAN IP, but it does not add a gateway/DNS; apply the MAC-matched Netplan config for the complete fix.

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

## Issue: GoXLR faders/colours look wrong after a reboot or service restart (wrong layout, wrong colours, Music/studio-return inaudible)

### Root cause (found 2026-09-09)
The correct fader layout, routing, and colours (`ARCHITECTURE.md` §9 "GoXLRInterface", `GOXLR-MINI-LINUX.md` §10) are asserted in code by `GoXLRInterface.start()`, **not** loaded from any saved `.goxlr` profile file. `start()` only runs when the pipeline actually connects — `POST /api/connect`, a channel-mode/Behringer-hotplug `_full_reconnect()`, or the RX watchdog's auto-reconnect. It does **not** run just because `briclite.service` (or the host) started — `main.py`'s `lifespan()` only constructs the `PipelineController`, it doesn't start it.

So after any reboot or `systemctl restart`, until something actually connects, the physical GoXLR is left showing whatever the `goxlr-utility` daemon's on-disk profile last had — on the PSA300 this is a profile literally named `Default`, unchanged since 1 Jun, with a different fader mapping (A=Mic, B=Music, C=Chat, D=System) and Music channel volume 0. This looks alarming (wrong colours, wrong fader assignment, the studio-return tone/audio inaudible) but is expected pre-connect state, not a fault, and there is no other "correct" profile file hiding somewhere to load instead.

### Diagnosis
```bash
goxlr-client --status-json | python3 -c "
import json,sys
d=json.load(sys.stdin); m=list(d['mixers'].values())[0]
print({k:v['channel'] for k,v in m['fader_status'].items()})
print(m['levels']['volumes'])
"
# Expect A=Mic, B=Chat, C=Game, D=LineIn once connected; anything else means start() hasn't run yet.
journalctl -u briclite.service | grep -i "api/connect"   # confirm whether a connect has actually happened this boot
```

### Fix
Call `POST /api/connect` (see the deploy round-trip above) — this reasserts the correct layout, routing, and colours from code within a second or two. Do **not** try to fix it by loading a different saved `.goxlr` profile via `goxlr-client profiles device load` or the GoXLR app — none of the profile files on disk match briclite's managed layout, and loading one has no lasting effect since the next `start()` overwrites it anyway.

## Issue: Behringer (or other USB peripherals) repeatedly disconnect, reset, or throw ALSA "No such device" errors

### Root cause
Mixing USB speed classes behind the **same** external hub can cause the slower devices to repeatedly reset. Two distinct instances of this have now been seen on the PSA300:

1. **GoXLR (high-speed/480Mbps) sharing a hub with the Behringer (full-speed/12Mbps).** Confirmed and fixed 2026-09-09 — see "Fix" below. The GoXLR itself always stayed stable; the Behringer's ALSA capture threw `SNDRV_PCM_IOCTL_DELAY failed (-19): No such device` 886 times in a 10-minute window.
2. **Behringer (full-speed/12Mbps) sharing a hub with low-speed/1.5Mbps HID devices** (mouse, keyboard) — the interaction the GoXLR/Behringer fix above didn't touch, since it only moved the GoXLR off that hub, not the Behringer. Confirmed overnight 2026-09-09→10: the kernel logged 5 unattended `usb 1-1.2.1: reset full-speed USB device` / `usb 1-1.2.4: reset low-speed USB device` events (`journalctl -k`), each one leaving `pipeline_manager.py`'s Behringer `alsasrc` (or, if it was mid-write, `goxlr_mix`'s downstream `alsasink`) holding a PCM handle to a device the kernel had already reset out from under it. Before the fix below existed, GStreamer's ALSA element doesn't treat that as fatal to the whole pipeline — it just keeps re-issuing the same failing ioctl (`SNDRV_PCM_IOCTL_DELAY`) from its clock-polling thread forever, because nothing ever tore the pipeline down to reopen the handle. One occurrence flooded the journal with **583,838** lines (~430MB) over ~8 hours before anyone noticed. This class of full/low-speed-behind-one-EHCI-hub reset is a long-standing, still-unresolved Linux kernel USB quirk — see the LKML thread ["USB EHCI: repeated resets on full and low speed devices"](https://lkml.kernel.org/lkml/5393eab7-8203-1696-ffc6-7e06cd63638a@oracle.com/T/) — so it should be expected to recur; it is not something briclite (or this box's 2015-era EHCI controller) can prevent outright.

Critically, **instance 2 is invisible to `_interface_monitor`'s presence poll** (`GoXLRInterface.behringer_available()` in `main.py`, polled every 5s): a USB *reset* leaves the card entry in `/proc/asound/cards` in place the whole time — only a full unplug/replug removes it — so `behringer_available()` never changes and the hotplug-driven `_full_reconnect()` never fires on its own.

### Diagnosis
```bash
journalctl -u briclite --since "-10 min" | grep -c "No such device"
lsusb -t                          # which devices share which hub, and at what speed
sudo dmesg | grep -iE "usb.*reset"
journalctl --disk-usage           # check whether a past flood already ate disk space
```

### Fix — physical (for instance 1, GoXLR/Behringer sharing a hub)
Plug the GoXLR directly into a host USB port, bypassing any hub. Put the Behringer and any other peripherals on a separate hub/port. See `GOXLR-MINI-LINUX.md` §12 for the full writeup and the PSA300's specific two-port wiring. Verify with `lsusb -t` (GoXLR should be a direct child of the root hub) and confirm 0 "No such device" errors over a minute of `journalctl` monitoring afterward. **Already done and confirmed stable — the GoXLR has not been implicated in any reset event since.**

### Fix — physical (for instance 2, Behringer/HID sharing a hub)
**Done 2026-09-10.** The mouse and keyboard were physically removed (not needed for testing) and the Behringer plugged directly into the PSA300's second port, no longer via an external hub. `lsusb -v` on the internal hub chip confirms `bDeviceProtocol: 1 Single TT` — both of the PSA300's rear ports lead into the **same** internal 8-port, single-TT hub regardless of which one is used, so which specific port each device uses makes no topological difference. What matters is that the Behringer is now the *only* full/low-speed device sharing that TT — the multi-device contention (Behringer + mouse + keyboard) this LKML bug class depends on no longer exists. Effect on long-run reset frequency not yet confirmed over an unattended window; see `USB-AUDIO-GLITCH.md` for the full topology writeup, including why an earlier documentation claim about a "USB3 port" giving the GoXLR a direct root-hub path does not hold up.

### Fix — software (automatic recovery + log safety, added 2026-09-10)
Since the underlying USB-level resets can't be eliminated outright (see LKML thread above), `pipeline_manager.py`'s `_on_bus_message` now recognises a GST bus `ERROR` whose text matches `_DEVICE_ERROR_PATTERNS` ("disconnected", "no such device", "input/output error") as a device-loss event rather than an ordinary stream error, and calls back into `main.py`'s `_on_pipeline_fault()` (also triggered by a second, unrelated failure mode — see "PFL stutters and goes silent" below), which waits `_PIPELINE_FAULT_SETTLE_S` (1.5s, to let the USB reset finish) and then runs the same `_full_reconnect()` used for channel-mode switches and Behringer hotplug — closing and reopening every ALSA handle, which stops the spinning thread and clears the flood. Concurrent triggers are coalesced into a single follow-up retry rather than stacking. This closes the gap the presence-poll watchdog missed (see "Root cause" above) — a reset event is now handled within ~2 seconds instead of persisting indefinitely.

As a disk-space backstop independent of that fix, `briclite.service` also now sets `LogRateLimitIntervalSec=30s` / `LogRateLimitBurst=1000` (see `BUILD.md` §7) — systemd's own default (10000/30s) is far too loose to catch a sustained ~20 lines/sec spin like the one observed.

Verify after a reset event: `journalctl -u briclite --since "-2 min" | grep -i "Device-level GST error"` should show at most one or two reconnects, not a runaway flood, and `journalctl --disk-usage` should stay flat.

## Issue: PFL audio stutters and goes silent for seconds to ~90s, then reconnects on its own

### What this actually is
This is an umbrella symptom with **at least four distinct underlying causes** — see `USB-AUDIO-GLITCH.md` for the full investigation, evidence, upstream references, and current status. Do not assume two reports of "PFL glitched" are the same bug; check the log first.

### Diagnosis — do this before assuming anything
```bash
journalctl -u briclite --since "-3 min" | grep -viE 'SNDRV_PCM_IOCTL_DELAY|goxlr.ipc|aggregator'
```
Look for, in order of what you'll actually find:
- `RX socket gap: ...ms ... delta 1 ... local read delay` — the RX-reading thread was briefly starved but no packets were lost; usually resolves in under a second on its own.
- `RX socket idle ...ms, N bytes queued unread` with `N > 0` — same as above, a genuine local stall in progress.
- `RX socket idle ...ms, 0 bytes queued unread — nothing queued` — a **real network gap**; nothing arrived at this host at all. Check whether it follows a TX interruption (hotplug, restart) — the remote drops its session on a TX gap and takes ~30-45s to reconnect. Self-heals via `main.py`'s `_rx_watchdog` (10s threshold).
- `RX sink produced no output for over 2.5s while RX packets keep arriving` — the local playout stall watchdog fired (`pipeline_manager._rx_stall_watchdog`); RX network was fine, something downstream (most likely `audiomixer`/`goxlr_mix`) silently stopped producing output. Self-heals via the same `_full_reconnect()` path.
- **Nothing logged at all**, despite audibly glitching — this is fault signature #4 in `USB-AUDIO-GLITCH.md`, the still-unresolved one. Don't spend time re-diagnosing it the same way; it's already established that it produces zero signal in current instrumentation.

### Fix
All three of the *detected* signatures above already self-heal automatically via `_full_reconnect()` — no manual action needed, typically resolved within 2-15 seconds. The undetected one (#4) has no fix yet; see `USB-AUDIO-GLITCH.md` for what's been ruled out and what to try next.

### Do not do this while diagnosing
Do not run `usbmon` or any full-rate USB packet trace while anyone is actively listening to or relying on the live audio — confirmed 2026-09-10 to itself cause audible audio breakup, likely by adding enough CPU/interrupt load to perturb the same fragile timing this investigation is trying to observe. See `USB-AUDIO-GLITCH.md` → "A real methodological trap: usbmon makes it worse."

## Issue: Behringer fallback interface never actually worked — misconfigured ALSA device

### Root cause
`config.json`'s `audio_network.alsa_device` (used by `BehringerInterface`, the automatic fallback when the GoXLR is briefly unavailable in `audio_interface: "auto"` mode) was set to `hw:0,0` — a numeric ALSA card **index**, not a name. Card indices are fragile and can shift; more importantly, `hw:0,0` normally refers to whatever's enumerated first, which under normal operation **is the GoXLR itself** (`/proc/asound/cards` card 0). The Behringer fallback interface has therefore never actually pointed at the Behringer — the moment the GoXLR genuinely disappears (the only time this fallback path is used), index 0 resolves to nothing, and the fallback crashes into a wall of ALSA "No such file or directory"/protocol errors for several seconds before `_interface_monitor`'s next poll notices the GoXLR is back and switches back. Confirmed live 2026-09-10 — a routine ~3s GoXLR hotplug turned into an ~11s error cascade because of this.

Found in both the live deployment's `config.json` and the repo's `config.example.json` template — this was wrong from initial setup, not a regression.

### Diagnosis
```bash
python3 -c "import json; print(json.load(open('/opt/briclite/config.json'))['audio_network']['alsa_device'])"
# hw:0,0 is wrong. Compare against:
cat /proc/asound/cards   # find the Behringer's actual card name (normally "CODEC")
```

### Fix
Set `alsa_device` to the name-based form, matching the same practice already used for the GoXLR elsewhere in this codebase (`hw:GoXLRMini,0`, never a numeric index):
```json
"alsa_device": "hw:CODEC,0"
```
**Fixed 2026-09-10** in both `/opt/briclite/config.json` (live) and `briclite/config.example.json` (repo template). Deploy via the standard restart+reconnect round-trip (see "deploying a code change causes an extended outage" above).

## Issue: codec silently stays disconnected after the GoXLR reappears (auto-mode interface hot-swap)

### Root cause
`main.py`'s `_interface_monitor()` polls every 5s and, in `audio_interface: "auto"` mode, hot-swaps between `GoXLRInterface` and `BehringerInterface` when GoXLR presence changes. The code that handles this transition constructed a new `PipelineController` but **never called `.start()` on it** — unlike every other reconnect path in the file (`_full_reconnect()`, the old inline `_rx_watchdog()` logic), which all correctly call `.start()`. The pipeline object existed, fully built, sitting in `Gst.State.NULL`, with no bound socket and no running threads — `is_active` stayed `False` forever until someone manually called `/api/connect`. This bug is pre-existing (predates 2026-09-10's other work) but had never been observed before because the GoXLR had been rock-solid throughout the whole investigation up to that point — it only manifests when the GoXLR is genuinely absent long enough for the auto-mode poll to fail over to Behringer and then come back.

### Diagnosis
After any GoXLR hotplug event, check for a `Started (jitter buf ...)` log line following an `Audio interface switched to GoXLR` line. If `switched to GoXLR` appears with no `Started` line after it (and no TX/RX state transitions), the pipeline was rebuilt but never started.
```bash
journalctl -u briclite --since "-5 min" | grep -A5 "Audio interface switched to"
```

### Fix
**Fixed 2026-09-10.** `_interface_monitor`'s hot-swap block now calls `controller.start(loop)` after constructing the new controller, matching every other reconnect path. (This was later further consolidated — see the next entry — to go through `_full_reconnect()` entirely rather than duplicating the stop/construct/start sequence inline.)

## Issue: RX network watchdog silently stops detecting outages under concurrent reconnect triggers

### Root cause
Three separate places in `main.py` rebuild the global `PipelineController`: `_full_reconnect()` (properly serialised behind `_full_reconnect_lock`), and — until 2026-09-10 — `_rx_watchdog()` and `_interface_monitor()`'s hot-swap block, **both of which duplicated the stop/construct/start sequence inline with no locking at all.** Under rapid concurrent triggers (confirmed live: a deliberate USB port swap firing the device-error path, the stall watchdog, and an interface hot-swap within the same few seconds), these unlocked paths raced on the shared `controller` global. The practical effect: `_rx_watchdog()`'s own loop silently stopped detecting further outages — a genuine RX outage then persisted for **100+ seconds with zero automatic recovery**, something that had never happened before in this investigation (every prior outage had self-healed within 2-15s). Required a manual `/api/disconnect` + `/api/connect` to restore.

No crash/traceback was logged — the task didn't die with an exception, it just stopped meaningfully checking (or its state got clobbered by a concurrent reassignment of the same global from another path).

### Diagnosis
If RX has been idle for well over the 10s watchdog threshold with no `No RX packets for Xs — auto-reconnecting` line in the log, the watchdog itself is stuck:
```bash
journalctl -u briclite --since "-5 min" | grep -E "RX socket idle|No RX packets|Pipeline fault|switched to"
```
Idle time climbing steadily with no corresponding watchdog/reconnect line is the signature. Restarting the service (`sudo systemctl restart briclite`, then reconnect) always clears it, since it creates a fresh watchdog task.

### Fix
**Fixed 2026-09-10.** Both `_rx_watchdog()` and `_interface_monitor()`'s hot-swap path now call `await _full_reconnect(...)` instead of duplicating the rebuild logic, so every pipeline rebuild — regardless of which trigger caused it — is serialised behind the same `_full_reconnect_lock`. See `ARCHITECTURE.md` §8 (Threading Model) for the updated invariant.

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

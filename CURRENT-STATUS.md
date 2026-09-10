# Current Investigation Status — 2026-09-09

This is the handoff document for the live residual RX-audio glitch investigation. Read this before resuming tests on the PSA300.

## Update — 2026-09-10 morning: overnight unattended run + Behringer USB-reset log flood, now fixed

Graham left the codec running unattended overnight (2026-09-09 20:24 → 2026-09-10 07:xx) sending music to the remote, as a soak test. `briclite.service`/`goxlr-daemon.service` never crashed or restarted, and the TX/RX link to the remote never dropped or reconnected once — the core broadcast path held up cleanly all night.

However: a Behringer USB reset at 23:23:21 UTC (`usb 1-1.2.1: reset full-speed USB device`, kernel log) left one GStreamer ALSA element in the RX pipeline spinning forever on a dead PCM handle (`SNDRV_PCM_IOCTL_DELAY failed (-19): No such device`, ~20/sec). It was never noticed because `_interface_monitor`'s Behringer-presence poll doesn't detect a reset (the ALSA card stays enumerated throughout, only a full unplug/replug would have tripped it) and nothing else was watching for it. By the time it was checked at ~07:15 it had logged **583,838** lines (~430MB) over ~8 hours, still ongoing. Three more resets happened overnight without incident (01:22 ×2, 04:04, 04:46) — evidently not all resets trigger the stuck-thread condition, just some.

Root cause of the resets themselves: the Behringer (full-speed/12Mbps) now shares its USB hub with the mouse and keyboard (low-speed/1.5Mbps) — a known, long-standing Linux/EHCI kernel quirk (mixing full- and low-speed devices behind one hub/Transaction-Translator on an older EHCI controller), not something briclite caused or can fully prevent. This is a *different* hub-sharing conflict than the GoXLR/Behringer one fixed 2026-09-09 (see `TROUBLESHOOTING.md` "Issue: Behringer ... repeatedly disconnect, reset" for both).

**Fixed and deployed 2026-09-10, ~07:30 UTC:**
- `pipeline_manager.py`/`main.py`: a device-loss GST bus error now triggers an automatic `_full_reconnect()` (~2s), instead of the affected element spinning indefinitely. See `TROUBLESHOOTING.md` for the mechanism.
- `briclite.service`: added `LogRateLimitIntervalSec=30s`/`LogRateLimitBurst=1000` as a disk-space backstop (live unit at `/etc/systemd/system/briclite.service`, and documented in `BUILD.md` §7).
- Journal vacuumed on the PSA300 (439M → 268M) to reclaim the space the flood used; 94G free on `/`, was never actually at risk of filling overnight.
- Deployed via the documented one-shot restart+reconnect round-trip (`TROUBLESHOOTING.md` "deploying a code change causes an extended outage") — confirmed clean restart, immediate reconnect, no cascade.

**Not yet done:** physically moving the Behringer off the hub it shares with the mouse/keyboard (would need hands-on access to the PSA300). The software fix means this no longer matters operationally (a reset now self-heals in ~2s instead of silently degrading for hours), but doing it would reduce how often resets happen at all.

**Not yet validated:** the new auto-reconnect code path hasn't been exercised against a real reset since deploying (none has occurred yet). Trigger one deliberately (e.g. `sudo usbctl` unbind/rebind or unplug/replug the Behringer) and confirm via `journalctl -u briclite -f` that exactly one `"Device-level GST error"` + reconnect appears, not a flood, before considering this closed.

## Bottom line

The residual glitch is **not in the received AAC audio, RTP/jitter buffer, Python playout loop, resampling, `audiorate`, or GStreamer generally when running the isolated raw-ALSA test**. It reproduces with a generated tone played directly through ALSA using `aplay`, while ALSA's playback buffer remains full and the kernel logs no xrun or USB error. The remaining fault domain is the GoXLR Mini's Linux USB-audio/implicit-feedback path, USB controller/device firmware, or the physical device/output path.

`goxlr-daemon` strongly increases the glitch rate but is not the sole cause (established on kernel 6.8.0-124 — see the timed A/B section below).

**New as of the evening of 2026-09-09 — read this before anything else:** running through the real `briclite`/GStreamer pipeline with `audiomixer` active (Behringer connected) produced the **same character of glitch at roughly 10–50x the rate** seen in the isolated raw-`aplay` test — every few seconds instead of every few minutes. This is the single most important open thread — see "Live pipeline glitch-rate spike" below before resuming.

The PSA300 is now running kernel **6.8.0-139-generic** (confirmed a normal boot, not kexec). The kernel-vs-6.8.0-124 daemon-on A/B is done (see "Kernel 6.8.0-139 results" below) — inconclusive/not-fixed, not worth repeating before the glitch-rate-spike thread is chased down.

## Live state at handoff (as of 22:37 UTC, 2026-09-09 evening)

- PSA300: `ssh marlowfm@172.16.10.213` (passwordless SSH and sudo). **Not a live/on-air session right now** — this is Graham's own isolated test environment, not the actual broadcast; a `172.16.10.212` (Broadcast Laptop) source in the logs below is Graham himself using the web UI from there, not a separate operator.
- Kernel: `6.8.0-139-generic`, normal boot (boot ID `e9a48c4f67cf4248883f9b8fbbbdd5d1`).
- Network: connected NIC is permanently named `codec0` by MAC `0c:c4:7a:b0:c9:d1`; DHCP supplies `172.16.10.213/24`, gateway/DNS `172.16.10.1`.
- `briclite.service`: active **and connected** (`TX→217.36.229.106:5004 RX←:5004`, reconnected 21:52:54 UTC).
- `goxlr-daemon.service`: active.
- GoXLR Mini: ALSA card 0 after this boot; always address it by name (`hw:GoXLRMini,0`).
- **Behringer is physically connected** (ALSA card 2, `CODEC`, USB `1-1.2.1`, full-speed) — plugged in mid-session to test its effect on the glitch. USB topology is still correctly isolated (GoXLR alone on the first hub port; Behringer + HID share the second hub) — verified via `lsusb -t`.
- GoXLR config is correct (faders A=Mic, B=Chat, C=Game, D=LineIn — see "GoXLR config source of truth" below) since the pipeline is connected.
- PFL: **off** as of the last toggle (22:34:21 UTC). Music volume 0, not routed to headphones. Press Bleep to re-engage before listening.
- Local code changes and their deployed `/opt/briclite` copies are **uncommitted**; do not discard or commit without reviewing them.

## GoXLR config source of truth (discovered 2026-09-09, evening)

After the reboot above, the operator found the GoXLR's fader layout and colours "not what I expect" and the test tone (written directly to ALSA channels 6/7) inaudible. Investigation found: the GoXLR's fader/routing/colour config is **not** stored in any `.goxlr` profile file — it's asserted in code by `GoXLRInterface.start()` and only reapplied when the pipeline actually connects (`/api/connect`, a hotplug/mode-switch `_full_reconnect()`, or the RX watchdog). It does **not** run merely because `briclite.service` or the host started. Since nobody had called `/api/connect` since the 20:23:58 restart, the GoXLR was still showing the `goxlr-utility` daemon's raw on-disk profile (literally named `Default`, unchanged since 1 Jun, with a different fader mapping and Music volume 0) — not a fault, just pre-connect state. Calling `POST /api/connect` fixed it immediately (verified via `goxlr-client --status-json`: A=Mic, B=Chat, C=Game, D=LineIn). Full writeup in `ARCHITECTURE.md` §9 "GoXLRInterface", `GOXLR-MINI-LINUX.md` §10, and `TROUBLESHOOTING.md` → "Issue: GoXLR faders/colours look wrong after a reboot or service restart". **Do not chase this by loading a different saved profile** — none on disk match briclite's managed layout, and the next connect overwrites whatever's loaded anyway.

Practical effect on this investigation: the direct-ALSA tone test writes to the same raw channels 6/7 regardless of GoXLR profile state, so the kernel/glitch test itself is unaffected either way — but a listener needs Music actually routed to headphones to hear it, which normally only happens via Bleep/PFL once the correct config is loaded (i.e., after a connect).

## Live pipeline glitch-rate spike (2026-09-09 evening) — the open thread to chase next

While connected via the **real** `briclite`/GStreamer pipeline (not the raw-`aplay` isolation test) with Behringer connected — so `rx_sink_bin()` inserts the `audiomixer` (`goxlr_mix`) branch — and listening to real program audio via PFL, Graham reported the glitch happening **every few seconds**, versus every few minutes in the isolated raw-`aplay` tests on the same kernel/daemon state. Same audible character as the tone-test glitches, just far more frequent. This is a much bigger effect than anything else recorded in this investigation (bigger than the daemon-on/off difference) and is not yet explained.

Two variables changed at once relative to the clean tone tests, and haven't been separated yet:
1. Going through GStreamer/briclite's real playback path (jitter buffer, `_playout_loop`, real network-timed AAC) instead of a raw synthetic tone piped straight into `aplay`.
2. `audiomixer` being in the RX pipeline at all, because Behringer is connected.

**Next test to run:** stay connected through the real pipeline (don't go back to raw `aplay`), unplug the Behringer so the pipeline drops back to the simple single-branch RX path (no `audiomixer` — see `rx_sink_bin()`'s `behringer_available()` check), and listen via PFL again.
- If the glitch rate drops back down toward the tone-test baseline (every few minutes), `audiomixer`/Behringer's presence is the aggravator — worth its own isolated investigation (possibly related to the `Latency query failed` warning it logs on every connect, or its own periodic scheduling adding USB-adjacent CPU/timing pressure, analogous to how `goxlr-daemon` control traffic aggravates the base fault).
- If it stays this frequent with Behringer unplugged, it's specifically "real GStreamer pipeline + real network audio" vs. "raw synthetic tone" that matters — meaning the isolated `aplay` tests may have been *understating* the true glitch rate the whole time, and every prior daemon/kernel comparison run through `aplay` should be treated as a lower bound, not the real-world rate.

Also worth doing once this is sorted: repeat with Behringer connected but **not** through PFL — i.e. confirm whether Game (which does feed the GoXLR's real hardware Broadcast Mix, unlike Music) is also glitching at this elevated rate, since that would mean the fault is currently reaching what gets sent to the remote, not just local monitoring. Nothing about this was verified either way this session — architecturally, this class of glitch is expected to not affect the TX/broadcast-mix path at all under normal operation (TX reads via the GoXLR's *capture* endpoint 0x88, upstream of the playback-side implicit-feedback bug — see `GOXLR-MINI-LINUX.md` §13), but this specific rate-spike scenario with `audiomixer` active hasn't itself been checked against that assumption.

### Two other loose threads noticed in passing this session, not yet investigated
- **A real GoXLR USB disconnect/re-enumeration happened spontaneously at 21:26:15–21:26:35 UTC**, unconnected to anything either of us was doing at the time (mid-way through unrelated profile-file investigation). `goxlr-daemon` logged `Error Received from S210310593DI7: Input/Output Error` → `Device Disconnected`, then re-enumerated as a new USB device number and re-initialised. This is a more severe class of fault than the audible glitches (a full device drop, not just a playback stutter) and has no established cause yet.
- **Two AAC decoder errors** (`libav ERROR: env_facs_q 254 is invalid` at 22:01:17, and `env_facs_q 255 is invalid` at 22:09:09) — a real corrupt-frame error in `avdec_aac`, decoding the incoming network AAC stream. This is a different subsystem entirely from the `audiomixer`/USB-playback fault (it's upstream, on the RTP/AAC receive side) and hasn't been correlated with anything else yet — could be ordinary real-world packet loss/reordering against the real remote, or could be something worth its own look if it recurs frequently.

## Kernel 6.8.0-139 results (2026-09-09 evening) — daemon-on A/B vs. the 6.8.0-124 baseline

Same raw-`aplay` isolation test as the "Timed daemon A/B" section below, same daemon-on state, run twice on the new kernel:

- **Without Behringer** (21:34:00–21:39:00 UTC): 3 glitches clustered at tone start (21:34:00–10), 2 more around the 2-minute mark (21:36:00–15), then clean for the final ~3 minutes. 5 glitches total in 5 minutes.
- **With Behringer connected** (21:41:42–21:46:43 UTC): 4 glitches spread across the whole run (~1:20, ~2:00, ~4:20, ~4:45 in) rather than clustered. 4 glitches total in 5 minutes.

Both runs: zero kernel/dmesg messages, zero ALSA XRUNs, buffer stayed 91–100% full throughout (same silent-fault signature as every prior test on 6.8.0-124). **Conclusion: the kernel update does not fix the fault** — glitch rate/character is comparable to the old-kernel daemon-on baseline in both configurations. Neither run reached the 10–15 clean minutes that would have called it a fix. Not worth re-testing further — the live-pipeline glitch-rate spike above is a much bigger and more promising lead.

## Next test

Do the Behringer-unplugged-while-connected-via-real-pipeline comparison described in "Live pipeline glitch-rate spike" above — that is the highest-value next step, well ahead of any further raw-`aplay` kernel/daemon A/B testing. The raw-`aplay` test script (still useful for isolating the base USB fault from GStreamer/network effects) is:

```bash
python3 -c 'import math,struct,sys
amp=int((2**31-1)*0.05)
out=sys.stdout.buffer
block=bytearray()
for n in range(48000):
 v=int(amp*math.sin(2*math.pi*440*n/48000))
 row=[0]*10; row[6]=v; row[7]=v
 block.extend(struct.pack("<10i",*row))
for _ in range(300): out.write(block); out.flush()' \
| aplay -q -D hw:GoXLRMini,0 -t raw -f S32_LE -r 48000 -c 10 \
    --buffer-time=400000 --period-time=20000
```

Only use it standalone (Briclite stopped/disconnected, `/dev/snd/pcmC0D0p` free) when specifically isolating the base USB fault from GStreamer/pipeline effects — the current open thread needs the *real* pipeline running, not this script.

When testing is finished, restore normal operation with `goxlr-daemon` and Briclite running, call `POST /api/connect` in one round-trip (see `TROUBLESHOOTING.md`), and re-engage PFL by pressing Bleep.

## Evidence collected

- Network capture: 2,400 consecutive frames; sequence increments, RTP timestamps exactly `+3840`, all ADTS headers/frame lengths valid, no gaps/loss.
- Incoming AAC captured around a reported glitch decoded cleanly. The operator carefully heard no glitch in [diagnostics/rx-source-around-20-36-11.wav](diagnostics/rx-source-around-20-36-11.wav).
- Glitches occurred at ordinary levels around -15 dBFS, ruling out a level/clipping threshold.
- Behringer was unplugged and the conditional `audiomixer` disappeared; glitches persisted.
- `audiorate` was removed; glitches persisted.
- RX/GoXLR conversion was simplified to direct 24 kHz → 48 kHz with redundant 44.1/48 kHz conversion removed; glitches persisted.
- A generated GStreamer tone through only `audiotestsrc → 10-channel S32LE 48 kHz → alsasink` glitched.
- A native `aplay` tone with no GStreamer or Codec code glitched at 20:56:06. `/proc/asound/.../status` still showed approximately 18,600–19,100 of 19,200 frames queued and no XRUN.
- GoXLR USB stream: 10-channel S32LE/24-bit playback at 48 kHz, asynchronous OUT endpoint `0x08`, implicit feedback from the 21-channel capture endpoint `0x88`. Autosuspend is disabled (`power/control=on`).
- Kernel and daemon logs contained no corresponding error.
- Upstream reports match this symptom: [Linux kernel bug 211211](https://bugzilla.kernel.org/show_bug.cgi?id=211211) and [alsa-lib issue 113](https://github.com/alsa-project/alsa-lib/issues/113) document GoXLR output stuttering with direct ALSA while capture remains clean and identify implicit-feedback handling.

## Timed daemon A/B results on kernel 6.8.0-124

- Native tone, daemon enabled: glitch at **20:56:06**.
- Daemon stopped at **20:56:56**; glitch at **21:00:04**, then none through tone end **21:04:12**.
- Cleanly routed repeat, daemon enabled from tone start **21:07:34**: double glitch seconds after start, glitch **21:08:08**, then another untimed glitch.
- Daemon stopped at **21:08:42**: no further glitches through tone end **21:12:40**.

All times are Europe/London/BST; the PSA300 journal uses UTC (one hour earlier).

## Code changes currently uncommitted and deployed

- `briclite/core/pipeline_manager.py`: removed `audiorate`; RX caps use `interface.rx_sample_rate()`.
- `briclite/interfaces/base.py`: added `rx_sample_rate()`, default 44.1 kHz.
- `briclite/interfaces/goxlr.py`: returns 48 kHz and removes its redundant second resampler/caps stage.
- Earlier deployed hardening remains: RX queue 10 → 50, requested sink buffer 400 ms/period 20 ms, and playout `SCHED_FIFO` priority 10 with the systemd capability grant. These did **not** eliminate the audible issue; GStreamer actually negotiated only about 9,600 frames/200 ms on this device.

## Boot and network repair performed today

- Firmware is 2015 AMI on Supermicro X10SBA-L. EFI `BootOrder` is wrong (`Hard Drive`, Network, then Ubuntu), and Ubuntu EFI entries are inactive/malformed. The firmware rejects Linux EFI-variable writes with `Input/output error`, despite NVRAM not being full.
- Added the missing fallback files `/boot/efi/EFI/BOOT/grubx64.efi` and `grub.cfg`, copied and hash-verified from `/boot/efi/EFI/Ubuntu/`. No existing EFI file was overwritten. This improves fallback booting, but a normal unattended firmware reboot is **not yet validated**.
- Installed `kexec-tools`; kexec successfully booted `6.8.0-139`. It took several minutes because Netplan was waiting on the wrong NIC, not because the kernel failed.
- Both Intel I210 NICs advertise the same firmware name `eno1`; udev logged a rename collision. Netplan previously configured unplugged `eno1` while the cable was on the other port. `/etc/netplan/50-cloud-init.yaml` now matches connected MAC `0c:c4:7a:b0:c9:d1`, renames it `codec0`, enables DHCP, and marks it optional. Backups exist beside it as `50-cloud-init.yaml.before-eth1-fix` and `.before-codec0`.
- UFW now allows TCP 22/80 and UDP 5004 on `codec0`. Older inactive-interface rules remain as fallbacks.

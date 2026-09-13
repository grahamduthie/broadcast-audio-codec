# Current Investigation Status — updated 2026-09-13

This is the handoff document for the live residual RX-audio glitch investigation, started 2026-09-09. Read this before resuming tests on the PSA300 — start with the most recent dated section below and work backward; older sections are historical record. For the full technical deep-dive specifically on the USB/audio glitch (not the overnight-flood or bug-fixing threads), see `USB-AUDIO-GLITCH.md`.

## Update — 2026-09-13: Fader B moved from Chat to LineIn (Behringer now analogue)

Follow-on from the Fader D change directly below, same session. The Behringer's guest mic used to reach Fader B via USB capture (`hw:CODEC,0`) mixed into the GoXLR's Chat bus (`extra_rx_source_bins()`/the conditional `audiomixer` — see `GOXLR-MINI-LINUX.md` §5 and [[project-audio-glitch-investigation]]), which was adding noticeable latency and was a suspected source of some of the glitching. With Fader D freeing the physical Line In jack, the Behringer's analogue output now goes there instead, and Fader B was reassigned from `Chat` to `LineIn` in `_FADER_TO_SOURCE`/`_FADERS`/`_ROUTING`. `Chat` is left inert in `_ROUTING` (not removed) — if the Behringer's USB stays plugged into the host, `behringer_available()` still detects it and the existing hotplug capture/mixer keeps running into that bus, it just no longer reaches any output.

**Decision (2026-09-13, confirmed by Graham): keep the Behringer USB capture path in the code, unused, for possible future re-use.** Do not remove `extra_rx_source_bins()`, the conditional `audiomixer` in `rx_sink_bin()`, `behringer_available()`, `_BEHRINGER_DEVICE`, or `_BEHRINGER_MATRIX` from `briclite/interfaces/goxlr.py` — this was a deliberate choice, not an oversight. If the Behringer's USB cable is unplugged from the host (which is not required), the existing hotplug logic already skips that pipeline branch automatically; if it stays plugged in, it keeps capturing into the now-inert `Chat` bus harmlessly. A future session picking this up should reconnect the Behringer's USB and, if reviving Fader B on Chat is ever wanted again, just swap `_FADER_TO_SOURCE`/`_FADERS`/`_ROUTING`/`_mic_volumes` back the way this session's diff shows (`git show dd84a12` in this repo, `38f12bd` on the PSA300's local repo) — the capture pipeline itself needs no changes.

Two things elsewhere had to change to keep working correctly with Fader B on a new channel name, both in `briclite/interfaces/goxlr.py` and `briclite/main.py`:
- **Studio Monitor Cut** (Cough button) tracks the guest-mic fader by channel name to decide when to cut Line Out for feedback safety — `_mic_volumes` was keyed on `("Mic", "Chat")` and is now `("Mic", "LineIn")`. Left unfixed, this would have silently stopped protecting Fader B.
- **Fader-volume restore-across-restart** whitelist in `main.py`'s `_persist_goxlr_fader_volume()` was `{"Mic", "Chat", "Game", "LineIn"}` and is now `{"Mic", "LineIn", "Game", "Console"}` — this also fixes a latent bug from the Fader D change below, which had left `Console` out of the whitelist, so Fader D's volume was silently not being persisted across restarts since that change went live.

Deployed via the standard restart+reconnect round-trip. Confirmed live via `goxlr-client --status-json`: Fader B's `channel` reads `LineIn`.

## Update — 2026-09-13: Fader D moved from LineIn to Console (optical)

Tested live and made permanent same day. The music player now feeds Fader D digitally over the GoXLR's optical input (`Console` in the IPC/routing) instead of the analogue 3.5mm line input, freeing the Line In jack for another purpose. `_FADER_TO_SOURCE["D"]` and the corresponding `_FADERS` entry in `briclite/interfaces/goxlr.py` changed from `LineIn` to `Console`; the `_ROUTING` table swapped which of the two gets Headphones/BroadcastMix/LineOut — `LineIn` is now left inert (no fader, routed nowhere) rather than removed, so nothing accidentally plugged into the 3.5mm jack can bleed into the mix uncontrolled.

Deployed via the standard restart+reconnect round-trip; no cascading outage. Confirmed live via `goxlr-client --status-json`: Fader D's `channel` reads `Console`. See `GOXLR-MINI-LINUX.md` §10 and `ARCHITECTURE.md` §9 for the updated fader tables.

## Update — 2026-09-13: GoXLR lighting fixes; new Studio Monitor Cut safety feature

Unrelated to the glitch investigation, but live on the PSA300 and pushed to `main` (GitHub) as of today, across four small deploys in one session. Commits, this repo → PSA300 local repo: `3f21d62`→`56286e1` (gradient colour order), `383b276`→`88e3a96` (gradient brightness), `f641ea0`→`a3fc833` (Studio Monitor Cut), `82fd0d5` on both (button brightness). All four were deployed with the standard restart+auto-reconnect round-trip; none caused a cascading outage.

- **Fader LED gradient direction and brightness fixed.** The A–D fader strips had the gradient backwards (red at the bottom, blue at the top) and, after an initial fix, an uneven-brightness side effect (one end was `000000`/black, which is inherently dim). Both are now fixed: `SetFaderColours` uses a real red (`FF0000`) as the fixed top-of-strip anchor instead of black, so the strip reads blue(low)/red(high) with both ends equally bright. See `briclite/interfaces/goxlr.py`'s `_set_fader_colour()`.
- **New: Studio Monitor Cut**, a feedback-safety feature. The Cough button is repurposed as an arm/disarm toggle (Cough previously had a real native hold-to-mute function, now retargeted to an unused bus via `SetCoughMuteFunction: "ToStream2"` so a quick press doesn't blip the mic — a genuine physical *hold* still forces a real native mute regardless, that's GoXLR firmware behaviour outside our control). While armed, Line Out is muted via `SetRouter` whenever fader A (Mic) or B (Chat) reads open (above a small near-zero threshold, `_MIC_OPEN_THRESHOLD = 5` — a fader at rest was observed reading 1/255, not a clean 0, which caused an immediate false cut on arming before the threshold was added), and restored the instant both close. Headphones are never touched. Armed/disarmed state persists across restarts (`monitor_cut_enabled` in the desired-link record), exactly like `studio_pfl`. `_apply_monitor_routing()` was unified to compute one final desired routing state from both PFL and Studio Monitor Cut together, so they compose correctly if both are ever active at once (cutting always wins on Line Out; Headphones always follow PFL alone). Full design/rationale in `ARCHITECTURE.md` and `GOXLR-MINI-LINUX.md` (search "Studio Monitor Cut").
- **Bleep/Cough LED brightness fixed.** Both looked very dim regardless of colour. Root cause: their native mute state is always "Unmuted" under this repurposing (we never actually mute them), and the default `SetButtonOffStyle` for a button in that state is `"Dimmed"` — it dims whatever colour is sent. Fix: `SetButtonOffStyle: [button, "Colour2"]` for both, plus sending the same colour in both `SetButtonColours` slots (previously colour_two was hardcoded `"000000"`, compounding the dimness). Confirmed valid enum values empirically against the live daemon: `Dimmed`, `Colour2`, `DimmedColour2`.
- **Current physical LED meanings**, all via `SetButtonColours`/`SetFaderColours` in `goxlr.py`: Bleep — cyan (`00FFFF`) = PFL off, orange (`FF8800`) = PFL on. Cough — cyan = Studio Monitor Cut disarmed, green (`00FF00`) = armed and Line Out live, red (`FF0000`) = armed and actively cutting Line Out. Faders A–D — blue-ish (low, `00FFFF` normal / `FFB000` amber pending-pickup) fading to red (`FF0000`, fixed top anchor).

**Live state at handoff:** Studio Monitor Cut is currently **armed** on the PSA300 (`monitor_cut_enabled: true` in `/var/lib/briclite/desired-link.json`) — left that way from this session's live testing, not something that needs undoing, but be aware Line Out will cut the instant Mic or Chat opens. Verify with `goxlr-client --status-json` (`cough_button`, `lighting.buttons.Cough`, `router.Microphone.LineOut`) before assuming Line Out is live.

All four changes are committed on both the PSA300 (`/opt/briclite` local repo) and this repo, and pushed to GitHub `main`.

## Update — 2026-09-11: GoXLR Utility web UI enabled for the headless PSA300

The GoXLR is attached to the headless PSA300, not to the Lenovo operator
laptop.  The GoXLR Utility is a web UI served by `goxlr-daemon` on TCP 14564;
the Lenovo at `172.16.10.212` can now use:

```
http://172.16.10.213:14564/
```

The durable PSA configuration is the systemd drop-in
`/etc/systemd/system/goxlr-daemon.service.d/network-ui.conf`, which replaces
the service command with:

```
/usr/bin/goxlr-daemon --log-level info --disable-tray true --http-bind-address 0.0.0.0
```

UFW deliberately limits external access to `14564/tcp` on `codec0` from only
`172.16.10.212` (rule comment: `Lenovo GoXLR Utility UI`).  Do not open this
unauthenticated mixer-control interface to the wider LAN or Internet.

### Critical localhost requirement

Do **not** replace the `0.0.0.0` bind above with `172.16.10.213` alone.  That
looks safer, but it removes the daemon's localhost listener.  Briclite's
`monitor_pfl()` connects to `ws://localhost:14564/api/websocket` for PFL,
physical fader, and fader-mute events.  When the daemon was briefly bound only
to `172.16.10.213`, the subscriber logged repeated `Connection refused`; it
could not mirror Fader C to the clean-news branch or clear amber pickup LEDs
after physical fader movement.

With the all-interface bind plus the source-restricted firewall rule, both
remote Lenovo access and local Briclite WebSocket access work.  Verified after
the final restart: the daemon listens on `0.0.0.0:14564`, `curl
http://127.0.0.1:14564/` returns HTTP 200, `goxlr.pfl` logs `PFL monitor
connected`, and both `goxlr-daemon.service` and `briclite.service` are active.
Restarting `goxlr-daemon` also restarts Briclite through its `PartOf=`
relationship; the persisted requested link restored automatically.  Schedule
future daemon changes accordingly.

### Amber LEDs after the web-UI change

Amber is Briclite's recovery/pickup indication, not a hardware fault: it means
the fader's logical level was restored but Briclite has not observed a physical
post-recovery movement.  The address-only bind prevented the WebSocket event
from arriving, which is why moving a fader did not return it to cyan.  The
WebSocket monitor is connected again; the next real movement of each fader
will clear that fader's amber colour to cyan.  Do not clear all LEDs manually,
as the individual amber state is useful confirmation of soft-pickup status.

## Update — 2026-09-11: clean news return mix deployed and timing-validated

The PSA300 is running the clean-news workaround.  Local commit `1761eba`
(`Route clean news return around GoXLR playback`) is deployed in the PSA
working tree as part of PSA commit `2a0a895`.  Its live configuration is
`goxlr.clean_news_return.enabled=true`,
`alignment_delay_ms=200`, and `timing_probe=false`.

The intentionally asymmetric signal path is:

```
RX Right/news decode ──> GoXLR Game ──> headphones + Line Out (may glitch)
                    └─> interaudiosink/src ──> TX audiomixer ──> RTP return
GoXLR Broadcast Mix, with Game excluded ────────────────────────┘
```

Therefore Fader C/Game remains the local news monitor and can still suffer
the known GoXLR USB-playback dropout, but the return encoder receives the
pre-playback PSA copy.  Game is deliberately excluded from the hardware
Broadcast Mix.  Fader C volume and mute WebSocket events are mirrored onto
the PSA-side clean branch; Fader D/Line In and the other broadcast sources
remain in the hardware Broadcast Mix.

### Evidence and alignment

- Before the workaround, a 51.75-second outgoing RTP capture contained exact
  zero gaps of approximately 100--123 ms (for example at 9.14, 14.81, 22.59,
  and 29.22 seconds), matching the audible GoXLR glitches.
- The final `interaudiosink`/`interaudiosrc` implementation produced a
  34.86-second outgoing capture with no exact-zero run of 20 ms or longer.
- With intermittent 801 Hz news tone on C and local music on D, a timing-probe
  capture correlated the known GoXLR headphone pair (USB capture channels
  10/11) with the outgoing RTP return.  Ten-second matched windows measured
  return-versus-headphone offsets from -1 ms to +4 ms: for practical purposes
  the 200 ms setting is aligned.

`timing_probe` is a temporary diagnostic mode, not normal operation.  When
enabled it captures channels 10/11 to `/tmp/briclite-headphone-timing.raw`
while using one 21-channel GoXLR capture handle for both the base mix and the
probe.  Leave it disabled unless repeating a controlled alignment test.

Do not reinstate either abandoned design: the direct `appsrc` into the TX
`audiomixer` eventually produced recurring roughly 400 ms silences, and
`snd-aloop` alternatives produced underflow/periodic silent sections.  An
`adder` variant also failed GStreamer negotiation on the PSA.  The timestamped
inter-audio handoff is the working design.

For an emergency rollback, set `clean_news_return.enabled` to `false` and
restart the active codec pipeline/service; this restores Game to the hardware
Broadcast Mix and returns to the historical all-GoXLR TX path.

### 11:00 news recording

The requested outgoing-return recording is available locally at
`recordings/news-return-2026-09-11-105907-110300.wav`, with its source capture
at `recordings/news-return-2026-09-11-105907-110317.pcap`.  It contains actual
PSA-originated RTP (172.16.10.213 to UDP/5004), then AAC-decoded audio.  The
capture began at 10:59:07 BST (seven seconds after the requested 10:59 start)
and the WAV was trimmed to exactly 11:03; 11,712 packets were captured with
zero kernel drops.  These recordings are handoff artefacts and intentionally
remain untracked rather than repository content.

## Update — 2026-09-11: Broadcast-monitor laptop screen blanking fixed at the Xfce layer

This is separate from the PSA300 audio investigation, but is important for
operators and future agents because the Briclite web GUI is monitored on the
Lenovo laptop `broadcast-T400` (`ssh broadcast@172.16.10.212`).

The display blanking was not GNOME/Cinnamon, hardware, a lost video signal,
or system suspend. It was the X11 screen saver, controlled by
`xfce4-power-manager`. The X server's default was a 600-second timeout. An
initial `xset`/autostart-only fix was incomplete: the power manager's
`blank-on-ac` and `blank-on-battery` properties were unset, so it retained its
built-in ten-minute default and later reapplied it to the live X server.

The durable settings now saved for user `broadcast` are:

- `/xfce4-power-manager/blank-on-ac = 0`
- `/xfce4-power-manager/blank-on-battery = 0`
- `/xfce4-power-manager/dpms-enabled = false`

They persist in
`/home/broadcast/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml`.
The additional login-time X11 guard remains
`/home/broadcast/.config/autostart/disable-screensaver.desktop`.

At the final live check (09:41 BST), `xset q` showed screen-saver `timeout: 0`
and `DPMS is Disabled`. For the detailed evidence, exact recovery commands,
and the reboot/startup-race behaviour, see `TROUBLESHOOTING.md` under
**Broadcast monitor laptop**. If blanking returns, inspect the live X11 state
before changing anything, then check these three Xfce settings first.

## Update — 2026-09-11: durable GoXLR fader recovery and pickup indication

Following the unattended-update recovery work, Graham reported a real desk
usability problem: after a reset, the daemon could retain an open logical
fader value while the non-motorised physical slider was closed (or had been
moved during the outage). The GoXLR then requires the slider to cross the
remembered value before it takes effect. This is its normal soft-pickup
protection and must be retained: forcing physical and virtual state to agree
would risk an abrupt, on-air level jump.

### Protocol result

The goxlr-utility WebSocket at `ws://localhost:14564/api/websocket` sends no
initial fader-position snapshot when a client connects. It publishes only
JSON Patch changes after movement, e.g.
`/mixers/<serial>/levels/volumes/LineIn = 222`. A live D-fader test produced
a continuous sequence of such absolute logical levels. Therefore Briclite
cannot know a slider's physical position, or prove it moved during an outage,
until a post-recovery fader event arrives. Do not try to replace the daemon's
soft-pickup mechanism without a new lower-level device-protocol capability.

### Implemented behaviour

Commit `3168824` (after restart-continuity commit `8a828e8`) is deployed on
the PSA300 and pushed to `main`.

- `/var/lib/briclite/desired-link.json` now stores `goxlr_fader_volumes` for
  `Mic`, `Chat`, `Game`, and `LineIn`, as well as Headphones and Line Out.
  The WebSocket persists physical fader/monitor changes while a desired active
  link exists.
- A fresh `GoXLRInterface` applies those logical fader values on recovery.
  An open channel thus stays open; no fader is muted or closed while awaiting
  a physical movement.
- Restored A–D fader LED gradients are steady amber (`FFB000`), meaning
  "logical level restored; physical position unverified". The first fader
  volume patch more than four seconds after restoration changes just that
  fader back to normal cyan. The four-second guard ignores echoes of
  Briclite's own `SetVolume` restoration commands.
- The indication is advisory only. It does not change routing, PFL, mute, or
  audio gain. Do not replace it with flashing: repeated lighting commands are
  undesirable on this USB-audio-sensitive device and amber is unambiguous at
  the desk.

### Deployment verification

Immediately before deployment, the active desired-link record was seeded
from the live GoXLR daemon: `Mic=1`, `Chat=0`, `Game=0`, `LineIn=233`,
Headphones `178`, Line Out `230` (stored UI equivalent `rx_volume=90`). A
controlled `briclite.service` restart restored the codec link to
`217.36.229.106:5004`, the exact values above, and all four fader colours
reported as amber. Both `briclite.service` and `goxlr-daemon.service` were
active. The fader-event handling path was also unit-checked locally with a
simulated `LineIn` patch; it persisted the value and changed D from amber to
cyan.

Operationally, amber after a recovery is expected: move a fader through its
displayed/restored level to make its physical control active and return its
LED to cyan. An untouched amber fader continues to pass audio at its restored
logical level.

## Update — 2026-09-11: restart continuity and unattended-update guard

The previous morning's `apt-daily-upgrade` run restarted Briclite and the
GoXLR daemon while a codec link had been in use. Neither service crashed and
the host did not reboot, but the link remained inactive because the requested
connection and PFL state existed only in the old Python process.

The deployed design now persists active operator intent in
`/var/lib/briclite/desired-link.json`. Connecting writes the target and audio
settings before the pipeline starts; disconnecting removes it. A new Briclite
process restores the pipeline, GoXLR setup and PFL state only when that record
exists. The service is also ordered after and coupled to `goxlr-daemon` so a
daemon restart triggers Briclite recovery.

`apt-daily-upgrade.service` is conditionally skipped whenever the durable
active-link record exists. Daily package-list downloads still occur, while
package installation is deferred until the codec is deliberately disconnected
for maintenance. See `BUILD.md` §7.1 and `TROUBLESHOOTING.md` for operations.

The GoXLR recovery record additionally preserves the four broadcast fader
levels. On recovery they are restored with the device's native soft-pickup
behaviour; amber fader LEDs mark levels whose physical slider position has not
yet been seen after the outage. They return to cyan when that fader moves.

## Update — 2026-09-10 afternoon/evening: objective glitch capture, Behringer HID experiment, monitor-output controls

### The residual GoXLR glitch can now be measured without listening

A five-minute full-duplex hardware-loopback test now gives objective evidence of the short playback fault. Briclite was disconnected, a deterministic 997 Hz tone was written directly to the GoXLR's 10-channel playback PCM on Game (channels 2/3), Game was routed into BroadcastMix, and the GoXLR's 21-channel capture PCM was recorded simultaneously. The unrelated capture channels continued normally while the returned Game signal contained exact-zero runs, proving the loss occurs in the GoXLR playback/Game path before BroadcastMix rather than in the recording process.

Results:

- Patched goxlr-daemon with **50 ms status polling**: 5 gaps in 5 minutes, **106.7–137.3 ms**.
- goxlr-daemon **stopped entirely**: 4 gaps in 5 minutes, **118.9–132.1 ms**.

Therefore neither 50 ms polling nor stopping the daemon fixes the glitch, and the daemon is not required for it to occur. A 100 ms build/test has not been run and is now low-value; a newer GoXLR firmware (if available) or a newer Ubuntu HWE kernel is the more useful next experiment. The stock `/usr/bin/goxlr-daemon` v1.2.4 is active again. The unused 50 ms binary remains staged at `/home/marlowfm/goxlr-daemon-1.2.4-poll50ms`; raw captures and scripts remain under `/tmp/goxlr-loopback-*` and `/tmp/run_goxlr_loopback.sh` on the PSA300.

This residual 100–137 ms playback fault is separate from Behringer USB resets and their multi-second reconnect/network cascade, although both are audible through PFL.

### Behringer resets continued after removing mouse and keyboard

The Behringer reset spontaneously at **10:40:33, 11:28:27, and 16:38:05 UTC** while it was the only full/low-speed device on the internal hub's shared Transaction Translator. This disproves the earlier claim that Behringer+keyboard/mouse TT contention was the complete reset cause. At 16:38 the reset invalidated ALSA handles, triggered Briclite's automatic full reconnect, and was followed by a genuine remote RTP outage of about **12.3 seconds**. That explains the reported multi-second PFL drop at that time; PSA CPU load and the local LAN were not the initiating fault.

The Behringer PCM2902 (`08bb:2902`) also exposes an otherwise-unused Consumer Control HID interface (mute/volume keys) at USB interface `1-1.1:1.3`. Linux `usbhid` recovery can call `usb_queue_reset_device()` after repeated interrupt-transfer errors, resetting the whole composite USB device including its audio interfaces. This is the strongest testable initiator hypothesis, but it is not yet proven because the default kernel log contains only the resulting reset.

At approximately 17:20 UTC that HID interface alone was **transiently unbound** from `usbhid`. The three `snd-usb-audio` interfaces remained bound, Briclite retained the same PCM handles/PID, both services stayed active, and no USB or audio fault occurred during the operation. Current `lsusb -t` shows the Behringer HID interface with `Driver=[none]`. This is a soak test:

- It is runtime-only; a Behringer reset/replug or host reboot will bind HID again.
- If no Behringer reset occurs over roughly 24 hours, make the ignore/unbind persistent (for example a device-specific HID ignore quirk or udev interface-unbind rule).
- If a reset occurs while HID is still unbound, reject the HID hypothesis and move to cable/device replacement, a powered multi-TT hub, or a PCIe xHCI controller.

### Web monitor controls fixed and deployed

- **Speaker Volume** now controls the GoXLR's actual `LineOut` master in GoXLR mode. It was verified live: 100%=`255`, 50%=`128`, then restored to 100%=`255`. In Behringer-only mode it controls a common `rx_vol` GStreamer gain; telemetry now includes `rx_volume`, and software gain survives pipeline rebuilds.
- **Bleep/PFL** now treats Headphones and Line Out as the same monitor pair. Engaging PFL removes Microphone/Chat/Game/LineIn from both and routes Music/studio return to both; releasing it restores the normal mix on both. Verified against live GoXLR routing after a physical Bleep press.

### Live state at this handoff (17:53 UTC)

- PSA300 kernel `6.8.0-139-generic`; `briclite.service` and stock `goxlr-daemon.service` active; codec connected to `217.36.229.106:5004`.
- Repository code exactly matches the four deployed files under `/opt/briclite/`.
- Behringer: USB `1-1.1`, audio interfaces bound, HID interface `1-1.1:1.3` unbound for the reset soak test.
- GoXLR Line Out and Headphones are both `255`. PFL is **off**: normal fader sources route to both outputs; Music/studio return routes to neither until Bleep is pressed.
- Several isolated `libav` malformed-AAC warnings occurred around reconnects/testing. They remain a separate receive-stream observation; no pipeline fault or persistent outage accompanied the latest ones.

## Update — 2026-09-10 morning: overnight unattended run + Behringer USB-reset log flood, now fixed

Graham left the codec running unattended overnight (2026-09-09 20:24 → 2026-09-10 07:xx) sending music to the remote, as a soak test. `briclite.service`/`goxlr-daemon.service` never crashed or restarted, and the TX/RX link to the remote never dropped or reconnected once — the core broadcast path held up cleanly all night.

However: a Behringer USB reset at 23:23:21 UTC (`usb 1-1.2.1: reset full-speed USB device`, kernel log) left one GStreamer ALSA element in the RX pipeline spinning forever on a dead PCM handle (`SNDRV_PCM_IOCTL_DELAY failed (-19): No such device`, ~20/sec). It was never noticed because `_interface_monitor`'s Behringer-presence poll doesn't detect a reset (the ALSA card stays enumerated throughout, only a full unplug/replug would have tripped it) and nothing else was watching for it. By the time it was checked at ~07:15 it had logged **583,838** lines (~430MB) over ~8 hours, still ongoing. Three more resets happened overnight without incident (01:22 ×2, 04:04, 04:46) — evidently not all resets trigger the stuck-thread condition, just some.

Initial reset hypothesis at that point: the Behringer (full-speed/12Mbps) shared its USB hub with the mouse and keyboard (low-speed/1.5Mbps), matching a known Linux/EHCI mixed-speed reset class. Later evidence showed this was incomplete: resets continued after the HID peripherals were removed. See the newer section above and `USB-AUDIO-GLITCH.md`.

**Fixed and deployed 2026-09-10, ~07:30 UTC:**
- `pipeline_manager.py`/`main.py`: a device-loss GST bus error now triggers an automatic `_full_reconnect()` (~2s), instead of the affected element spinning indefinitely. See `TROUBLESHOOTING.md` for the mechanism.
- `briclite.service`: added `LogRateLimitIntervalSec=30s`/`LogRateLimitBurst=1000` as a disk-space backstop (live unit at `/etc/systemd/system/briclite.service`, and documented in `BUILD.md` §7).
- Journal vacuumed on the PSA300 (439M → 268M) to reclaim the space the flood used; 94G free on `/`, was never actually at risk of filling overnight.
- Deployed via the documented one-shot restart+reconnect round-trip (`TROUBLESHOOTING.md` "deploying a code change causes an extended outage") — confirmed clean restart, immediate reconnect, no cascade.

**Update, later 2026-09-10:** both of the above were done later the same day — see the next section. Physically moving the Behringer off the HID hub: done (mouse/keyboard removed entirely, Behringer now direct). Validation: done, twice, via deliberate deauthorize/reauthorize of the Behringer's USB device — confirmed clean single-reconnect recovery, no flood.

## Update — 2026-09-10 later morning: PFL stutter investigation — three real bugs found and fixed, core glitch still open

Full technical detail, evidence, and upstream references for everything in this section are in **`USB-AUDIO-GLITCH.md`** — read that file for the deep dive. This section is the session summary and current live state.

### What was chased
Graham reported PFL audio stuttering then going silent (seconds to ~90s) shortly after pressing Bleep, then reconnecting on its own — "regularly reproducible" at first. Investigation established this is **not one bug** but an umbrella over at least four distinct fault signatures, only one of which (frequent small glitches with zero log signature) remains unexplained:

1. **Silent `audiomixer`/aggregator stall** — no bus error, RX/TX meters unaffected because they're upstream of the failure point. Already partially anticipated by a pre-existing code comment in `goxlr.py`. Mitigated with a new RX-hardware-sink buffer probe + `_rx_stall_watchdog()` in `pipeline_manager.py` (2.5s threshold, guarded by RX network still being recent). Not yet caught firing on a confirmed clean reproduction — every live repro that got instrumented turned out to be #2 instead.
2. **Genuine RX network gaps** following any TX interruption (hotplug, restart) — the remote drops its own session on a TX gap and takes ~30-45s to notice and reconnect. Pre-existing, well-understood, self-heals via the existing 10s `_rx_watchdog`. Confirmed definitively via new instrumentation (RTP sequence-continuity check + `FIONREAD` socket-queue-depth probe) — every genuine capture showed 0 bytes queued throughout, i.e. real loss, not local scheduling delay.
3. **GoXLR's own tight ALSA error loop** when the device is briefly absent (e.g. mid physical-swap) — caught by the same device-error auto-reconnect as the Behringer flood fix.
4. **Frequent small "regular glitches"** — reported twice, live-checked both times, **zero log entries of any kind** during active glitching. This is the one still open; see `USB-AUDIO-GLITCH.md` for why it's most likely the actual upstream implicit-feedback hardware/firmware bug (kernel bugzilla #211211, alsa-lib #113) rather than anything this investigation's instrumentation can catch.

### Diagnostic instrumentation added (all in `pipeline_manager.py` / `goxlr.py`)
- RX/TX/sink buffer gap logging (`_GAP_LOG_THRESHOLD_S = 0.3s`) with RTP sequence-delta verdict distinguishing "local read delay" from "real network loss."
- `_socket_recv_queue_bytes()` — `FIONREAD`-based kernel receive-buffer probe, polled every second whenever RX is idle; settles the local-vs-network question even when the coarser watchdog tears the socket down before a "next packet" can be measured.
- `goxlr.py`'s `_ipc()` now times every GoXLR daemon IPC call (`goxlr.ipc` logger) — revealed the daemon's own constant ~50-100Hz background status polling (see `USB-AUDIO-GLITCH.md`), a likely bigger factor than briclite's own occasional command bursts.

### usbmon caution — read before using it again
Used twice for raw USB-bus evidence. The second attempt, run **while Graham was actively listening to PFL**, itself caused audible audio breakup worse than the glitch being investigated. Stopped immediately. Do not run `usbmon` (or any full-rate USB trace) while anyone is relying on the live audio — see `USB-AUDIO-GLITCH.md` for the likely mechanism (CPU/interrupt load perturbing the same fragile timing). The two captures taken (a 6s baseline, a 15-minute rolling capture) are still valid for what they directly decoded (the daemon's polling format/rate) but not as a clean glitch-frequency baseline.

### USB topology — confirmed empirically, not just theorised
`lsusb -v` on the PSA300's internal hub chip: `bDeviceProtocol: 1 Single TT`, 8 ports. **Both of the two physical rear ports lead into the same internal hub** — which port a device uses makes no topological difference. (This contradicts an 2026-09-09 documentation claim that the "USB3"/blue port gave the GoXLR a direct root-hub path — that claim doesn't hold up under this evidence and should be treated as superseded; see `USB-AUDIO-GLITCH.md`.) Mouse and keyboard were removed and the Behringer moved to a direct port (no external hub), leaving it as the only full/low-speed device on the shared Single-TT hub — this should reduce (not eliminate) the reset frequency behind the Behringer-flood fix's root cause, but hasn't yet been confirmed over a long unattended window. Several deliberate port swaps were done afterward purely as recovery-mechanism stress tests (see next section) — they don't change anything topologically, since both ports are equivalent.

### Three real software bugs found (all unrelated to USB topology) — all fixed and deployed
1. **Behringer fallback ALSA device misconfigured** (`hw:0,0` instead of `hw:CODEC,0`) — the automatic GoXLR→Behringer failover (auto mode) had never actually pointed at the Behringer; `hw:0,0` normally resolves to the GoXLR itself. Any GoXLR hotplug triggered a ~11s error cascade instead of a clean fallback. Fixed in both live `config.json` and `config.example.json`.
2. **Interface hot-swap never called `.start()`** — after GoXLR reappears and `_interface_monitor` switches back to it, the pipeline was rebuilt but left in `Gst.State.NULL` forever; codec silently stayed disconnected until a manual `/api/connect`. Pre-existing bug, only exposed today because the GoXLR had never before gone genuinely absent-then-present in this investigation.
3. **RX watchdog race condition** — `_rx_watchdog()` and the interface-hotswap path each rebuilt the shared `controller` global inline, unlocked (unlike `_full_reconnect()`, which is properly locked). Rapid concurrent triggers during a USB swap test raced on that global and silently disabled the RX watchdog's own outage detection — a real outage then persisted **100+ seconds with zero automatic recovery**, worse than anything else seen this whole investigation. Fixed by routing both paths through the same locked `_full_reconnect()`. See `ARCHITECTURE.md` §8 for the now-documented invariant.

All three are written up in full in `TROUBLESHOOTING.md`.

### Live state at end of session (2026-09-10, ~09:50 UTC)
- `briclite.service` active, connected (`TX→217.36.229.106:5004 RX←:5004`), no ongoing errors.
- USB topology: mouse/keyboard removed; GoXLR and Behringer both on the PSA300's internal Single-TT hub (which physical port each uses doesn't matter — see above). Last known arrangement before the final deliberate swap test: GoXLR on the "standard" port, Behringer on "blue" — but this was swapped multiple times during testing and may not reflect the very final physical state; verify with `lsusb -t` before relying on it.
- Journal: `journalctl --disk-usage` flat throughout despite one flood-scale event during testing (rate limiter held).
- Code changes from this session (pipeline_manager.py, main.py, goxlr.py, config.example.json) were uncommitted as of this write-up — check `git log`/`git status` to confirm whether they've since been committed.

### Next steps for whoever picks this up
1. **Signature #4 (frequent small glitches) is the real open problem.** See `USB-AUDIO-GLITCH.md` → "Where this leaves things" for concrete next steps (narrow, announced `usbmon` window; investigate `goxlr-daemon` poll-rate options; don't re-conflate with the other three signatures).
2. Let the topology change (HID removed) run unattended for a while and check whether Behringer USB resets (`journalctl -k | grep 'usb 1-1'`) actually became rarer.
3. If another concurrency bug like #3 above turns up, the fix pattern is the same: route it through `_full_reconnect()`, never touch the shared `controller` global directly.

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

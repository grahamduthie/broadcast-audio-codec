# The GoXLR Mini USB Audio Glitch — Full Investigation Record

This document is the single place to read for the full history of the residual audio glitch/dropout problem on the PSA300. `TROUBLESHOOTING.md` has shorter, task-oriented entries for the individual symptoms and fixes; this file is the narrative and the evidence behind them. Read `CURRENT-STATUS.md` first for what's live right now — this file is background, not a live-state doc.

There are, as of 2026-09-10, at least **four distinct fault signatures** under the "PFL glitches/drops out" umbrella. They are not all the same bug. Two are well-understood and self-heal automatically. One is understood but still unmitigated in hardware. One is still a mystery. This document tries to keep them clearly separated rather than lumping them together, because that conflation cost real time early in the investigation.

## What it is, in one paragraph

The GoXLR Mini's USB audio playback path (the direction that carries Music/Game/Chat/System to your ears and to the GoXLR's own hardware Broadcast Mix) uses **asynchronous isochronous transfer with implicit feedback** — a USB Audio Class mechanism where the host has to actively pace exactly when it sends playback packets, inferring the device's real clock from timing on the device's *capture* endpoint. This is a delicate, well-documented-as-fragile mechanism in Linux's USB audio stack, and this specific device/driver combination is known upstream to glitch under it while capture (the direction TX reads from) stays completely clean. Various kinds of USB bus activity — a USB reset elsewhere on the same controller, a burst of vendor control commands to the GoXLR itself, a firmware/driver hiccup with no clean signal machinery — can perturb that timing and produce anything from a brief click to tens of seconds of silence.

## The four fault signatures

### 1. Behringer USB reset → zombie spinning thread (SOLVED, software fix in place)

**Symptom:** none audible in isolation — this one manifests as a runaway journal (hundreds of thousands of log lines, hundreds of MB) rather than as sound. First caught 2026-09-10 morning after an unattended overnight run.

**Mechanism:** the Behringer's USB hub port undergoes a kernel-level reset (`usb 1-1.2.1: reset full-speed USB device`) — the kernel's own recovery action for whatever the underlying condition was (default log verbosity doesn't capture the precipitating error, only the reset itself). This leaves whichever GStreamer ALSA element held that PCM handle (the Behringer `alsasrc`, or `goxlr_mix`'s downstream `alsasink`) with a handle to a device that no longer validly exists. GStreamer doesn't always treat this as pipeline-fatal — the affected element's clock-polling thread just keeps re-issuing the same failing ioctl (`SNDRV_PCM_IOCTL_DELAY failed (-19): No such device`) forever, at roughly 20/sec, because nothing ever tears the pipeline down to reopen the handle. One occurrence produced 583,838 log lines (~430MB) over ~8 hours before anyone noticed.

Critically, this is **invisible to presence-based hotplug detection** — `GoXLRInterface.behringer_available()` just checks whether the card is listed in `/proc/asound/cards`, and a *reset* (as opposed to a full unplug) never removes the card entry, so the poll never notices anything changed.

**Root cause of the resets themselves:** the Behringer (full-speed/12Mbps) was, at the time, sharing a USB hub with a mouse and keyboard (both low-speed/1.5Mbps) behind this board's EHCI-only controller. See "USB topology and the hardware root cause" below.

**Fix (software, 2026-09-10):** `pipeline_manager.py`'s bus-message handler now matches `_DEVICE_ERROR_PATTERNS` ("disconnected", "no such device", "input/output error") against any `GST_MESSAGE_ERROR` text and treats a match as device loss rather than an ordinary stream error, calling back into `main.py`'s `_on_pipeline_fault()` (originally named `_on_device_error` — renamed once it grew a second trigger, see #2 below), which does a full `_full_reconnect()` (~2s) instead of leaving the spin running. As a disk-space backstop independent of that fix, `briclite.service` also sets `LogRateLimitIntervalSec=30s`/`LogRateLimitBurst=1000` (systemd's own default of 10000/30s is far too loose to catch a sustained ~20/s spin).

**Validated:** twice, live, by deliberately deauthorizing/reauthorizing the Behringer's USB device (`echo 0/1 > /sys/bus/usb/devices/.../authorized`) while the pipeline was running. First attempt exposed a second bug (the RX-extra-source presence flag going stale after a device-error-triggered rebuild — also fixed, see `TROUBLESHOOTING.md`); second attempt recovered cleanly in ~7 seconds with the Chat mic branch correctly restored.

### 2. Silent `audiomixer`/aggregator stall (MITIGATED, watchdog in place, root mechanism still unconfirmed)

**Symptom:** what the user actually experiences most often — PFL audio stutters for a second or two, then goes completely silent, typically for tens of seconds up to ~90s, then reconnects and resumes cleanly on its own. RX peak meters and TX continue showing normal levels throughout, because they're measured upstream of the point of failure.

**First observed:** 2026-09-10, ~08:00 UTC, shortly after engaging Bleep/PFL for the first time that morning. A kernel USB reset on the HID hub port (`1-1.2.4`, then carrying the mouse/keyboard) occurred 99 seconds after PFL was engaged — close to the reported "~90 seconds after starting to listen" pattern — with **no** GStreamer bus error and **no** goxlr-daemon error logged at all. The existing code comment in `goxlr.py`'s `rx_sink_bin()` already anticipated this class of fault from an earlier (undated, pre-2026-09-10) investigation: *"the audiomixer/aggregator element has proven unreliable in this pipeline even with a single pad connected — intermittently stops producing output while everything upstream (decode, meter) keeps working, with no error ever posted to the bus."*

**Why RX/TX meters look fine:** `rx_meter` (the `level` element used for the RX peak display) sits *before* `goxlr_mix` in the pipeline; TX reads from the GoXLR's separate capture endpoint entirely, upstream of the playback-side bug (see "Why TX never glitches" below). Neither can see a stall that only affects the mixer's *output*.

**Fix (software, 2026-09-10):** `pipeline_manager.py` now attaches a `Gst.PadProbeType.BUFFER` probe to the RX pipeline's actual hardware sink(s) (found generically via `pipeline.iterate_sinks()`, so it works for any interface's `rx_sink_bin()`), timestamping every buffer that reaches hardware. A new `_rx_stall_watchdog()` coroutine checks every second: if the sink has produced no output for `_RX_SINK_STALL_S` (2.5s) **while RX network packets are still arriving** (the `rx_network_recent` guard, which is what stops this from firing redundantly with the separate 10s network watchdog in `main.py`), it declares a local playout stall and triggers the same `_full_reconnect()` path as #1.

**Not fully closed:** this watchdog treats the *symptom* (no output reaching hardware) rather than a confirmed root cause inside the aggregator itself. It has not yet been caught firing on a genuine reproduction of *this exact* signature in isolation — every other live reproduction attempt on 2026-09-10 turned out to be signature #3 (a real network gap) instead, once instrumented. It remains plausible but unconfirmed that repeated observations attributed to this signature are actually intermittent instances of #3 or #4.

### 3. Genuine RX network gap following any TX interruption (UNDERSTOOD, pre-existing, self-heals in ~10-15s)

**Symptom:** RX goes idle for several seconds to ~15s, `main.py`'s existing `_rx_watchdog` fires ("No RX packets for Xs — auto-reconnecting"), reconnects, resumes.

**Mechanism, already documented in `_rx_watchdog`'s own docstring:** the remote (a Comrex-like device) drops its own return session when it sees a gap in *our* TX stream. Anything that causes even a brief TX interruption — a Behringer hotplug reconnect, a service restart, an interface hot-swap — triggers this: the remote notices the TX gap, drops its side, and takes some seconds (empirically ~30-45s from the TX gap to the RX gap becoming visible, then up to ~15s more for our own watchdog's 10s threshold plus reconnect time) to reconnect on its own.

**Confirmed repeatedly, e.g. 2026-09-10:**
- Behringer hotplug at `09:11:44` (a ~3s TX gap while rebuilding) → RX gap detected `09:12:28`–`09:12:39` (11.5s) → auto-reconnected.
- A `systemctl restart` at `09:40:26` → PFL engaged `09:40:57` → RX gap `09:41:24`–`09:41:35` (11.1s) → auto-reconnected.

**Diagnostic added 2026-09-10** to distinguish this from #2/#4 with certainty rather than inference: `pipeline_manager.py`'s `_rx_loop` now logs any gap over `_GAP_LOG_THRESHOLD_S` (0.3s) between successfully-parsed RTP packets, including the **RTP sequence-number delta across the gap** — a delta of ~1 despite a multi-second real-time gap means the packets were sitting in the OS socket buffer the whole time and the reading thread just wasn't scheduled (a genuinely local stall); a delta matching the gap's duration at the stream's ~23.4 pkt/s rate means they were never sent/received at all (confirming a real network-side gap). A second, independent check (`_socket_recv_queue_bytes()`, using `FIONREAD`) polls the kernel's actual UDP receive-buffer depth every second whenever RX is idle, which settles the same question without depending on a "next packet" ever arriving to measure against (useful because the coarser 10s watchdog can tear the socket down before the fine-grained per-packet logger gets a chance).

Every genuine reproduction captured with this instrumentation live on 2026-09-10 showed **0 bytes queued** throughout — real network gaps, not local stalls, in every case that was actually caught and measured.

### 4. Frequent small "regular glitches" (UNRESOLVED — likely the true core hardware/firmware bug)

**Symptom:** brief, frequent audible glitches/pops during PFL listening, reported 2026-09-10 ~09:22 UTC ("I'm listening to the PFL now and still hearing regular glitches... much worse than the usual". Separately reported again after topology changes: "still hearing regular glitches").

**Status: completely invisible to all current instrumentation.** A live check during an active glitching session (`journalctl -u briclite --since '3 min ago'`) returned **zero** matching log lines — no RX gap, no TX gap, no sink-buffer gap, no bus warning/error, nothing crossing any threshold this investigation has instrumented. This is the strongest evidence that signature #4 is a distinct, lower-severity phenomenon from #2/#3, not just an unlogged instance of them — and that it's most likely the actual implicit-feedback hardware/firmware fragility itself (see "Upstream references" below), too brief and too deep in the USB stack for any of GStreamer/ALSA/the application layer to see.

**Not yet tried:** raw USB-level tracing (`usbmon`) during a live glitch would be the natural next diagnostic step, but **do not do this while anyone is actively listening** — see "A real methodological trap: usbmon makes it worse" below.

## Why TX never glitches while RX does

This asymmetry (confirmed by direct observation throughout the investigation, and consistent with upstream reports) comes from the GoXLR's USB endpoint design, not from anything briclite does differently on each side:

- **Playback (RX/monitoring), OUT endpoint 0x08:** *asynchronous isochronous with implicit feedback* — the host has to actively pace exactly when it sends packets, inferring the device's true sample clock from timing observed on the *capture* endpoint. This is the fragile, well-documented-as-buggy mechanism.
- **Capture (TX), IN endpoint 0x88:** plain isochronous read — the device streams what it has, the host reads it. No host-side clock-matching loop to get wrong.

Upstream reports (see below) describe exactly this signature: "output stutters, capture remains clean."

## Upstream references

- [Linux kernel bugzilla #211211](https://bugzilla.kernel.org/show_bug.cgi?id=211211) — GoXLR-class output stuttering with direct ALSA while capture remains clean; implicit-feedback handling implicated.
- [alsa-lib issue #113](https://github.com/alsa-project/alsa-lib/issues/113) — same symptom class, alsa-lib side.
- [LKML: "USB EHCI: repeated resets on full and low speed devices"](https://lkml.kernel.org/lkml/5393eab7-8203-1696-ffc6-7e06cd63638a@oracle.com/T/) — long-standing (2008-era, still open), describes exactly the EHCI/Transaction-Translator interaction behind fault signature #1's root cause (full+low-speed devices sharing one hub's TT under an EHCI controller). Confirms this class of USB-level fault is a genuine, unfixed-upstream kernel/hardware limitation, not something specific to this deployment.

## USB topology and the hardware root cause

This board (Supermicro X10SBA-L, Intel Atom Z36xx/Z37xx SoC) has **exactly one USB controller** (`lspci`: `00:1d.0 USB controller: Intel Atom... EHCI`) — confirmed via `lsusb`: only one root hub (`Linux Foundation 2.0 root hub`), no separate xHCI/USB3 controller anywhere on the board, regardless of what a given rear port's physical colour suggests.

Both of the PSA300's two rear USB ports lead into **the same internal 8-port hub chip**, confirmed decisively via `lsusb -v`:
```
bDeviceProtocol   1 Single TT
nNbrPorts         8
```
"Single TT" means all 8 ports share **one** Transaction Translator for the whole hub. This has two practical consequences, both confirmed empirically 2026-09-10:
1. **Which of the two rear ports a device uses makes no topological difference whatsoever** — swapping the GoXLR and Behringer between "standard" and "blue" ports produces the identical hub relationship every time. (There had been a documentation claim from 2026-09-09 that the "USB3"/blue port put the GoXLR as "a direct child of the root hub" — this does not hold up under the 2026-09-10 `lsusb -v` evidence; that claim was likely based on an incomplete reading of `lsusb -t` and should be treated as superseded.)
2. **A high-speed device (the GoXLR) doesn't contend for the shared TT at all** — the TT only translates between a high-speed upstream link and a full/low-speed downstream device, so the GoXLR sharing this hub with the Behringer isn't itself a TT-contention risk. The real risk is **multiple full/low-speed devices** sharing the one TT — which was the Behringer + mouse + keyboard, all three, prior to 2026-09-10's hardware change.

Timeline of the physical topology:
- **Through 2026-09-09:** GoXLR and Behringer shared a hub together (plus keyboard/mouse on a nested external hub). Fixed same day by moving GoXLR onto its own hub/port and Behringer+HID onto a separate external hub.
- **2026-09-10, morning:** confirmed the Behringer was still sharing its hub with the mouse and keyboard — the class of interaction the LKML thread describes. 5 unattended overnight kernel-level resets logged, at least one of which triggered fault signature #1.
- **2026-09-10, later:** mouse and keyboard physically removed (not needed for this test), Behringer plugged directly into the PSA300's second port (no longer via an external hub). This leaves the Behringer as the **only** full/low-speed device on the shared internal hub, eliminating the multi-device TT contention that fault signature #1's root cause depends on. Not yet observed over a long enough unattended window to confirm reset frequency has actually dropped — this is the natural experiment now running.
- Multiple deliberate port swaps performed afterward as stress tests of the recovery machinery (see `CURRENT-STATUS.md` for what those swaps incidentally exposed — three real software bugs, unrelated to the topology itself).

## A real methodological trap: usbmon makes it worse

`usbmon` (the kernel's built-in USB bus tracer, `/sys/kernel/debug/usb/usbmon/1u`) was used twice on 2026-09-10 to try to get raw USB-level evidence of the glitches. **The second attempt, run live while the user was actively listening to PFL, visibly made the audio break up "quite badly — much worse than the usual glitches."** Stopped immediately.

Mechanism (inferred, not separately proven): `usbmon` in this text/data mode traces every USB transaction on the bus — including the GoXLR's own isochronous audio streams, which run at roughly 4,000 packets/second — dumping full hex payloads for each through the kernel's debugfs interface, piped through `cat`/`grep`. That's genuine, sustained CPU and interrupt-path load on hardware this whole investigation has already established is fragile under exactly that kind of pressure (the playout thread needs `SCHED_FIFO` just to survive ordinary scheduling contention — see `ARCHITECTURE.md` §10/§12.4). Tracing the GoXLR's own audio endpoints at full rate very plausibly perturbs the same timing-sensitive implicit-feedback mechanism this investigation is trying to observe — a heisenbug in the opposite direction from the one raised earlier in the session (instrumentation *masking* a bug via timing changes): here, instrumentation *aggravated* one.

**Consequence for future sessions:** the two earlier `usbmon` captures taken before this was noticed (a 6-second baseline and a 15-minute rolling capture) cannot be fully trusted for glitch-*frequency* conclusions, since the capture itself was adding load throughout. Their content is still valid for what it directly decoded (e.g. the daemon's control-endpoint request format and timing — see below), just not as a clean baseline of "how often does this glitch without interference."

**Rule going forward: never run `usbmon` (or any full-rate USB trace) while someone is actively listening to or relying on the live audio.** If raw USB-level evidence is needed again, prefer either a much narrower `usbmon` filter (a specific endpoint only) or accept degraded audio as the deliberate cost of a short, announced diagnostic window.

## goxlr-daemon's own background USB traffic

Confirmed via a clean 6-second `usbmon` baseline capture (taken before the interference problem above was identified) and decoded: `goxlr-daemon` continuously polls the GoXLR's **entire status blob (4,160 bytes)** over USB control transfers on endpoint 0, roughly every 10-20ms, forever, independent of anything briclite does:
```
Ci:1:007:0  bmRequestType=0xc1 (vendor, device→host)  bRequest=0x03  wLength=0x1040 (4160 bytes)
Co:1:007:0  bmRequestType=0x41 (vendor, host→device)  bRequest=0x02  wLength=0x0010 (16 bytes)
```
The 16-byte write payload carries a monotonically incrementing counter — a genuine fixed-interval heartbeat/poll loop, not sporadic activity. This constant ~50-100Hz background traffic on the GoXLR's own control endpoint, continuously interleaved with its audio isochronous streams, is the most plausible explanation for the earlier (2026-09-09) finding that *"`goxlr-daemon` strongly increases the glitch rate but is not the sole cause."* `goxlr-daemon` (v1.2.4, the GoXLR-on-Linux project) exposes **no configuration option** to slow this down — checked via `--help` and `settings.json`; it would require patching and rebuilding the daemon, which is out of scope here.

On top of that constant baseline, briclite adds its own occasional bursts of the same kind of control traffic: ~40 `SetRouter`/`SetFader`/`SetColour` commands (~1.6-1.7s of continuous back-to-back USB traffic) on every pipeline connect/reconnect, and ~7 commands (~250-300ms) on every Bleep press. Each individual command measured 15-90ms round-trip (median ~30-45ms) via the per-call timing instrumentation added to `goxlr.py`'s `_ipc()` on 2026-09-10 (`ipc_logger`, `goxlr.ipc` in the journal). These bursts are real and plausibly aggravating, but are spikes on top of a much larger constant background load this investigation cannot control — they are very unlikely to be the primary cause on their own.

## Mitigations applied so far (2026-09-10) — summary

| # | Mitigation | Addresses | Status |
|---|---|---|---|
| 1 | Bus-error device-loss detection → auto-reconnect (`_DEVICE_ERROR_PATTERNS`, `_on_pipeline_fault`) | Signature #1 | Validated live, twice |
| 2 | RX hardware-sink stall watchdog (`_rx_stall_watchdog`, buffer probes) | Signature #2 | Deployed, not yet caught firing on a clean reproduction |
| 3 | Journald per-unit rate limiting (`LogRateLimitIntervalSec`/`Burst`) | Disk-space blast radius of any future flood | Validated live under a worse flood than #1's original |
| 4 | Fine-grained RX/TX/sink gap + sequence-continuity + socket-queue-depth logging | Distinguishing #2/#3/#4 from each other | Deployed, working as designed |
| 5 | GoXLR IPC per-command timing (`ipc_logger`) | Correlating Bleep/connect bursts against gaps | Deployed |
| 6 | Removed mouse/keyboard from the shared hub; Behringer moved to a direct port | Signature #1's root cause (multi-device TT contention) | Done, effect on reset frequency not yet confirmed over a long unattended window |
| 7 | Fixed Behringer fallback ALSA device (`hw:0,0` → `hw:CODEC,0`) | A misconfiguration that turned ordinary GoXLR hotplugs into ~11s error cascades | Fixed and deployed |
| 8 | Fixed interface hot-swap never calling `.start()` | Codec silently staying disconnected after any GoXLR-absent→present transition in auto mode | Fixed and deployed |
| 9 | Serialised all pipeline-rebuild paths through one lock (`_full_reconnect()`) | A race that could silently disable the RX network watchdog entirely under concurrent triggers | Fixed and deployed |

None of these fix signature #4, the frequent small glitches, which remains the open problem.

## Where this leaves things — recommendations for whoever picks this up next

1. **Signature #4 is almost certainly the real underlying hardware/firmware bug** this whole multi-day investigation has been circling — the implicit-feedback playback fragility documented upstream. It may simply not be fixable from the Linux/application side. Don't expect the mitigations above to touch it; they were built for the *other* signatures.
2. If more evidence on #4 is genuinely needed, get it via a **short, narrowly-filtered, announced** `usbmon` window (not a long unattended one, and never while someone is relying on the live audio) — filter to one endpoint if possible to minimise the interference problem above.
3. Consider whether `goxlr-daemon`'s constant ~50-100Hz polling could be reduced by a means other than patching it — e.g. checking whether a newer daemon version exposes a poll-rate setting, or whether the specific status fields it's requesting could be narrowed.
4. The topology change (removing HID devices from the shared hub) needs a long unattended run to confirm whether it actually reduced signature #1's reset frequency, separate from and in addition to the software fix that now makes signature #1 self-heal regardless.
5. Don't re-conflate the four signatures. If something "glitches," check the log for RX/TX/sink gaps and their sequence-continuity verdict *before* assuming it's the same thing as the last report — this session's biggest wasted effort was treating early reports as one problem when they turned out to be at least three.

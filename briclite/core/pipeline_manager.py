import gi
import threading
import asyncio
import fcntl
import logging
import os
import socket
import struct
import sys
import random
import termios
import time

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
from .data_broker import global_state
from interfaces.base import AudioInterface

logger = logging.getLogger("pipeline")

_WRAP      = 0x10000
_MATRIX_STRINGS = {
    "stereo": "<<1.0,0.0>,<0.0,1.0>>",
    "left":   "<<1.0,0.0>,<1.0,0.0>>",
    "right":  "<<0.0,1.0>,<0.0,1.0>>",
}
_CLEAN_NEWS_MATRIX = "<<0.0,1.0>,<0.0,1.0>>"  # RX Right as dual-mono news
_RTP_CLOCK = 90000
_AAC_SAMP  = 24000
_AAC_FRAME = 1024
_FRAME_S   = _AAC_FRAME / _AAC_SAMP
_TS_INC    = _AAC_FRAME * _RTP_CLOCK // _AAC_SAMP   # 3840

# Headroom against transient scheduling stalls (GIL contention from the meter/
# telemetry threads, CPU load spikes) on the weak embedded hardware this runs
# on. This is a ceiling only — the queue sits near-empty in steady state, so
# raising max-size costs no steady-state latency, just stall headroom.
# See ARCHITECTURE.md §10/§12.4.
_RX_QUEUE_BUFFERS   = 50        # ~2.1s of compressed AAC frames, was 10 (~430ms)
_PLAYOUT_RT_PRIORITY = 10       # SCHED_FIFO priority for the playout thread (Linux only)

# Substrings (lowercased) that identify a GST_MESSAGE_ERROR as the underlying
# ALSA device having gone away rather than an ordinary stream/format error —
# e.g. a USB audio device (Behringer) dropped and re-enumerated by the kernel
# mid-session. When one of these hits the bus, the element that lost its
# device keeps existing but its clock-polling thread spins forever re-issuing
# the same failing ioctl (observed: SNDRV_PCM_IOCTL_DELAY, hundreds of
# thousands of log lines over several hours) because nothing ever tears the
# pipeline down. See TROUBLESHOOTING.md "Behringer USB resets".
_DEVICE_ERROR_PATTERNS = ("disconnected", "no such device", "input/output error")

# How long the RX hardware sink can go with no buffer reaching it, while RX
# network packets keep arriving, before we treat it as a silent stall rather
# than ordinary studio silence (silence is still encoded/transmitted as AAC
# frames at the normal ~42ms cadence, so a healthy pipeline never actually
# goes this long between buffers regardless of how quiet the source is).
# Distinct from _rx_watchdog's 10s threshold in main.py, which detects the
# *network* going quiet — this detects local playout going quiet while the
# network stays healthy, e.g. GStreamer's audiomixer/aggregator silently
# wedging (no error posted to the bus — see goxlr.py's rx_sink_bin() comment,
# and TROUBLESHOOTING.md "PFL stutters and goes silent ~90s after Bleep").
_RX_SINK_STALL_S    = 2.5
_STALL_CHECK_INTERVAL_S = 1.0

# Diagnostic instrumentation added 2026-09-10 to chase the "PFL stutters and
# goes silent" symptom (TROUBLESHOOTING.md) — logs any gap over this length
# on the RX socket read loop, the RX hardware sink, and the TX capture
# stream, whenever one occurs, well below the 2.5s/10s watchdog thresholds
# above so we get the full timeline rather than just "it's been broken a
# while." _rx_loop additionally logs the RTP sequence delta across the gap:
# a delta of ~1 despite a multi-second real-time gap means the packets were
# sitting in the OS socket buffer the whole time and _rx_loop's thread just
# wasn't scheduled to read them (a local stall) — a delta matching the
# gap's real-time duration at the stream's ~23.4 pkt/s rate means they
# genuinely weren't sent/received (a real network gap).
_GAP_LOG_THRESHOLD_S = 0.3
_RTP_PACKET_RATE     = _AAC_SAMP / _AAC_FRAME   # ~23.4375 pkt/s


def _seq_after(a: int, b: int) -> bool:
    return 0 < ((b - a) % _WRAP) < _WRAP // 2


class JitterBuffer:
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
            if self._next_seq is not None and not _seq_after(self._next_seq - 1, seq) and seq != self._next_seq:
                self.packets_late += 1
                return
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
                    if any(_seq_after(self._next_seq, s) for s in self._buf):
                        self.packets_lost += 1
                        self._next_seq = (self._next_seq + 1) % _WRAP
                        return last_good or b""
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

    def __init__(self, config: dict, interface: AudioInterface, rx_channel_mode: str = "stereo",
                 rx_volume_pct: int = 100, on_pipeline_fault=None):
        Gst.init(None)
        net = config["audio_network"]
        self.interface  = interface
        self.target_ip  = net["target_ip"]
        self.tx_port    = net["tx_port"]
        self.rx_port    = net["rx_port"]
        self.latency_ms = net.get("buffer_ms", 200)

        # Coroutine (no args) scheduled on self.event_loop the first time
        # either (a) a device-loss error hits the bus (_DEVICE_ERROR_PATTERNS)
        # or (b) the RX stall watchdog notices the hardware sink went quiet
        # while RX packets kept arriving (_RX_SINK_STALL_S) — two distinct
        # failure signatures needing the same fix: tear down and rebuild
        # every ALSA handle. (b) exists because GStreamer's audiomixer can
        # wedge with *no* bus error at all — see goxlr.py's rx_sink_bin().
        self.on_pipeline_fault    = on_pipeline_fault
        self._device_error_seen   = False
        self._stall_reported      = False
        self._last_rx_sink_buffer = time.monotonic()
        # Diagnostic-only, separate from the above: None until the first
        # buffer after a (re)build actually arrives, so build/startup latency
        # itself never gets logged as a false "gap" — see _on_rx_sink_buffer.
        self._last_rx_sink_buffer_seen = None

        self.event_loop = None
        self.is_active  = False
        self.rtp_seq    = random.randint(0, 65535)
        self.rtp_ts     = random.randint(0, 0xFFFFFFFF)
        self.rtp_ssrc   = random.randint(0, 0xFFFFFFFF)

        self.rx_channel_mode  = rx_channel_mode
        # Speaker-volume gain for the network receive path. Keep this on the
        # controller rather than only on the GStreamer element so an RX-only
        # pipeline rebuild (channel-mode change, USB hotplug, etc.) preserves
        # the operator's selected level.
        self.rx_volume_pct = max(0, min(100, rx_volume_pct))
        self._rx_rebuild_timer = None
        self._rx_rebuild_lock  = threading.Lock()
        self.jitter_buf  = JitterBuffer(latency_ms=self.latency_ms)
        self.tx_pipeline = None
        self.rx_pipeline = None
        self.rx_appsrc   = None
        self._clean_news_return_level = 255
        self._clean_news_return_muted = False
        self._headphone_timing_probe_file = None
        self.sock        = None
        self.glib_loop   = GLib.MainLoop()
        self.last_rx_packet = 0.0
        self._last_tx_sample = 0.0
        # PTS fed to rx_appsrc, in nanoseconds. Must live on the instance,
        # not as a local in _playout_loop(): stop()/start() spawn a fresh
        # playout thread on every reconnect but never rebuild rx_pipeline/
        # rx_appsrc (see begin_local()'s docstring — only full_stop()+a new
        # PipelineController instance does that, which is when this should
        # actually go back to 0). A local variable resetting to 0 on each
        # new thread would push buffers with PTS far behind the appsrc's
        # already-advanced internal segment, corrupting the AAC/SBR decoder
        # state for an extended period after every plain reconnect.
        self.rx_pts = 0

        # Ground truth for "does the currently-built RX pipeline actually
        # include an extra source (e.g. the Behringer)?" — set on every
        # (re)build, from whichever code path triggered it. main.py's
        # _interface_monitor compares this against live presence instead of
        # tracking its own separate "last seen" flag, so a rebuild triggered
        # by something else (e.g. on_pipeline_fault) can't leave the monitor's
        # bookkeeping stale and the branch silently missing thereafter.
        self.rx_extra_sources_active = False

        self._build_pipelines()

    def _build_rx_pipeline(self, mode: str):
        matrix = _MATRIX_STRINGS.get(mode, _MATRIX_STRINGS["stereo"])
        rx_rate = self.interface.rx_sample_rate()
        self.rx_extra_sources_active = bool(self.interface.extra_rx_source_bins())
        decoded = (
            f"appsrc name=rx_src is-live=true format=time block=false ! "
            f"queue max-size-buffers={_RX_QUEUE_BUFFERS} max-size-bytes=0 max-size-time=0 ! "
            f"audio/mpeg,mpegversion=4,stream-format=adts ! "
            f"avdec_aac ! audioconvert"
        )
        monitor = (
            f' ! audiomixmatrix name=rx_router in-channels=2 out-channels=2 matrix="{matrix}" ! '
            f"level name=rx_meter ! audioresample ! "
            f"audio/x-raw,rate={rx_rate},channels=2 ! "
            f"volume name=rx_vol volume={self.rx_volume_pct / 100.0} ! "
            f"{self.interface.rx_sink_bin()}"
        )
        if self.interface.clean_news_return_enabled():
            # Do not let a slow/failed inter-pipeline handoff back-pressure local RX audio.
            # The tap is before rx_router so changing the monitor channel mode
            # cannot accidentally change which remote feed returns as news.
            rx_str = (
                f"{decoded} ! tee name=decoded_rx "
                f"decoded_rx. ! queue{monitor} "
                f"decoded_rx. ! queue leaky=downstream max-size-time=500000000 "
                f"max-size-bytes=0 max-size-buffers=0 ! "
                f'audiomixmatrix in-channels=2 out-channels=2 matrix="{_CLEAN_NEWS_MATRIX}" ! '
                f"audioresample ! audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=2 ! "
                f"{self.interface.clean_news_sink_bin()}"
            )
        else:
            rx_str = f"{decoded}{monitor}"
        for extra in self.interface.extra_rx_source_bins():
            rx_str += f" {extra}"
        logger.info(f"RX ({mode}): {rx_str}")
        pipeline = Gst.parse_launch(rx_str)
        appsrc = pipeline.get_by_name("rx_src")
        appsrc.set_property("caps", Gst.Caps.from_string("audio/mpeg,mpegversion=4,stream-format=adts"))
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        self._last_rx_sink_buffer = time.monotonic()
        self._last_rx_sink_buffer_seen = None
        self._stall_reported = False
        self._attach_rx_stall_probe(pipeline)
        return pipeline, appsrc

    def _attach_rx_stall_probe(self, pipeline):
        """Timestamp every buffer that actually reaches the RX pipeline's
        hardware sink(s) — interface-agnostic via iterate_sinks(), so this
        works whether rx_sink_bin() ends in alsasink, osxaudiosink, etc.
        _rx_stall_watchdog compares this against last_rx_packet to catch a
        silent output stall (e.g. audiomixer wedging) that RX network health
        alone wouldn't reveal."""
        for sink in pipeline.iterate_sinks():
            # The inter branch is a clean-news TX handoff, not local
            # monitoring. It must not mask a GoXLR monitor-sink stall.
            if sink.get_name() == "clean_news_inter_sink":
                continue
            if sink.get_name() == "headphone_timing_probe":
                continue
            pad = sink.get_static_pad("sink")
            if pad:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._on_rx_sink_buffer)

    def _on_rx_sink_buffer(self, pad, info):
        now = time.monotonic()
        if self._last_rx_sink_buffer_seen is not None:
            gap = now - self._last_rx_sink_buffer_seen
            if gap > _GAP_LOG_THRESHOLD_S:
                logger.warning(f"RX sink buffer gap: {gap*1000:.0f}ms since previous buffer reached hardware")
        self._last_rx_sink_buffer_seen = now
        self._last_rx_sink_buffer = now
        return Gst.PadProbeReturn.OK

    def _build_pipelines(self):
        tx_str = (
            f"{self.interface.tx_source_bin()} ! "
            f"level name=tx_meter ! "
            f"audioresample ! audio/x-raw,rate=24000,channels=2 ! "
            f"avenc_aac ! aacparse ! "
            f"audio/mpeg,mpegversion=4,stream-format=adts ! "
            f"appsink name=tx_sink emit-signals=true sync=false"
        )
        logger.info(f"TX: {tx_str}")
        self.tx_pipeline = Gst.parse_launch(tx_str)
        self.tx_pipeline.get_by_name("tx_sink").connect("new-sample", self._on_tx_sample)
        headphone_probe = self.tx_pipeline.get_by_name("headphone_timing_probe")
        if headphone_probe:
            self._headphone_timing_probe_file = open("/tmp/briclite-headphone-timing.raw", "wb")
            headphone_probe.connect("new-sample", self._on_headphone_timing_sample)
        bus = self.tx_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self.rx_pipeline, self.rx_appsrc = self._build_rx_pipeline(self.rx_channel_mode)

    def begin_local(self, event_loop):
        """Bring the local GoXLR/Behringer audio pipeline live — TX capture
        (Broadcast Mix metering included) and RX playback — independent of
        whether a network link to the remote end is connected. Call once
        right after construction (process startup, or after a full_stop()+
        rebuild) and never again on this instance.

        This exists because is_active (see start()/stop() below) now means
        only "network link active", not "local pipeline running": the local
        pipeline runs continuously so the Broadcast Mix meter reflects real
        GoXLR hardware levels regardless of connection status. TX and RX are
        two directions of the same physical USB audio device on both
        interfaces (GoXLR Mini, Behringer UCA202) — see _full_reconnect()'s
        docstring in main.py for why they must always be brought up and torn
        down together, never independently.
        """
        self.event_loop = event_loop
        self.interface.start()
        self.set_clean_news_return_level(self.interface.clean_news_return_level())
        self.set_clean_news_return_muted(self.interface.clean_news_return_muted())
        self.tx_pipeline.set_state(Gst.State.PLAYING)
        self.rx_pipeline.set_state(Gst.State.PLAYING)
        threading.Thread(target=self.glib_loop.run, daemon=True).start()

    def start(self, event_loop):
        """Bring the network link up: open the RTP socket and start
        exchanging audio with the remote end. Assumes begin_local() has
        already brought the local pipeline live on this instance."""
        self.event_loop = event_loop
        self.jitter_buf.reset()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(0.5)
        self.sock.bind(("0.0.0.0", self.rx_port))

        self.last_rx_packet = time.monotonic()
        self._last_rx_sink_buffer = time.monotonic()
        self._last_rx_sink_buffer_seen = None
        self._last_tx_sample = 0.0
        self.is_active = True

        threading.Thread(target=self._rx_loop,        daemon=True).start()
        threading.Thread(target=self._playout_loop,   daemon=True).start()

        asyncio.run_coroutine_threadsafe(
            global_state.update_metrics({"connection_status": "CONNECTED"}),
            event_loop
        )
        asyncio.run_coroutine_threadsafe(self._poll_stats_loop(), event_loop)
        asyncio.run_coroutine_threadsafe(self._rx_stall_watchdog(), event_loop)
        logger.info(
            f"Started (jitter buf {self.latency_ms} ms): "
            f"TX→{self.target_ip}:{self.tx_port}  RX←:{self.rx_port}"
        )

    def stop(self, event_loop):
        """Take the network link down: close the socket, stop RX/playout
        threads, mark DISCONNECTED. Leaves the local pipeline (TX capture/
        metering, RX playback) running — see begin_local(). Broadcast Mix
        keeps showing real levels; the two network-fed meters (Studio
        Return/News) go quiet since nothing is arriving to decode, so their
        peaks are explicitly reset here rather than left to freeze at their
        last real value.

        Use full_stop() instead when the whole PipelineController is being
        discarded (mode change, hotplug, fault recovery, process shutdown) —
        only that path may safely tear down TX/RX, and always together; see
        full_stop()'s docstring."""
        self.is_active = False
        if self._rx_rebuild_timer:
            self._rx_rebuild_timer.cancel()
            self._rx_rebuild_timer = None
        self.jitter_buf.reset()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        asyncio.run_coroutine_threadsafe(
            global_state.update_metrics({
                "connection_status": "DISCONNECTED",
                "rx_peak_l": -60.0, "rx_peak_r": -60.0,
                "jitter": 0, "lost": 0,
            }),
            event_loop
        )

    def full_stop(self, event_loop):
        """Tear down the local pipeline entirely, including TX/RX hardware
        handles — only safe when this PipelineController object is about to
        be discarded and replaced (a fresh instance takes over via its own
        begin_local()). TX and RX are two directions of the same physical
        USB audio device on both interfaces; tearing down and reopening only
        one side while the other stays open reliably wedges it (confirmed
        live on the GoXLR Mini — see _full_reconnect()'s docstring in
        main.py), so both always go down together here."""
        self.stop(event_loop)
        self.tx_pipeline.set_state(Gst.State.NULL)
        self.rx_pipeline.set_state(Gst.State.NULL)
        self.tx_pipeline.get_state(Gst.SECOND)   # block until audio devices are released
        self.rx_pipeline.get_state(Gst.SECOND)
        self.glib_loop.quit()
        self.interface.stop()
        if self._headphone_timing_probe_file:
            self._headphone_timing_probe_file.close()
            self._headphone_timing_probe_file = None
        asyncio.run_coroutine_threadsafe(
            global_state.update_metrics({"tx_peak_l": -60.0, "tx_peak_r": -60.0}),
            event_loop
        )

    def set_rx_volume(self, pct: int) -> None:
        self.rx_volume_pct = max(0, min(100, pct))
        vol = self.rx_pipeline.get_by_name("rx_vol")
        if vol:
            vol.set_property("volume", self.rx_volume_pct / 100.0)

    def set_clean_news_return_level(self, level: int) -> None:
        """Mirror GoXLR Fader C onto the PSA-side, clean news TX path."""
        gain = max(0, min(255, level)) / 255.0
        volume = self.tx_pipeline.get_by_name("clean_news_return_gain")
        if volume:
            volume.set_property("volume", 0.0 if self._clean_news_return_muted else gain)
        self._clean_news_return_level = max(0, min(255, level))

    def set_clean_news_return_muted(self, muted: bool) -> None:
        self._clean_news_return_muted = bool(muted)
        volume = self.tx_pipeline.get_by_name("clean_news_return_gain")
        if volume:
            gain = self._clean_news_return_level / 255.0
            volume.set_property("volume", 0.0 if self._clean_news_return_muted else gain)

    def set_rx_channel_mode(self, mode: str):
        # No longer gated on is_active — the RX pipeline is always running
        # (see begin_local()), so a mode change applies whether or not the
        # network link is connected.
        self.rx_channel_mode = mode
        if self._rx_rebuild_timer:
            self._rx_rebuild_timer.cancel()
        self._rx_rebuild_timer = threading.Timer(0.25, self._do_rx_rebuild)
        self._rx_rebuild_timer.start()

    def _do_rx_rebuild(self):
        # Serialised against _playout_loop's push-buffer calls and against
        # overlapping rebuild triggers (e.g. a channel-mode switch and a
        # Behringer hotplug event landing close together) — without this,
        # tearing down self.rx_pipeline/self.rx_appsrc while another thread
        # is mid-emit() or mid-rebuild on the same objects wedges GStreamer
        # and freezes the RX meter with no error ever posted to the bus.
        with self._rx_rebuild_lock:
            mode = self.rx_channel_mode
            old = self.rx_pipeline
            old.set_state(Gst.State.NULL)
            old.get_state(Gst.SECOND)          # block until audio device is released
            self.rx_pipeline, self.rx_appsrc = self._build_rx_pipeline(mode)
            self.rx_pipeline.set_state(Gst.State.PLAYING)
            # Fresh appsrc, fresh segment — see the self.rx_pts comment in
            # __init__ for why this must track the appsrc's own lifetime.
            self.rx_pts = 0

    def _on_tx_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        now = time.monotonic()
        if self._last_tx_sample:
            gap = now - self._last_tx_sample
            if gap > _GAP_LOG_THRESHOLD_S:
                logger.warning(f"TX capture gap: {gap*1000:.0f}ms since previous sample")
        self._last_tx_sample = now

        # TX capture now runs continuously for Broadcast Mix metering (see
        # begin_local()), independent of the network link — do not transmit,
        # or advance RTP sequence/timestamp state, while disconnected.
        if not self.is_active or not self.sock:
            return Gst.FlowReturn.OK

        buf  = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())

        header = struct.pack("!BBHII",
            0x80, 14,
            self.rtp_seq & 0xFFFF,
            self.rtp_ts  & 0xFFFFFFFF,
            self.rtp_ssrc
        ) + b"\x00\x00\x00\x00"   # RFC2250

        try:
            self.sock.sendto(header + data, (self.target_ip, self.tx_port))
        except Exception as e:
            logger.warning(f"TX send: {e}")

        self.rtp_seq += 1
        self.rtp_ts  = (self.rtp_ts + _TS_INC) & 0xFFFFFFFF
        return Gst.FlowReturn.OK

    def _on_headphone_timing_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if sample and self._headphone_timing_probe_file:
            buf = sample.get_buffer()
            self._headphone_timing_probe_file.write(buf.extract_dup(0, buf.get_size()))
        return Gst.FlowReturn.OK


    def _rx_loop(self):
        last_seq = None
        last_arrival = None
        while self.is_active:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                break
            recv_time = time.monotonic()

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

            if len(payload) >= 4 and payload[0] == 0x00 and payload[1] == 0x00:
                payload = payload[4:]

            if len(payload) < 2 or payload[0] != 0xFF or (payload[1] & 0xF0) != 0xF0:
                continue

            if last_arrival is not None:
                gap = recv_time - last_arrival
                if gap > _GAP_LOG_THRESHOLD_S:
                    seq_delta = (seq - last_seq) % _WRAP
                    expected_if_real_loss = round(gap * _RTP_PACKET_RATE)
                    verdict = ("local read delay — packets were queued, not lost"
                               if seq_delta <= 1 else
                               "real network gap — sequence numbers actually skipped"
                               if abs(seq_delta - expected_if_real_loss) <= max(2, expected_if_real_loss * 0.2) else
                               "unclear — delta doesn't cleanly match either case")
                    logger.warning(
                        f"RX socket gap: {gap*1000:.0f}ms since previous packet "
                        f"(seq {last_seq}→{seq}, delta {seq_delta}, "
                        f"~{expected_if_real_loss} expected if genuinely lost at "
                        f"{_RTP_PACKET_RATE:.1f} pkt/s) — {verdict}"
                    )
            last_seq = seq
            last_arrival = recv_time

            self.jitter_buf.push(seq, rtp_ts, bytes(payload))
            self.last_rx_packet = recv_time

    def _playout_loop(self):
        # Best-effort: protect this thread from being descheduled by system
        # load or GIL contention (meter/telemetry threads) for long enough to
        # starve the direct `hw:` ALSA sink. Needs CAP_SYS_NICE/RTPRIO — see
        # BUILD.md §7 for the systemd unit grant. Silently stays SCHED_OTHER
        # if not permitted; the larger buffers above are the primary defence.
        if sys.platform.startswith("linux"):
            try:
                os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(_PLAYOUT_RT_PRIORITY))
                logger.info(f"Playout thread: SCHED_FIFO priority {_PLAYOUT_RT_PRIORITY}")
            except OSError as e:
                logger.warning(f"Playout thread: could not raise scheduling priority ({e}) — see BUILD.md §7")

        FRAME_DUR = Gst.SECOND * _AAC_FRAME // _AAC_SAMP
        last_good = None

        while self.is_active:
            payload = self.jitter_buf.pop(last_good)

            if payload is None:
                time.sleep(0.010)
                continue

            if payload:
                last_good = payload

            if not payload:
                self.rx_pts += FRAME_DUR
                continue

            buf          = Gst.Buffer.new_wrapped(payload)
            buf.duration = FRAME_DUR

            # pts read/increment shares the rebuild lock with the appsrc
            # emit below: _do_rx_rebuild() resets self.rx_pts for a brand
            # new appsrc/segment under the same lock, so a mode-change
            # rebuild can never land between "read rx_pts" and "push this
            # buffer" and hand the new appsrc a stale, already-advanced pts.
            with self._rx_rebuild_lock:
                buf.pts = self.rx_pts
                self.rx_pts += FRAME_DUR
                ret = self.rx_appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                logger.warning(f"appsrc push: {ret}")

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            logger.error(f"GST ERROR: {err} | {dbg}")
            # Not gated on is_active (network-link state) any more — the
            # local pipeline now runs continuously (see begin_local()), so a
            # device-level fault must be recovered regardless of whether the
            # network link happens to be connected at the moment it occurs.
            if (self.on_pipeline_fault and not self._device_error_seen
                    and any(p in str(err).lower() for p in _DEVICE_ERROR_PATTERNS)):
                self._device_error_seen = True
                logger.warning(
                    "Device-level GST error — the underlying ALSA device likely "
                    "reset/dropped; triggering automatic reconnect"
                )
                asyncio.run_coroutine_threadsafe(self.on_pipeline_fault(), self.event_loop)
        elif t == Gst.MessageType.WARNING:
            w, dbg = message.parse_warning()
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

    def _socket_recv_queue_bytes(self) -> int:
        """Bytes currently sitting unread in the RX UDP socket's kernel
        receive buffer (FIONREAD) — read-only, doesn't consume anything.
        Diagnostic added 2026-09-10: distinguishes "packets are arriving at
        the kernel but _rx_loop's thread isn't being scheduled to read them"
        (queue depth > 0 and growing) from "nothing is actually arriving,
        the gap is upstream of this host" (queue stays at 0) — the per-packet
        gap logger in _rx_loop can't observe this because main.py's 10s
        _rx_watchdog tears the socket down (discarding anything queued on it)
        before a "next" packet ever arrives to measure the resumption
        against. Returns -1 if the ioctl fails (e.g. socket already closed)."""
        try:
            return struct.unpack("I", fcntl.ioctl(self.sock.fileno(), termios.FIONREAD, b"\0\0\0\0"))[0]
        except OSError:
            return -1

    async def _rx_stall_watchdog(self):
        """Catch the RX hardware sink going silently quiet while RX network
        packets keep arriving — a failure GStreamer doesn't post any bus
        message for (see _attach_rx_stall_probe/_on_rx_sink_buffer and
        _RX_SINK_STALL_S above). Requires RX to be recently healthy so this
        doesn't fire redundantly with main.py's _rx_watchdog, which already
        handles the network itself going quiet.

        Also logs the RX socket's kernel receive-queue depth whenever RX has
        been idle beyond _GAP_LOG_THRESHOLD_S, regardless of which watchdog
        ends up handling it — see _socket_recv_queue_bytes."""
        while self.is_active:
            await asyncio.sleep(_STALL_CHECK_INTERVAL_S)
            if not self.is_active:
                break
            now = time.monotonic()
            rx_idle_s = now - self.last_rx_packet
            if rx_idle_s > _GAP_LOG_THRESHOLD_S:
                queued = self._socket_recv_queue_bytes()
                verdict = ("data IS waiting in the kernel buffer — _rx_loop's thread "
                           "isn't reading it (local stall)" if queued > 0 else
                           "nothing queued — genuinely no packets have arrived at this host")
                logger.warning(
                    f"RX socket idle {rx_idle_s*1000:.0f}ms, {queued} bytes queued unread — {verdict}"
                )
            rx_network_recent = rx_idle_s < _RX_SINK_STALL_S
            sink_stalled = (now - self._last_rx_sink_buffer) > _RX_SINK_STALL_S
            if (rx_network_recent and sink_stalled
                    and self.on_pipeline_fault and not self._stall_reported):
                self._stall_reported = True
                logger.warning(
                    f"RX sink produced no output for over {_RX_SINK_STALL_S}s while RX "
                    f"packets keep arriving — local playout stall (e.g. audiomixer wedged, "
                    f"see TROUBLESHOOTING.md 'PFL stutters and goes silent'); "
                    f"triggering automatic reconnect"
                )
                asyncio.run_coroutine_threadsafe(self.on_pipeline_fault(), self.event_loop)

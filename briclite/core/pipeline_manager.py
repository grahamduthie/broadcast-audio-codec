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
_TS_INC    = _AAC_FRAME * _RTP_CLOCK // _AAC_SAMP   # 3840


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
        tx_str = (
            f"alsasrc device={self.alsa_dev} ! audioconvert ! "
            f"audio/x-raw,rate=44100,channels=2 ! "
            f"level name=tx_meter ! "
            f"audioresample ! audio/x-raw,rate=24000,channels=2 ! "
            f"avenc_aac ! aacparse ! "
            f"audio/mpeg,mpegversion=4,stream-format=adts ! "
            f"appsink name=tx_sink emit-signals=true sync=false"
        )
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

    def _on_tx_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if not sample:
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

    def _rx_loop(self):
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

            if len(payload) >= 4 and payload[0] == 0x00 and payload[1] == 0x00:
                payload = payload[4:]

            if len(payload) < 2 or payload[0] != 0xFF or (payload[1] & 0xF0) != 0xF0:
                continue

            self.jitter_buf.push(seq, rtp_ts, bytes(payload))

    def _playout_loop(self):
        FRAME_DUR = Gst.SECOND * _AAC_FRAME // _AAC_SAMP
        rx_pts    = 0
        last_good = None

        while self.is_active:
            payload = self.jitter_buf.pop(last_good)

            if payload is None:
                time.sleep(0.010)
                continue

            if payload:
                last_good = payload

            if not payload:
                rx_pts += FRAME_DUR
                continue

            buf          = Gst.Buffer.new_wrapped(payload)
            buf.pts      = rx_pts
            buf.duration = FRAME_DUR
            rx_pts      += FRAME_DUR

            ret = self.rx_appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                logger.warning(f"appsrc push: {ret}")

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            logger.error(f"GST ERROR: {err} | {dbg}")
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

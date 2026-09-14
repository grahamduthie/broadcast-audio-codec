import asyncio
import os
import sys
import socket
import struct
import json
import logging
import time
from typing import Callable, Optional

import websockets

from .base import ALSA_BUFFER_TIME_US, ALSA_LATENCY_TIME_US, AudioInterface

_IS_MACOS = sys.platform == "darwin"

logger = logging.getLogger("goxlr")
ipc_logger = logging.getLogger("goxlr.ipc")

_SOCKET_PATH = "/tmp/goxlr.socket"
_WEBSOCKET_URL = "ws://localhost:14564/api/websocket"
_ALSA_CONFIG_PATH = os.path.expanduser("~/.asoundrc")

_PFL_COLOUR      = "FF8800"  # orange — studio return PFL active
_NORMAL_COLOUR   = "00FFFF"  # cyan   — normal
_PICKUP_COLOUR   = "FFB000"  # amber — logical level restored; physical position unverified
_GRADIENT_TOP_COLOUR = "FF0000"  # red — fixed top-of-strip anchor for the low/high gradient
_MONITOR_CUT_CUTTING_COLOUR = "FF0000"  # red   — Studio Monitor Cut armed and actively muting Line Out
_MUTE_ENGAGED_COLOUR = "FF0000"  # red   — channel mute button pressed, full brightness regardless of fader
_MUTE_OPEN_COLOUR    = "00FF00"  # green — unmuted, fader open past the Line Out mute threshold, full brightness
# A fader resting at the bottom of its travel can still read a small nonzero
# volume (observed: 1/255) rather than a clean 0. Treat anything at or below
# this as closed so that noise doesn't falsely trigger a Line Out cut.
_MIC_OPEN_THRESHOLD = 5
_BEHRINGER_DEVICE = "hw:CODEC,0"
_MONITOR_OUTPUTS = ("Headphones", "LineOut")

# Software trim (rescaling SetVolume against the raw fader reading) was
# tried for LineIn/Game/Console and reverted 2026-09-13: on this hardware the
# fader's own LEDs are driven by that same SetVolume value, so trimming it
# made the physical fader's lights stop matching its physical position
# (e.g. pushed fully up but showing ~80% lit) — confirmed live and judged
# not worth it. Only Mic keeps a trim control, using the real hardware
# preamp gain (SetMicrophoneGain) instead, which has no such side effect.
_MIC_GAIN_MIN = 0
_MIC_GAIN_MAX = 72  # goxlr-client: "recommended to be lower than 72dB"

# audiomixer's automatic latency query fails on this pipeline ("Latency query
# failed" — the mix of a manually-fed appsrc branch and a live alsasrc branch
# never negotiates a value), leaving it to assume 0 additional latency. Giving
# it an explicit budget matching the jitter buffer's 200ms lets both branches'
# buffers actually line up instead of the aggregator starving/stalling.
_GOXLR_MIX_PROPS = "ignore-inactive-pads=true min-upstream-latency=200000000 latency=200000000"

# The Game path normally has this much ALSA buffering before it is visible in
# the GoXLR capture/BroadcastMix.  The clean-news branch bypasses that path,
# so it must be delayed by the same order of magnitude before it is mixed with
# the hardware capture.  This is deliberately configurable: the exact value
# is a property of the live ALSA/USB path and must be confirmed by a tone test.
_DEFAULT_CLEAN_NEWS_ALIGNMENT_DELAY_MS = 200
_CLEAN_NEWS_INTER_CHANNEL = "briclite_clean_news"

# Maps fader letters to GoXLR routing source names
# D moved from LineIn to Console (optical input) 2026-09-13: music now
# arrives digitally from the PC over optical rather than analogue 3.5mm.
# B moved from Chat (Behringer USB capture) to LineIn (Behringer analogue
# output) the same day: the software USB path was adding latency and was a
# suspected glitch source, and the freed Line In jack now carries it instead.
_FADER_TO_SOURCE = {
    "A": "Microphone",
    "B": "LineIn",     # Guest mic (Behringer analogue out -> GoXLR line in)
    "C": "Game",       # News / Codec RX Right
    "D": "Console",    # Music Player / GoXLR optical input
}

_FADERS = [
    ("A", "Mic"),      # Main microphone (XLR)
    ("B", "LineIn"),   # Guest microphone (Behringer analogue output)
    ("C", "Game"),     # News feed (Codec RX Right)
    ("D", "Console"),  # Music Player (GoXLR optical input)
]
_FADER_TO_CHANNEL = dict(_FADERS)

# Each fader's own hardware mute button (distinct from Bleep/Cough).
_FADER_MUTE_BUTTONS = {"A": "Fader1Mute", "B": "Fader2Mute", "C": "Fader3Mute", "D": "Fader4Mute"}

# Full routing matrix applied on every start.
# Music carries the studio return (codec RX Left) but has no fader — only
# accessible via Bleep PFL. LineIn is now the second mic (Behringer,
# analogue) and goes to BroadcastMix so the operator can fade it in.
_ROUTING = {
    "Microphone": {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "LineIn":     {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    # Game is deliberately excluded from BroadcastMix.  RX Right/news still
    # reaches the local monitors through Fader C, while its clean pre-GoXLR
    # copy is added to codec TX in PipelineController.
    "Game":       {"Headphones": True,  "BroadcastMix": False, "Sampler": False, "LineOut": True,  "StreamMix2": False},
    # Chat is unused since Fader B moved to LineIn (analogue) 2026-09-13; left
    # inert (no fader controls it). If the Behringer's USB is still plugged
    # into the host, extra_rx_source_bins()/rx_sink_bin() (see behringer_available())
    # still captures it into this bus via a running audiomixer branch, but
    # it goes nowhere audible/broadcast since every output here is False.
    "Chat":       {"Headphones": False, "BroadcastMix": False, "Sampler": False, "LineOut": False, "StreamMix2": False},
    "Music":      {"Headphones": False, "BroadcastMix": False,  "Sampler": False, "LineOut": False, "StreamMix2": False},
    # Console (optical input): music player, Fader D. Carries the routing
    # LineIn used to have.
    "Console":    {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "System":     {"Headphones": False, "BroadcastMix": False,  "Sampler": False, "LineOut": False, "StreamMix2": False},
    "Samples":    {"Headphones": False, "BroadcastMix": False,  "Sampler": False, "LineOut": False, "StreamMix2": False},
}

# ALSA virtual device written to ~/.asoundrc on start.
# Extracts the Broadcast Mix (channels 0-1) from the 21-channel USB capture stream.
_ALSA_CONFIG = """\
# Written by briclite GoXLRInterface — do not edit manually

pcm.goxlr_broadcast {
    type route
    slave {
        pcm "hw:GoXLRMini,0"
        channels 21
    }
    ttable {
        0.0 1.0
        1.1 1.0
    }
}
"""

# 2→10 matrix: codec RX → GoXLR playback channels.
# Rows = GoXLR output channel, Cols = codec RX input (L=0, R=1).
#   ch 0-1: System  — silent
#   ch 2-3: Game    — RX Right → Fader C → Broadcast Mix
#   ch 4-5: Chat    — silent (Behringer second mic arrives via extra_rx_source_bins)
#   ch 6-7: Music   — RX Left (studio return, no fader — PFL-only via Bleep)
#   ch 8-9: Sample  — silent
_RX_MATRIX = (
    "<<0.0,0.0>,<0.0,0.0>,"
    "<0.0,1.0>,<0.0,1.0>,"
    "<0.0,0.0>,<0.0,0.0>,"
    "<1.0,0.0>,<1.0,0.0>,"
    "<0.0,0.0>,<0.0,0.0>>"
)

# RX Right/news as dual mono, matching the existing Game mapping above.  This
# is tapped immediately after AAC decode, before the GoXLR USB playback path.
_CLEAN_NEWS_MATRIX = "<<0.0,1.0>,<0.0,1.0>>"

# 2→10 matrix: Behringer stereo capture → GoXLR Chat (ch 4-5) only.
# Rows = GoXLR output channel, Cols = Behringer input (L=0, R=1).
_BEHRINGER_MATRIX = (
    "<<0.0,0.0>,<0.0,0.0>,"
    "<0.0,0.0>,<0.0,0.0>,"
    "<1.0,0.0>,<0.0,1.0>,"
    "<0.0,0.0>,<0.0,0.0>,"
    "<0.0,0.0>,<0.0,0.0>>"
)


def _broadcast_extract_matrix(n_in: int) -> str:
    """Matrix that extracts channels 0 and 1 (BroadcastMix) from an n_in-channel stream."""
    row0 = ["1.0" if i == 0 else "0.0" for i in range(n_in)]
    row1 = ["1.0" if i == 1 else "0.0" for i in range(n_in)]
    return f"<<{','.join(row0)}>,<{','.join(row1)}>>"


def _capture_pair_extract_matrix(n_in: int, left: int, right: int) -> str:
    """Matrix selecting one stereo pair from GoXLR's multichannel capture."""
    row0 = ["1.0" if i == left else "0.0" for i in range(n_in)]
    row1 = ["1.0" if i == right else "0.0" for i in range(n_in)]
    return f"<<{','.join(row0)}>,<{','.join(row1)}>>"


class GoXLRInterface(AudioInterface):

    def __init__(self, config: dict, studio_pfl: bool = False,
                 studio_saved_volume: Optional[int] = None,
                 on_pfl_changed: Optional[Callable[[bool, Optional[int]], None]] = None,
                 restored_fader_volumes: Optional[dict[str, int]] = None,
                 on_fader_volume_changed: Optional[Callable[[str, int], None]] = None,
                 on_fader_mute_changed: Optional[Callable[[str, bool], None]] = None,
                 on_volume_changed: Optional[Callable[[str, int], None]] = None,
                 monitor_cut_enabled: bool = False,
                 on_monitor_cut_changed: Optional[Callable[[bool], None]] = None,
                 restored_mic_gain: Optional[int] = None,
                 on_mic_gain_changed: Optional[Callable[[int], None]] = None,
                 restored_studio_return_level: Optional[int] = None,
                 on_studio_return_level_changed: Optional[Callable[[int], None]] = None):
        self._serial: str | None = None
        self._studio_pfl = studio_pfl
        self._studio_saved_volume = studio_saved_volume if studio_pfl else None
        self._on_pfl_changed = on_pfl_changed
        valid_channels = {channel for _, channel in _FADERS}
        supplied_volumes = restored_fader_volumes if isinstance(restored_fader_volumes, dict) else {}
        self._restored_fader_volumes = {
            channel: level
            for channel, level in supplied_volumes.items()
            if channel in valid_channels and isinstance(level, int) and 0 <= level <= 255
        }
        # Studio Monitor Cut: while armed, Line Out is silenced whenever
        # either microphone (Mic/A or LineIn/B) fader is open, to protect
        # against feedback when speakers are near the mics. Tracks the
        # channel names actually assigned to those two faders, so this must
        # be kept in sync with _FADER_TO_SOURCE above.
        self._monitor_cut_enabled = monitor_cut_enabled
        self._on_monitor_cut_changed = on_monitor_cut_changed
        # Tracks all four faders' current volumes, not just the two mics:
        # also drives the per-channel mute button's open/closed LED colour.
        self._fader_volumes = {
            channel: self._restored_fader_volumes.get(channel, 0)
            for _, channel in _FADERS
        }
        self._fader_muted = {fader: False for fader, _ in _FADERS}
        # The GoXLR has non-motorised faders. Amber identifies controls whose
        # restored logical value may still need physical soft pickup.
        self._pending_pickup_faders = {
            fader for fader, channel in _FADERS if channel in self._restored_fader_volumes
        }
        self._pickup_ignore_until = 0.0
        self._on_fader_volume_changed = on_fader_volume_changed
        self._on_fader_mute_changed = on_fader_mute_changed
        self._on_volume_changed = on_volume_changed
        self._monitor_routing: dict[tuple[str, str], bool] = {}
        goxlr_cfg = config.get("goxlr", {})
        clean_cfg = goxlr_cfg.get("clean_news_return", {})
        if not isinstance(clean_cfg, dict):
            clean_cfg = {}
        # Enable by default on the real Linux GoXLR.  The macOS two-channel
        # virtual-device trial remains unchanged unless explicitly enabled.
        self._clean_news_return = bool(clean_cfg.get("enabled", not _IS_MACOS))
        delay_ms = clean_cfg.get("alignment_delay_ms", _DEFAULT_CLEAN_NEWS_ALIGNMENT_DELAY_MS)
        self._clean_news_delay_ms = max(0, min(2000, delay_ms if isinstance(delay_ms, int) else _DEFAULT_CLEAN_NEWS_ALIGNMENT_DELAY_MS))
        self._game_level = self._restored_fader_volumes.get("Game", 255)
        self._timing_probe = bool(clean_cfg.get("timing_probe", False))

        self._mic_type: str = "Dynamic"
        self._mic_gain: Optional[int] = (
            max(_MIC_GAIN_MIN, min(_MIC_GAIN_MAX, restored_mic_gain))
            if isinstance(restored_mic_gain, int) else None
        )
        self._on_mic_gain_changed = on_mic_gain_changed

        self._studio_return_level: Optional[int] = (
            max(0, min(255, restored_studio_return_level))
            if isinstance(restored_studio_return_level, int) else None
        )
        self._on_studio_return_level_changed = on_studio_return_level_changed
        if _IS_MACOS:
            self._mac_tx_device: str = goxlr_cfg.get("mac_tx_device", "")
            self._mac_rx_device: str = goxlr_cfg.get("mac_rx_device", "")
            self._mac_behringer_device: str = goxlr_cfg.get("mac_behringer_device", "")
            self._capture_channels: int = goxlr_cfg.get("capture_channels", 21)

    @staticmethod
    def is_available() -> bool:
        """Return True if the daemon is running and has a GoXLR Mini connected."""
        if not os.path.exists(_SOCKET_PATH):
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(_SOCKET_PATH)
            resp = GoXLRInterface._query(sock, {"GetStatus": None})
            sock.close()
            return bool(resp.get("Status", {}).get("mixers"))
        except Exception:
            return False

    @staticmethod
    def behringer_available() -> bool:
        """Return True if the Behringer (second mic) ALSA device is currently present.

        The Behringer is an optional second source mixed into the GoXLR RX
        pipeline (extra_rx_source_bins). Unlike the GoXLR itself, its absence
        must not take the whole RX pipeline down.

        As of 2026-09-13, Fader B (the guest mic) is fed by GoXLR LineIn
        (the Behringer's analogue output) rather than this USB path, so this
        capture branch feeds the now-inert Chat bus and is currently unused
        in practice. Kept deliberately, not dead code: retained in case the
        USB path is needed again. See CURRENT-STATUS.md and
        GOXLR-MINI-LINUX.md ("Fader B moved from Chat to LineIn").
        """
        try:
            with open("/proc/asound/cards") as f:
                return "CODEC" in f.read()
        except OSError:
            return False

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("GoXLR IPC socket closed unexpectedly")
            buf += chunk
        return buf

    @staticmethod
    def _query(sock: socket.socket, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        sock.sendall(struct.pack(">I", len(data)) + data)
        length = struct.unpack(">I", GoXLRInterface._recv_exact(sock, 4))[0]
        return json.loads(GoXLRInterface._recv_exact(sock, length))

    def _ipc(self, payload: dict) -> object:
        label = next(iter(payload))
        if label == "Command" and isinstance(payload["Command"], list) and len(payload["Command"]) == 2:
            label = f"Command:{next(iter(payload['Command'][1]))}"
        t0 = time.monotonic()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(_SOCKET_PATH)
        result = self._query(sock, payload)
        sock.close()
        dt_ms = (time.monotonic() - t0) * 1000
        # Diagnostic instrumentation added 2026-09-10 to chase "PFL stutters
        # and goes silent" (TROUBLESHOOTING.md) — Bleep triggers a burst of
        # these over the *same* USB device that's carrying the live audio
        # streams; logging every call's duration lets us correlate a slow
        # one against exactly when RX/TX gaps start in pipeline_manager.py's
        # matching instrumentation.
        ipc_logger.info(f"{label} took {dt_ms:.0f}ms")
        return result

    def _cmd(self, command: dict) -> None:
        if self._serial is None:
            return
        result = self._ipc({"Command": [self._serial, command]})
        if result != "Ok":
            logger.warning(f"GoXLR command {command} returned: {result}")

    def tx_source_bin(self) -> str:
        if _IS_MACOS:
            if not self._mac_tx_device:
                # No device configured — use default system input
                return "osxaudiosrc ! audioconvert ! audio/x-raw,rate=48000,channels=2"
            n = self._capture_channels
            if n <= 2:
                # Stereo device (e.g. VB-Cable) — no channel extraction needed
                return (
                    f'osxaudiosrc unique-id="{self._mac_tx_device}" ! '
                    f'audioconvert ! audio/x-raw,rate=48000,channels=2'
                )
            matrix = _broadcast_extract_matrix(n)
            return (
                f'osxaudiosrc unique-id="{self._mac_tx_device}" ! '
                f'audio/x-raw,channels={n},layout=interleaved ! '
                f'audiomixmatrix in-channels={n} out-channels=2 matrix="{matrix}" ! '
                f'audioconvert ! audio/x-raw,rate=48000,channels=2'
            )
        if not self._clean_news_return:
            return (
                "alsasrc device=goxlr_broadcast ! audioconvert ! "
                "audio/x-raw,rate=48000,channels=2"
            )
        delay_ns = self._clean_news_delay_ms * 1_000_000
        if self._timing_probe:
            # Test-only: use a single 21-channel GoXLR capture handle for both
            # normal TX base audio and the headphone pair (10/11, confirmed by
            # the duplex Game-tone capture). A second ALSA capture handle is
            # not possible while codec TX is active.
            broadcast = _broadcast_extract_matrix(21)
            headphones = _capture_pair_extract_matrix(21, 10, 11)
            hardware_branch = (
                "alsasrc device=hw:GoXLRMini,0 ! "
                "audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=21 ! "
                "tee name=goxlr_capture "
                f'goxlr_capture. ! queue ! audiomixmatrix in-channels=21 out-channels=2 matrix="{broadcast}" ! '
                "audioconvert ! audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=2 ! tx_program_mix. "
                f'goxlr_capture. ! queue leaky=downstream max-size-time=1000000000 ! audiomixmatrix in-channels=21 out-channels=2 matrix="{headphones}" ! '
                "audioconvert ! audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=2 ! "
                "appsink name=headphone_timing_probe emit-signals=true sync=false max-buffers=32 drop=true "
            )
        else:
            hardware_branch = (
                "alsasrc device=goxlr_broadcast ! audioconvert ! audioresample ! "
                "audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=2 ! tx_program_mix. "
            )
        # interaudiosrc presents a timestamped live source to TX. Unlike the
        # failed direct appsrc bridge, it owns the cross-pipeline buffering and
        # latency reporting required by audiomixer's aggregator.
        return (
            f"{hardware_branch}"
            f"interaudiosrc channel={_CLEAN_NEWS_INTER_CHANNEL} latency-time=200000000 buffer-time=1000000000 ! "
            "audioconvert ! audioresample ! "
            "audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=2 ! "
            f"identity name=clean_news_delay ts-offset={delay_ns} ! "
            "volume name=clean_news_return_gain volume=1.0 ! audioconvert ! "
            "audio/x-raw,format=S32LE,layout=interleaved,rate=48000,channels=2 ! tx_program_mix. "
            f"audiomixer name=tx_program_mix {_GOXLR_MIX_PROPS}"
        )

    def rx_sample_rate(self) -> int:
        return 48000

    def rx_sink_bin(self) -> str:
        if _IS_MACOS:
            # Simple stereo output when no GoXLR (capture_channels <= 2)
            if not self._mac_rx_device or self._capture_channels <= 2:
                uid = f'unique-id="{self._mac_rx_device}" ' if self._mac_rx_device else ''
                return (
                    f'audioconvert ! '
                    f'osxaudiosink {uid}sync=false '
                    f'buffer-time={ALSA_BUFFER_TIME_US} latency-time={ALSA_LATENCY_TIME_US}'
                )
            # GoXLR: route through 10-channel playback matrix.
            # Only insert audiomixer when there's a second source to combine —
            # the aggregator base class has proven unreliable here even with a
            # single pad connected, so it must not sit in the common no-extra
            # -source path (see the ALSA/no-macOS branch below for details).
            mix = f'audiomixer name=goxlr_mix {_GOXLR_MIX_PROPS} ! ' if self._mac_behringer_device else ''
            return (
                f'audiomixmatrix in-channels=2 out-channels=10 matrix="{_RX_MATRIX}" ! '
                f'{mix}'
                f'audioconvert ! audio/x-raw,format=S32LE ! '
                f'osxaudiosink unique-id="{self._mac_rx_device}" sync=false '
                f'buffer-time={ALSA_BUFFER_TIME_US} latency-time={ALSA_LATENCY_TIME_US}'
            )
        # Only insert audiomixer when the Behringer is actually present and will
        # be combined in via extra_rx_source_bins(). The audiomixer/aggregator
        # element has proven unreliable in this pipeline even with a single pad
        # connected (intermittently stops producing output while everything
        # upstream — decode, meter — keeps working, with no error posted to the
        # bus), so it must be kept out of the common single-source path
        # entirely rather than relied on to gracefully pass through one pad.
        mix = f'audiomixer name=goxlr_mix {_GOXLR_MIX_PROPS} ! ' if GoXLRInterface.behringer_available() else ''
        return (
            f'audiomixmatrix in-channels=2 out-channels=10 matrix="{_RX_MATRIX}" ! '
            f'{mix}'
            f'audioconvert ! audio/x-raw,format=S32LE ! '
            f'alsasink device=hw:GoXLRMini,0 sync=false '
            f'buffer-time={ALSA_BUFFER_TIME_US} latency-time={ALSA_LATENCY_TIME_US}'
        )

    def extra_rx_source_bins(self) -> list[str]:
        if _IS_MACOS:
            if not self._mac_behringer_device:
                return []
            return [
                f'osxaudiosrc unique-id="{self._mac_behringer_device}" ! audioconvert ! '
                f'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
                f'audiomixmatrix in-channels=2 out-channels=10 matrix="{_BEHRINGER_MATRIX}" ! '
                f'goxlr_mix.'
            ]
        if not GoXLRInterface.behringer_available():
            return []
        return [
            f'alsasrc device={_BEHRINGER_DEVICE} ! audioconvert ! '
            f'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
            f'audiomixmatrix in-channels=2 out-channels=10 matrix="{_BEHRINGER_MATRIX}" ! '
            f'goxlr_mix.'
        ]

    def clean_news_return_enabled(self) -> bool:
        return self._clean_news_return

    def clean_news_sink_bin(self) -> str:
        """Timed in-process handoff for clean RX Right, before GoXLR playback."""
        return (
            f"interaudiosink name=clean_news_inter_sink channel={_CLEAN_NEWS_INTER_CHANNEL} sync=false"
        )

    def clean_news_return_level(self) -> int:
        return self._game_level

    def clean_news_return_muted(self) -> bool:
        return self._fader_muted["C"]

    def start(self) -> None:
        # Reset the "last pushed to hardware" routing cache so it gets fully
        # reapplied below — but NOT self._studio_pfl / self._studio_saved_volume.
        # Those must survive a reconnect: a mode switch, Behringer hotplug, or
        # the RX watchdog's auto-reconnect tears down and rebuilds the
        # GStreamer pipeline on this *same* GoXLRInterface instance, not the
        # physical GoXLR device, so an operator's studio-return PFL must not
        # be silently dropped underneath them. A genuinely fresh instance
        # (see __init__, used on process boot and GoXLR hotplug) still starts
        # with PFL off, which is correct there.
        monitor_sources = [_FADER_TO_SOURCE[f] for f, _ in _FADERS] + ["Music"]
        self._monitor_routing = {
            (source, output): _ROUTING[source][output]
            for source in monitor_sources
            for output in _MONITOR_OUTPUTS
        }
        if not _IS_MACOS:
            self._write_alsa_config()
        if not GoXLRInterface.is_available():
            logger.warning("GoXLR Mini not detected — running without hardware control")
            self._serial = None
            return
        status = self._ipc({"GetStatus": None})
        self._serial = next(iter(status["Status"]["mixers"]))
        mixer = next(iter(status["Status"]["mixers"].values()))
        levels = mixer.get("levels", {}).get("volumes", {})
        if isinstance(levels.get("Game"), int):
            self._game_level = levels["Game"]
        for _, channel in _FADERS:
            if isinstance(levels.get(channel), int):
                self._fader_volumes[channel] = levels[channel]
        fader_status_all = mixer.get("fader_status", {})
        for fader in _FADER_MUTE_BUTTONS:
            mute_state = fader_status_all.get(fader, {}).get("mute_state")
            self._fader_muted[fader] = isinstance(mute_state, str) and mute_state != "Unmuted"
        mic_status = mixer.get("mic_status", {})
        if isinstance(mic_status.get("mic_type"), str):
            self._mic_type = mic_status["mic_type"]
        if self._mic_gain is None:
            gains = mic_status.get("mic_gains", {})
            if isinstance(gains.get(self._mic_type), int):
                self._mic_gain = gains[self._mic_type]
        if self._studio_return_level is None and isinstance(levels.get("Music"), int):
            self._studio_return_level = levels["Music"]
        logger.info(f"GoXLR Mini ready: serial={self._serial}")
        self._apply_faders()
        self._apply_fader_volumes()
        if self._mic_gain is not None:
            self._cmd({"SetMicrophoneGain": [self._mic_type, self._mic_gain]})
        if self._studio_return_level is not None and not self._studio_pfl:
            self._cmd({"SetVolume": ["Music", self._studio_return_level]})
        self._apply_routing()
        self._apply_mute_functions()
        self._apply_colours()
        # Reconcile Headphones/Line Out against both PFL and Studio Monitor
        # Cut — needed even when neither is active, so the freshly-reset
        # _monitor_routing cache above matches what was actually just sent.
        self._apply_monitor_routing()
        if self._studio_pfl:
            if self._studio_saved_volume is None:
                self._apply_studio_pfl_volume()
            else:
                # Already mid-PFL from before this reconnect — the hardware
                # itself was never touched, so just re-force the volume
                # rather than re-deriving _studio_saved_volume from current
                # status (which would read back 255 and clobber the real
                # pre-PFL value we're holding onto).
                self._cmd({"SetVolume": ["Music", 255]})
        self._set_bleep_colour()
        self._set_cough_colour()
        # SetVolume is echoed through the daemon WebSocket. Do not mistake
        # our restoration commands for a physical slider pickup.
        self._pickup_ignore_until = time.monotonic() + 4.0

    def stop(self) -> None:
        pass

    def _write_alsa_config(self) -> None:
        with open(_ALSA_CONFIG_PATH, "w") as f:
            f.write(_ALSA_CONFIG)

    def _apply_faders(self) -> None:
        for fader, channel in _FADERS:
            self._cmd({"SetFader": [fader, channel]})

    def _apply_fader_volumes(self) -> None:
        """Restore logical levels; GoXLR soft pickup protects against jumps."""
        for _, channel in _FADERS:
            if channel in self._restored_fader_volumes:
                self._cmd({"SetVolume": [channel, self._restored_fader_volumes[channel]]})
        if "Game" in self._restored_fader_volumes:
            self._game_level = self._restored_fader_volumes["Game"]

    def _apply_routing(self) -> None:
        for source, outputs in _ROUTING.items():
            for output, enabled in outputs.items():
                # The opt-out remains a genuine rollback switch: when the
                # PSA-side clean branch is disabled, restore the historical
                # Game-to-BroadcastMix routing rather than silently losing
                # news from TX.
                if source == "Game" and output == "BroadcastMix" and not self._clean_news_return:
                    enabled = True
                self._cmd({"SetRouter": [source, output, enabled]})

    def _apply_mute_functions(self) -> None:
        for fader, _ in _FADERS:
            self._cmd({"SetFaderMuteFunction": [fader, "All"]})
        # Cough is repurposed as the Studio Monitor Cut toggle (see
        # monitor_pfl()/_handle_ws_message()). Retargeting its own mute to
        # the unused ToStream2 bus means a press (or a hold under the
        # firmware's hold threshold) doesn't also audibly mute the mic; a
        # genuine hold still forces a real mute to All regardless of this
        # setting — a GoXLR firmware behaviour, not something we control.
        self._cmd({"SetCoughMuteFunction": "ToStream2"})

    def _apply_colours(self) -> None:
        for fader, _ in _FADERS:
            self._cmd({"SetFaderDisplayStyle": [fader, "Gradient"]})
            self._set_fader_colour(fader)
            self._set_mute_button_colour(fader)
        # Bleep and Cough are two-colour status buttons. Their native "off"
        # (Unmuted) appearance defaults to SetButtonOffStyle "Dimmed", which
        # dims colour_one — and since neither button is ever actually put
        # into a "Muted" state by this code, that left our status colour
        # permanently dim regardless of which colour we sent. "Colour2"
        # instead shows colour_two at full brightness in that state, so
        # _set_bleep_colour()/_set_cough_colour() put the current status
        # colour in both slots.
        self._cmd({"SetButtonOffStyle": ["Bleep", "Colour2"]})
        self._cmd({"SetButtonOffStyle": ["Cough", "Colour2"]})

    def _set_fader_colour(self, fader: str) -> None:
        colour = _PICKUP_COLOUR if fader in self._pending_pickup_faders else _NORMAL_COLOUR
        # SetFaderColours is [fader, top, bottom]. A black top made that end
        # look dim relative to the accent-coloured bottom, even though the
        # blue(low)/red(high) hue order was correct; using a real red for
        # the top keeps both ends equally bright.
        self._cmd({"SetFaderColours": [fader, _GRADIENT_TOP_COLOUR, colour]})

    def _mute_open(self, channel: str) -> bool:
        return self._fader_volumes.get(channel, 0) > _MIC_OPEN_THRESHOLD

    def _any_mic_open(self) -> bool:
        return any(self._mute_open(channel) for channel in ("Mic", "LineIn"))

    def _set_mute_button_colour(self, fader: str) -> None:
        """Channel mute button: red/full when muted (regardless of fader
        position); otherwise green/full when the fader is open past the
        same threshold used for the Line Out mute cut, or blue/dim
        (matching the fader's own resting colour) when closed. Full
        brightness in the button's native "off" (Unmuted) state requires
        SetButtonOffStyle "Colour2" — see SetButtonOffStyle comment in
        _apply_colours — so that must be resent alongside the colours
        whenever the open/closed state changes, not just once at start."""
        button = _FADER_MUTE_BUTTONS[fader]
        if self._fader_muted.get(fader, False):
            colour, off_style = _MUTE_ENGAGED_COLOUR, "Colour2"
        elif self._mute_open(_FADER_TO_CHANNEL[fader]):
            colour, off_style = _MUTE_OPEN_COLOUR, "Colour2"
        else:
            colour, off_style = _NORMAL_COLOUR, "Dimmed"
        self._cmd({"SetButtonColours": [button, colour, colour]})
        self._cmd({"SetButtonOffStyle": [button, off_style]})

    def _monitor_cut_active(self) -> bool:
        return self._monitor_cut_enabled and self._any_mic_open()

    def _apply_monitor_routing(self) -> None:
        """Reconcile Headphones/Line Out routing against PFL and Studio
        Monitor Cut together, since both can affect Line Out at once.
        PFL solos Music into both outputs, in place of the normal fader
        mix. Studio Monitor Cut then additionally removes everything
        (including a soloed Music) from Line Out only, never Headphones,
        whenever it's armed and a mic fader is open — this always wins
        over PFL on Line Out, since feedback safety matters more than a
        pre-fade cue being audible on the room speakers.
        Only sends SetRouter for crosspoints whose state has actually
        changed."""
        cutting = self._monitor_cut_active()
        desired: dict[tuple[str, str], bool] = {}
        for fader, _ in _FADERS:
            source = _FADER_TO_SOURCE[fader]
            desired[(source, "Headphones")] = not self._studio_pfl
            desired[(source, "LineOut")] = (not self._studio_pfl) and not cutting
        desired[("Music", "Headphones")] = self._studio_pfl
        desired[("Music", "LineOut")] = self._studio_pfl and not cutting
        for key, enabled in desired.items():
            if self._monitor_routing.get(key) != enabled:
                self._cmd({"SetRouter": [key[0], key[1], enabled]})
                self._monitor_routing[key] = enabled

    def _apply_studio_pfl_volume(self) -> None:
        """Override Music volume to 255 when PFL active (true pre-fade listen);
        restore the saved volume when PFL is released."""
        if self._studio_pfl:
            status = self._ipc({"GetStatus": None})
            mixer = next(iter(status["Status"]["mixers"].values()))
            self._studio_saved_volume = mixer["levels"]["volumes"]["Music"]
            self._cmd({"SetVolume": ["Music", 255]})
        else:
            if self._studio_saved_volume is not None:
                self._cmd({"SetVolume": ["Music", self._studio_saved_volume]})
                self._studio_saved_volume = None

    def _set_bleep_colour(self) -> None:
        colour = _PFL_COLOUR if self._studio_pfl else _NORMAL_COLOUR
        self._cmd({"SetButtonColours": ["Bleep", colour, colour]})

    def _set_cough_colour(self) -> None:
        if not self._monitor_cut_enabled:
            colour = _NORMAL_COLOUR
        elif self._monitor_cut_active():
            colour = _MONITOR_CUT_CUTTING_COLOUR
        else:
            # Armed but not yet cutting: same orange as Bleep shows for an
            # engaged studio return PFL, rather than a colour of its own.
            colour = _PFL_COLOUR
        self._cmd({"SetButtonColours": ["Cough", colour, colour]})

    def get_headphone_volume(self) -> int:
        if not GoXLRInterface.is_available():
            return 128
        status = self._ipc({"GetStatus": None})
        mixers = status.get("Status", {}).get("mixers", {})
        if not mixers:
            return 128
        mixer = next(iter(mixers.values()))
        return mixer["levels"]["volumes"]["Headphones"]

    def set_headphone_volume(self, level: int) -> None:
        if self._serial is None:
            return
        self._cmd({"SetVolume": ["Headphones", max(0, min(255, level))]})

    def get_line_out_volume(self) -> int:
        if not GoXLRInterface.is_available():
            return 255
        status = self._ipc({"GetStatus": None})
        mixers = status.get("Status", {}).get("mixers", {})
        if not mixers:
            return 255
        mixer = next(iter(mixers.values()))
        return mixer["levels"]["volumes"]["LineOut"]

    def set_line_out_volume(self, level: int) -> None:
        if self._serial is None:
            return
        self._cmd({"SetVolume": ["LineOut", max(0, min(255, level))]})

    def studio_pfl_state(self) -> tuple[bool, Optional[int]]:
        """Return the desired PFL state so a new process can restore it."""
        return self._studio_pfl, self._studio_saved_volume

    def monitor_cut_state(self) -> bool:
        """Return whether Studio Monitor Cut is armed, for restoration."""
        return self._monitor_cut_enabled

    def get_fader_volumes(self) -> dict[str, int]:
        """Return the four broadcast fader levels currently held by the daemon."""
        if not GoXLRInterface.is_available():
            return {}
        status = self._ipc({"GetStatus": None})
        mixers = status.get("Status", {}).get("mixers", {})
        if not mixers:
            return {}
        volumes = next(iter(mixers.values())).get("levels", {}).get("volumes", {})
        return {
            channel: volumes[channel]
            for _, channel in _FADERS
            if isinstance(volumes.get(channel), int) and 0 <= volumes[channel] <= 255
        }

    def get_mic_type(self) -> str:
        return self._mic_type

    def get_mic_gain(self) -> Optional[int]:
        """Hardware preamp gain (dB-ish, 0-72) for the active mic capsule type.
        Falls back to a live query if start() hasn't run yet (mirrors
        get_headphone_volume/get_line_out_volume below)."""
        if self._mic_gain is None and GoXLRInterface.is_available():
            status = self._ipc({"GetStatus": None})
            mixers = status.get("Status", {}).get("mixers", {})
            if mixers:
                mic_status = next(iter(mixers.values())).get("mic_status", {})
                if isinstance(mic_status.get("mic_type"), str):
                    self._mic_type = mic_status["mic_type"]
                gains = mic_status.get("mic_gains", {})
                if isinstance(gains.get(self._mic_type), int):
                    self._mic_gain = gains[self._mic_type]
        return self._mic_gain

    def set_mic_gain(self, value: int) -> None:
        value = max(_MIC_GAIN_MIN, min(_MIC_GAIN_MAX, value))
        self._mic_gain = value
        if self._on_mic_gain_changed is not None:
            self._on_mic_gain_changed(value)
        if self._serial is not None:
            self._cmd({"SetMicrophoneGain": [self._mic_type, value]})

    def get_studio_return_level(self) -> Optional[int]:
        """Baseline Music-bus (studio return) volume used outside of PFL.
        Falls back to a live query if start() hasn't run yet."""
        if self._studio_return_level is None and GoXLRInterface.is_available():
            status = self._ipc({"GetStatus": None})
            mixers = status.get("Status", {}).get("mixers", {})
            if mixers:
                volumes = next(iter(mixers.values())).get("levels", {}).get("volumes", {})
                if isinstance(volumes.get("Music"), int):
                    self._studio_return_level = volumes["Music"]
        return self._studio_return_level

    def set_studio_return_level(self, level: int) -> None:
        level = max(0, min(255, level))
        self._studio_return_level = level
        if self._on_studio_return_level_changed is not None:
            self._on_studio_return_level_changed(level)
        if self._serial is None:
            return
        if self._studio_pfl:
            # PFL is currently forcing Music to 255; the new baseline takes
            # effect once PFL is released (see _apply_studio_pfl_volume).
            self._studio_saved_volume = level
        else:
            self._cmd({"SetVolume": ["Music", level]})

    async def monitor_pfl(self) -> None:
        """Subscribe to the GoXLR daemon WebSocket and handle studio return PFL via Bleep button."""
        log = logging.getLogger("goxlr.pfl")
        while True:
            try:
                async with websockets.connect(_WEBSOCKET_URL) as ws:
                    log.info("PFL monitor connected")
                    async for message in ws:
                        await self._handle_ws_message(json.loads(message))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"PFL monitor disconnected: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_ws_message(self, data: dict) -> None:
        for patch in data.get("data", {}).get("Patch", []):
            path = patch.get("path", "")
            value = patch.get("value")
            if isinstance(value, int) and 0 <= value <= 255:
                for channel in ("Headphones", "LineOut"):
                    if path.endswith(f"/levels/volumes/{channel}"):
                        if self._on_volume_changed is not None:
                            self._on_volume_changed(channel, value)
                        break
            for fader, channel in _FADERS:
                if (path.endswith(f"/levels/volumes/{channel}")
                        and isinstance(value, int) and 0 <= value <= 255):
                    reported = value
                    if self._on_fader_volume_changed is not None:
                        self._on_fader_volume_changed(channel, reported)
                    if channel == "Game":
                        self._game_level = reported
                    was_open = self._mute_open(channel)
                    was_cutting = self._monitor_cut_active() if channel in ("Mic", "LineIn") else None
                    self._fader_volumes[channel] = reported
                    if was_cutting is not None and self._monitor_cut_active() != was_cutting:
                        await asyncio.to_thread(self._apply_monitor_routing)
                        await asyncio.to_thread(self._set_cough_colour)
                        logger.info(
                            "Studio Monitor Cut %s (%s fader %s)",
                            "engaging" if not was_cutting else "releasing",
                            channel, "opened" if reported > _MIC_OPEN_THRESHOLD else "closed",
                        )
                    if not self._fader_muted.get(fader, False) and self._mute_open(channel) != was_open:
                        await asyncio.to_thread(self._set_mute_button_colour, fader)
                    if (fader in self._pending_pickup_faders
                            and time.monotonic() >= self._pickup_ignore_until):
                        self._pending_pickup_faders.remove(fader)
                        await asyncio.to_thread(self._set_fader_colour, fader)
                        logger.info("Fader %s physically picked up after recovery", fader)
                    break
            matched_mute_state = False
            for fader in _FADER_MUTE_BUTTONS:
                if path.endswith(f"/fader_status/{fader}/mute_state"):
                    muted = value != "Unmuted"
                    self._fader_muted[fader] = muted
                    if fader == "C" and self._on_fader_mute_changed is not None:
                        self._on_fader_mute_changed("Game", muted)
                    await asyncio.to_thread(self._set_mute_button_colour, fader)
                    matched_mute_state = True
                    break
            if matched_mute_state:
                continue
            # The immediate button-down patch arrives before mute_state. It
            # makes the mute LED (and, for Fader C, the clean TX path)
            # respond at the same instant as the physical press; the
            # authoritative mute_state patch above corrects it on release.
            matched_button_down = False
            for fader, button in _FADER_MUTE_BUTTONS.items():
                if path.endswith(f"/button_down/{button}") and value is True:
                    self._fader_muted[fader] = not self._fader_muted[fader]
                    if fader == "C" and self._on_fader_mute_changed is not None:
                        self._on_fader_mute_changed("Game", self._fader_muted[fader])
                    await asyncio.to_thread(self._set_mute_button_colour, fader)
                    matched_button_down = True
                    break
            if matched_button_down:
                continue
            if path.endswith("/button_down/Cough") and value is True:
                self._monitor_cut_enabled = not self._monitor_cut_enabled
                logger.info(
                    "Cough pressed — Studio Monitor Cut %s",
                    "armed" if self._monitor_cut_enabled else "disarmed",
                )
                await asyncio.to_thread(self._apply_monitor_routing)
                await asyncio.to_thread(self._set_cough_colour)
                if self._on_monitor_cut_changed is not None:
                    self._on_monitor_cut_changed(self._monitor_cut_enabled)
                continue
            if "/button_down/Bleep" not in path or patch.get("value") is not True:
                continue
            self._studio_pfl = not self._studio_pfl
            t0 = time.monotonic()
            logger.info(
                f"Bleep pressed — PFL {'engaging' if self._studio_pfl else 'releasing'}, "
                f"starting GoXLR IPC burst"
            )
            await asyncio.to_thread(self._apply_studio_pfl_volume)
            await asyncio.to_thread(self._apply_monitor_routing)
            await asyncio.to_thread(self._set_bleep_colour)
            if self._on_pfl_changed is not None:
                self._on_pfl_changed(self._studio_pfl, self._studio_saved_volume)
            dt_ms = (time.monotonic() - t0) * 1000
            logger.info(
                f"Studio return PFL {'active' if self._studio_pfl else 'off'} "
                f"(IPC burst took {dt_ms:.0f}ms total)"
            )

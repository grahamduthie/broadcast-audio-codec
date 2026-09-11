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
_BEHRINGER_DEVICE = "hw:CODEC,0"
_MONITOR_OUTPUTS = ("Headphones", "LineOut")

# audiomixer's automatic latency query fails on this pipeline ("Latency query
# failed" — the mix of a manually-fed appsrc branch and a live alsasrc branch
# never negotiates a value), leaving it to assume 0 additional latency. Giving
# it an explicit budget matching the jitter buffer's 200ms lets both branches'
# buffers actually line up instead of the aggregator starving/stalling.
_GOXLR_MIX_PROPS = "ignore-inactive-pads=true min-upstream-latency=200000000 latency=200000000"

# Maps fader letters to GoXLR routing source names
_FADER_TO_SOURCE = {
    "A": "Microphone",
    "B": "Chat",       # Guest mic (Behringer)
    "C": "Game",       # News / Codec RX Right
    "D": "LineIn",     # Music Player / GoXLR Line In
}

_FADERS = [
    ("A", "Mic"),      # Main microphone (XLR)
    ("B", "Chat"),     # Guest microphone (Behringer capture)
    ("C", "Game"),     # News feed (Codec RX Right)
    ("D", "LineIn"),   # Music Player (GoXLR Line In)
]

# Full routing matrix applied on every start.
# Music carries the studio return (codec RX Left) but has no fader — only
# accessible via Bleep PFL. Chat is now the second mic (Behringer) and
# goes to BroadcastMix so the operator can fade it into the broadcast.
_ROUTING = {
    "Microphone": {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "LineIn":     {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "Game":       {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "Chat":       {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "Music":      {"Headphones": False, "BroadcastMix": False,  "Sampler": False, "LineOut": False, "StreamMix2": False},
    "Console":    {"Headphones": False, "BroadcastMix": False,  "Sampler": False, "LineOut": False, "StreamMix2": False},
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


class GoXLRInterface(AudioInterface):

    def __init__(self, config: dict, studio_pfl: bool = False,
                 studio_saved_volume: Optional[int] = None,
                 on_pfl_changed: Optional[Callable[[bool, Optional[int]], None]] = None,
                 restored_fader_volumes: Optional[dict[str, int]] = None,
                 on_fader_volume_changed: Optional[Callable[[str, int], None]] = None,
                 on_volume_changed: Optional[Callable[[str, int], None]] = None):
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
        # The GoXLR has non-motorised faders. Amber identifies controls whose
        # restored logical value may still need physical soft pickup.
        self._pending_pickup_faders = {
            fader for fader, channel in _FADERS if channel in self._restored_fader_volumes
        }
        self._pickup_ignore_until = 0.0
        self._on_fader_volume_changed = on_fader_volume_changed
        self._on_volume_changed = on_volume_changed
        self._monitor_routing: dict[tuple[str, str], bool] = {}
        if _IS_MACOS:
            goxlr_cfg = config.get("goxlr", {})
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
        return (
            "alsasrc device=goxlr_broadcast ! audioconvert ! "
            "audio/x-raw,rate=48000,channels=2"
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
        logger.info(f"GoXLR Mini ready: serial={self._serial}")
        self._apply_faders()
        self._apply_fader_volumes()
        self._apply_routing()
        self._apply_mute_functions()
        self._apply_colours()
        if self._studio_pfl:
            # Reassert PFL routing/volume/colour over the non-PFL defaults
            # _apply_routing()/_apply_colours() just set above.
            self._apply_monitor_routing()
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

    def _apply_routing(self) -> None:
        for source, outputs in _ROUTING.items():
            for output, enabled in outputs.items():
                self._cmd({"SetRouter": [source, output, enabled]})

    def _apply_mute_functions(self) -> None:
        for fader, _ in _FADERS:
            self._cmd({"SetFaderMuteFunction": [fader, "All"]})

    def _apply_colours(self) -> None:
        for fader, _ in _FADERS:
            self._cmd({"SetFaderDisplayStyle": [fader, "Gradient"]})
            self._set_fader_colour(fader)
        self._cmd({"SetButtonColours": ["Bleep", _NORMAL_COLOUR, "000000"]})

    def _set_fader_colour(self, fader: str) -> None:
        colour = _PICKUP_COLOUR if fader in self._pending_pickup_faders else _NORMAL_COLOUR
        self._cmd({"SetFaderColours": [fader, colour, "000000"]})

    def _apply_monitor_routing(self) -> None:
        """Solo Music in headphones and Line Out during PFL; restore both when off.
        Only sends SetRouter for crosspoints whose state has actually changed."""
        desired = {
            **{_FADER_TO_SOURCE[fader]: not self._studio_pfl for fader, _ in _FADERS},
            "Music": self._studio_pfl,
        }
        for source, enabled in desired.items():
            for output in _MONITOR_OUTPUTS:
                key = (source, output)
                if self._monitor_routing.get(key) != enabled:
                    self._cmd({"SetRouter": [source, output, enabled]})
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
        self._cmd({"SetButtonColours": ["Bleep", colour, "000000"]})

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
                    if self._on_fader_volume_changed is not None:
                        self._on_fader_volume_changed(channel, value)
                    if (fader in self._pending_pickup_faders
                            and time.monotonic() >= self._pickup_ignore_until):
                        self._pending_pickup_faders.remove(fader)
                        await asyncio.to_thread(self._set_fader_colour, fader)
                        logger.info("Fader %s physically picked up after recovery", fader)
                    break
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

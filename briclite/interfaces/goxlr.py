import asyncio
import os
import sys
import socket
import struct
import json
import logging

import websockets

from .base import AudioInterface

_IS_MACOS = sys.platform == "darwin"

logger = logging.getLogger("goxlr")

_SOCKET_PATH = "/tmp/goxlr.socket"
_WEBSOCKET_URL = "ws://localhost:14564/api/websocket"
_ALSA_CONFIG_PATH = os.path.expanduser("~/.asoundrc")

_PFL_COLOUR      = "FF8800"  # orange — studio return PFL active
_NORMAL_COLOUR   = "00FFFF"  # cyan   — normal
_BEHRINGER_DEVICE = "hw:CODEC,0"

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

    def __init__(self, config: dict):
        self._serial: str | None = None
        self._studio_pfl: bool = False
        self._studio_saved_volume: int | None = None
        self._hp_routing: dict[str, bool] = {}
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
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(_SOCKET_PATH)
        result = self._query(sock, payload)
        sock.close()
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

    def rx_sink_bin(self) -> str:
        if _IS_MACOS:
            # Simple stereo output when no GoXLR (capture_channels <= 2)
            if not self._mac_rx_device or self._capture_channels <= 2:
                uid = f'unique-id="{self._mac_rx_device}" ' if self._mac_rx_device else ''
                return f'audioresample ! audioconvert ! volume name=rx_vol volume=1.0 ! osxaudiosink {uid}sync=false'
            # GoXLR: route through 10-channel playback matrix
            return (
                f'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
                f'audiomixmatrix in-channels=2 out-channels=10 matrix="{_RX_MATRIX}" ! '
                f'audiomixer name=goxlr_mix ! '
                f'audioconvert ! audio/x-raw,format=S32LE ! '
                f'osxaudiosink unique-id="{self._mac_rx_device}" sync=false'
            )
        return (
            f'audioresample ! audio/x-raw,rate=48000,channels=2 ! '
            f'audiomixmatrix in-channels=2 out-channels=10 matrix="{_RX_MATRIX}" ! '
            f'audiomixer name=goxlr_mix ! '
            f'audioconvert ! audio/x-raw,format=S32LE ! '
            f'alsasink device=hw:GoXLRMini,0 sync=false'
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
        self._studio_pfl = False
        self._studio_saved_volume = None
        self._hp_routing = {_FADER_TO_SOURCE[f]: True for f, _ in _FADERS}
        self._hp_routing["Music"] = False  # studio return starts off in headphones
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
        self._apply_routing()
        self._apply_mute_functions()
        self._apply_colours()

    def stop(self) -> None:
        pass

    def _write_alsa_config(self) -> None:
        with open(_ALSA_CONFIG_PATH, "w") as f:
            f.write(_ALSA_CONFIG)

    def _apply_faders(self) -> None:
        for fader, channel in _FADERS:
            self._cmd({"SetFader": [fader, channel]})

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
            self._cmd({"SetFaderColours": [fader, _NORMAL_COLOUR, "000000"]})
        self._cmd({"SetButtonColours": ["Bleep", _NORMAL_COLOUR, "000000"]})

    def _apply_headphone_routing(self) -> None:
        """Solo Music (studio return) in headphones when PFL active; restore full mix when not.
        Only sends SetRouter for crosspoints whose state has actually changed."""
        for fader, _ in _FADERS:
            source = _FADER_TO_SOURCE[fader]
            enabled = not self._studio_pfl
            if self._hp_routing.get(source) != enabled:
                self._cmd({"SetRouter": [source, "Headphones", enabled]})
                self._hp_routing[source] = enabled
        # Music (studio return): only routed to headphones during PFL
        music_enabled = self._studio_pfl
        if self._hp_routing.get("Music") != music_enabled:
            self._cmd({"SetRouter": ["Music", "Headphones", music_enabled]})
            self._hp_routing["Music"] = music_enabled

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
            if "/button_down/Bleep" not in path or patch.get("value") is not True:
                continue
            self._studio_pfl = not self._studio_pfl
            await asyncio.to_thread(self._apply_studio_pfl_volume)
            await asyncio.to_thread(self._apply_headphone_routing)
            await asyncio.to_thread(self._set_bleep_colour)
            logger.info(f"Studio return PFL {'active' if self._studio_pfl else 'off'}")

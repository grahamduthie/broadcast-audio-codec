import os
import socket
import struct
import json
import logging

from .base import AudioInterface

logger = logging.getLogger("goxlr")

_SOCKET_PATH = "/tmp/goxlr.socket"
_ALSA_CONFIG_PATH = os.path.expanduser("~/.asoundrc")

_FADERS = [
    ("A", "Mic"),
    ("B", "LineIn"),
    ("C", "Game"),    # Codec RX Right
    ("D", "Chat"),    # Codec RX Left
]

# Full routing matrix applied on every start.
# Source names and output names match the daemon's IPC JSON keys.
_ROUTING = {
    "Microphone": {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "LineIn":     {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "Game":       {"Headphones": True,  "BroadcastMix": True,  "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "Chat":       {"Headphones": True,  "BroadcastMix": False, "Sampler": False, "LineOut": True,  "StreamMix2": False},
    "Music":      {"Headphones": False, "BroadcastMix": False, "Sampler": False, "LineOut": False, "StreamMix2": False},
    "Console":    {"Headphones": False, "BroadcastMix": False, "Sampler": False, "LineOut": False, "StreamMix2": False},
    "System":     {"Headphones": False, "BroadcastMix": False, "Sampler": False, "LineOut": False, "StreamMix2": False},
    "Samples":    {"Headphones": False, "BroadcastMix": False, "Sampler": False, "LineOut": False, "StreamMix2": False},
}

# ALSA virtual devices written to ~/.asoundrc on start.
#
# goxlr_broadcast (capture): extracts the Broadcast Mix (capture ch 0-1)
#   from the GoXLR's 21-channel USB capture stream as a stereo source.
#
# goxlr_rx (playback): routes a stereo input to two GoXLR playback channel
#   pairs so each codec RX channel lands on a separate fader:
#     source ch 0 (RX Left)  → Chat  playback ch 4+5 → Fader D
#     source ch 1 (RX Right) → Game  playback ch 2+3 → Fader C
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

pcm.goxlr_rx {
    type route
    slave {
        pcm "hw:GoXLRMini,0"
        channels 10
    }
    ttable {
        2.1 1.0
        3.1 1.0
        4.0 1.0
        5.0 1.0
    }
}
"""


class GoXLRInterface(AudioInterface):

    def __init__(self, config: dict):
        self._serial: str | None = None

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
        result = self._ipc({"Command": [self._serial, command]})
        if result != "Ok":
            logger.warning(f"GoXLR command {command} returned: {result}")

    def tx_source_bin(self) -> str:
        return (
            "alsasrc device=goxlr_broadcast ! audioconvert ! "
            "audio/x-raw,rate=48000,channels=2"
        )

    def rx_sink_bin(self) -> str:
        return (
            "audioresample ! audio/x-raw,rate=48000,channels=2 ! "
            "alsasink device=goxlr_rx sync=false"
        )

    def start(self) -> None:
        self._write_alsa_config()
        status = self._ipc({"GetStatus": None})
        self._serial = next(iter(status["Status"]["mixers"]))
        logger.info(f"GoXLR Mini ready: serial={self._serial}")
        self._apply_faders()
        self._apply_routing()
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

    def _apply_colours(self) -> None:
        for fader, _ in _FADERS:
            self._cmd({"SetFaderDisplayStyle": [fader, "Gradient"]})
            self._cmd({"SetFaderColours": [fader, "00FFFF", "000000"]})

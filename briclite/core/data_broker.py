import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass
class ApplianceState:
    connection_status: str = "DISCONNECTED"
    runtime_seconds: int = 0
    tx_peak_l: float = -60.0
    tx_peak_r: float = -60.0
    rx_peak_l: float = -60.0
    rx_peak_r: float = -60.0
    jitter_avg: int = 0
    packets_lost: int = 0
    packets_late: int = 0
    rx_channel_mode: str = "stereo"
    audio_interface: str = "Behringer"
    headphone_volume: int = 255
    rx_volume: int = 100
    # GoXLR channel strips. fader_volumes is keyed by channel name
    # (Mic/LineIn/Game/Console — see interfaces/goxlr.py's _FADERS). This is
    # fader *position*, not a real audio-level meter — the GoXLR Mini has no
    # such telemetry (see CURRENT-STATUS.md's 2026-09-13 investigation).
    # mic_gain is the real hardware preamp gain for the Mic capsule; a
    # software trim was also tried for LineIn/Game/Console and reverted the
    # same day — see the note above _MIC_GAIN_MIN in interfaces/goxlr.py.
    fader_volumes: Dict[str, int] = field(default_factory=dict)
    mic_gain: int = 0
    mic_gain_max: int = 72
    studio_return_level: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def update_metrics(self, data: Dict[str, Any]):
        async with self.lock:
            for key, val in data.items():
                if hasattr(self, key):
                    setattr(self, key, val)

    async def set_fader_volume(self, channel: str, value: int):
        async with self.lock:
            self.fader_volumes[channel] = value

    async def get_snapshot(self) -> Dict[str, Any]:
        async with self.lock:
            return {
                "status": self.connection_status,
                "runtime": self.runtime_seconds,
                "tx_peak_l": self.tx_peak_l,
                "tx_peak_r": self.tx_peak_r,
                "rx_peak_l": self.rx_peak_l,
                "rx_peak_r": self.rx_peak_r,
                "jitter": self.jitter_avg,
                "lost": self.packets_lost,
                "late": self.packets_late,
                "rx_channel_mode": self.rx_channel_mode,
                "audio_interface": self.audio_interface,
                "headphone_volume": self.headphone_volume,
                "rx_volume": self.rx_volume,
                "fader_volumes": dict(self.fader_volumes),
                "mic_gain": self.mic_gain,
                "mic_gain_max": self.mic_gain_max,
                "studio_return_level": self.studio_return_level,
            }


global_state = ApplianceState()

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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def update_metrics(self, data: Dict[str, Any]):
        async with self.lock:
            for key, val in data.items():
                if hasattr(self, key):
                    setattr(self, key, val)

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
            }


global_state = ApplianceState()

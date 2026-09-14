import asyncio
from collections import deque
from dataclasses import dataclass, field
import time
from typing import Deque, Dict, Any, Tuple


_NETWORK_SHORT_WINDOW_S = 60.0
_NETWORK_LONG_WINDOW_S = 300.0


@dataclass
class ApplianceState:
    connection_status: str = "DISCONNECTED"
    runtime_seconds: int = 0
    tx_peak_l: float = -60.0
    tx_peak_r: float = -60.0
    rx_peak_l: float = -60.0
    rx_peak_r: float = -60.0
    jitter_avg: float = 0.0
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
    # Rolling network-health history deliberately belongs to the process-wide
    # broker, not PipelineController: an automatic reconnect replaces the
    # controller and its jitter buffer, but must not erase the evidence that
    # caused the reconnect in the first place.
    _jitter_samples: Deque[Tuple[float, float]] = field(default_factory=deque)
    _loss_events: Deque[float] = field(default_factory=deque)
    _late_events: Deque[float] = field(default_factory=deque)
    _rx_outages: Deque[Tuple[float, float, float, str]] = field(default_factory=deque)
    _rx_outage_active: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def update_metrics(self, data: Dict[str, Any]):
        async with self.lock:
            for key, val in data.items():
                if hasattr(self, key):
                    setattr(self, key, val)

    async def set_fader_volume(self, channel: str, value: int):
        async with self.lock:
            self.fader_volumes[channel] = value

    def _trim_network_history(self, now: float) -> None:
        cutoff = now - _NETWORK_LONG_WINDOW_S
        while self._jitter_samples and self._jitter_samples[0][0] < cutoff:
            self._jitter_samples.popleft()
        while self._loss_events and self._loss_events[0] < cutoff:
            self._loss_events.popleft()
        while self._late_events and self._late_events[0] < cutoff:
            self._late_events.popleft()
        while self._rx_outages and self._rx_outages[0][0] < cutoff:
            self._rx_outages.popleft()

    @staticmethod
    def _count_since(events: Deque[float], cutoff: float) -> int:
        return sum(event >= cutoff for event in events)

    async def record_network_sample(self, jitter_ms: float, lost_events: int, late_events: int) -> None:
        """Record one playout-stat sample plus any newly observed packet events.

        Counters passed in are deltas since the preceding sample, not totals.
        This makes the rolling history independent of a controller/jitter-buffer
        reset during a normal or automatic reconnect.
        """
        now = time.monotonic()
        async with self.lock:
            self.jitter_avg = round(max(0.0, jitter_ms), 1)
            self._jitter_samples.append((now, self.jitter_avg))
            self._loss_events.extend(now for _ in range(max(0, lost_events)))
            self._late_events.extend(now for _ in range(max(0, late_events)))
            self._trim_network_history(now)

    async def record_rx_outage(self, duration_s: float, reason: str) -> None:
        """Retain an RX outage and mark recovery as currently in progress."""
        now = time.monotonic()
        async with self.lock:
            self._rx_outages.append((now, time.time(), max(0.0, duration_s), reason))
            self._rx_outage_active = True
            self._trim_network_history(now)

    async def mark_rx_packets_resumed(self) -> None:
        """Clear the active-outage state as soon as the replacement RX path
        receives a valid packet. Historical outage data remains available for
        the five-minute warning and incident readout."""
        async with self.lock:
            self._rx_outage_active = False

    def _network_health(self, now: float, lost_60s: int, late_60s: int) -> str:
        if self.connection_status != "CONNECTED":
            return "idle"
        if self._rx_outage_active:
            return "critical"
        if self.jitter_avg >= 30.0 or lost_60s or late_60s or self._rx_outages:
            return "warning"
        return "healthy"

    async def get_snapshot(self) -> Dict[str, Any]:
        async with self.lock:
            now = time.monotonic()
            self._trim_network_history(now)
            short_cutoff = now - _NETWORK_SHORT_WINDOW_S
            lost_60s = self._count_since(self._loss_events, short_cutoff)
            late_60s = self._count_since(self._late_events, short_cutoff)
            jitter_5m_max = max((value for _, value in self._jitter_samples), default=0.0)
            last_outage = self._rx_outages[-1] if self._rx_outages else None
            return {
                "status": self.connection_status,
                "runtime": self.runtime_seconds,
                "tx_peak_l": self.tx_peak_l,
                "tx_peak_r": self.tx_peak_r,
                "rx_peak_l": self.rx_peak_l,
                "rx_peak_r": self.rx_peak_r,
                "jitter": self.jitter_avg,
                "jitter_5m_max": round(jitter_5m_max, 1),
                "lost": lost_60s,
                "late": late_60s,
                "lost_60s": lost_60s,
                "lost_5m": len(self._loss_events),
                "late_60s": late_60s,
                "late_5m": len(self._late_events),
                "rx_outages_5m": len(self._rx_outages),
                "last_rx_incident_at": last_outage[1] if last_outage else None,
                "last_rx_incident_duration_s": round(last_outage[2], 1) if last_outage else None,
                "last_rx_incident_reason": last_outage[3] if last_outage else None,
                "network_health": self._network_health(now, lost_60s, late_60s),
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

from .base import ALSA_BUFFER_TIME_US, ALSA_LATENCY_TIME_US, AudioInterface


class BehringerInterface(AudioInterface):

    def __init__(self, config: dict):
        self._device = config["audio_network"]["alsa_device"]

    def tx_source_bin(self) -> str:
        return (
            f"alsasrc device={self._device} ! audioconvert ! "
            f"audio/x-raw,rate=44100,channels=2"
        )

    def rx_sink_bin(self) -> str:
        return (
            f"alsasink device={self._device} sync=false "
            f"buffer-time={ALSA_BUFFER_TIME_US} latency-time={ALSA_LATENCY_TIME_US}"
        )

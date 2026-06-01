from abc import ABC, abstractmethod


class AudioInterface(ABC):

    @abstractmethod
    def tx_source_bin(self) -> str:
        """GStreamer bin description for TX capture. Must produce audio/x-raw,rate=44100,channels=2."""

    @abstractmethod
    def rx_sink_bin(self) -> str:
        """GStreamer bin description for RX playout. Accepts audio/x-raw,rate=44100,channels=2."""

    def start(self) -> None:
        """Called when the codec pipeline is starting."""

    def stop(self) -> None:
        """Called when the codec pipeline is stopping."""

from abc import ABC, abstractmethod


class AudioInterface(ABC):

    @abstractmethod
    def tx_source_bin(self) -> str:
        """GStreamer bin description for TX capture. Must produce audio/x-raw,rate=44100,channels=2."""

    @abstractmethod
    def rx_sink_bin(self) -> str:
        """GStreamer bin description for RX playout. Accepts audio/x-raw,rate=44100,channels=2."""

    def extra_rx_source_bins(self) -> list[str]:
        """Additional GStreamer source bins to mix into the RX output.
        Each string is appended to the RX pipeline and must connect to a named
        audiomixer element (e.g. 'goxlr_mix.') defined in rx_sink_bin()."""
        return []

    def start(self) -> None:
        """Called when the codec pipeline is starting."""

    def stop(self) -> None:
        """Called when the codec pipeline is stopping."""

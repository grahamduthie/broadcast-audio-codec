from abc import ABC, abstractmethod

# Shared hardware-sink buffering for every rx_sink_bin() implementation.
# Doubled from GStreamer's audiobasesink defaults (200000/10000) to give the
# RX playout thread headroom against transient scheduling stalls on the weak
# embedded hardware this runs on, at the cost of ~200ms extra output latency —
# see ARCHITECTURE.md §10/§12.4.
ALSA_BUFFER_TIME_US  = 400000   # 400ms
ALSA_LATENCY_TIME_US = 20000    # 20ms


class AudioInterface(ABC):

    def rx_sample_rate(self) -> int:
        """Native sample rate expected by rx_sink_bin()."""
        return 44100

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

    def clean_news_return_enabled(self) -> bool:
        """Whether TX has a PSA-side clean-news branch.

        The default keeps conventional interfaces entirely unchanged.  The
        GoXLR implementation uses this to keep an RX-right/news signal out of
        the GoXLR Broadcast Mix and add its pre-playback copy at the PSA.
        """
        return False

    def clean_news_return_level(self) -> int:
        """Current Fader-C-equivalent level (0--255) for the clean TX branch."""
        return 255

    def clean_news_return_muted(self) -> bool:
        """Current Fader-C-equivalent mute state for the clean TX branch."""
        return False

    def clean_news_sink_bin(self) -> str:
        """Hardware/virtual sink for the clean RX-right branch when enabled."""
        raise NotImplementedError("clean news return is not enabled for this interface")

    def start(self) -> None:
        """Called when the codec pipeline is starting."""

    def stop(self) -> None:
        """Called when the codec pipeline is stopping."""

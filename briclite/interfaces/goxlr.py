from .base import AudioInterface


class GoXLRInterface(AudioInterface):
    """
    GoXLR Mini audio interface.

    TX source: GoXLR Broadcast Mix (capture channels 0-1 of the 21-channel USB stream).
    RX sink:   Configurable GoXLR playback channel pairs, allowing the L and R codec
               receive streams to be routed to separate GoXLR input busses.

    Routing matrix configuration (fader assignments, mix bus membership) is managed
    via the goxlr-utility daemon IPC.
    """

    def __init__(self, config: dict):
        self._config = config.get("goxlr", {})

    def tx_source_bin(self) -> str:
        raise NotImplementedError("GoXLR interface not yet implemented")

    def rx_sink_bin(self) -> str:
        raise NotImplementedError("GoXLR interface not yet implemented")

    def start(self) -> None:
        raise NotImplementedError("GoXLR interface not yet implemented")

    def stop(self) -> None:
        raise NotImplementedError("GoXLR interface not yet implemented")

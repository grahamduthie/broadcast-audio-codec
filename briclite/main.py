import json
import logging
import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.data_broker import global_state
from core.pipeline_manager import PipelineController
from interfaces.base import AudioInterface
from interfaces.behringer import BehringerInterface
from interfaces.goxlr import GoXLRInterface

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

_BRICLITE_HOME = os.environ.get("BRICLITE_HOME", "/opt/briclite")

with open(os.path.join(_BRICLITE_HOME, "config.json")) as f:
    config = json.load(f)

controller: Optional[PipelineController] = None
interface: Optional[AudioInterface] = None


async def _sync_goxlr_state(iface: AudioInterface) -> None:
    """Push GoXLR-specific values into global state after an interface is created."""
    if isinstance(iface, GoXLRInterface):
        vol = iface.get_headphone_volume()
        await global_state.update_metrics({"headphone_volume": vol})


def _make_interface(cfg: dict) -> AudioInterface:
    kind = cfg.get("system", {}).get("audio_interface", "auto")
    if kind == "goxlr":
        return GoXLRInterface(cfg)
    if kind == "behringer":
        return BehringerInterface(cfg)
    # auto: GoXLR takes priority when available
    if GoXLRInterface.is_available():
        logging.getLogger("main").info("GoXLR Mini detected — using GoXLR interface")
        return GoXLRInterface(cfg)
    logging.getLogger("main").info("No GoXLR Mini found — using Behringer interface")
    return BehringerInterface(cfg)


async def _cancel_task(task: Optional[asyncio.Task]) -> None:
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


_RX_WATCHDOG_S = 10.0
_full_reconnect_lock = asyncio.Lock()


async def _full_reconnect(rx_channel_mode: Optional[str] = None) -> None:
    """Stop and fully rebuild the pipeline (both TX and RX together) rather than
    rebuilding RX in place.

    With the GoXLR interface, TX (capture) and RX (playback) are two directions
    of the same physical USB audio device. Tearing down and reopening only the
    RX side while TX stays open reliably wedges the GoXLR's ALSA device —
    PipelineController._do_rx_rebuild's in-place RX-only rebuild is only safe
    for the plain Behringer interface, which has no shared device. This is used
    for RX channel-mode changes and Behringer hotplug while running GoXLR.
    """
    global controller
    async with _full_reconnect_lock:
        if not controller.is_active:
            return
        loop = asyncio.get_event_loop()
        mode = rx_channel_mode if rx_channel_mode is not None else controller.rx_channel_mode
        controller.stop(loop)
        await asyncio.sleep(0.3)
        controller = PipelineController(config, interface, rx_channel_mode=mode)
        controller.start(loop)


async def _rx_watchdog():
    """Reconnect automatically when no RX packets arrive for _RX_WATCHDOG_S seconds.

    The Comrex drops its session on a TX gap. Briclite has no way to know this
    has happened other than noticing that the Comrex has stopped sending back.
    A new PipelineController produces a new random SSRC, which makes the Comrex
    treat the resumed stream as a fresh incoming call and re-establish.
    """
    global controller
    log = logging.getLogger("watchdog")
    while True:
        await asyncio.sleep(3.0)
        if not controller.is_active:
            continue
        elapsed = time.monotonic() - controller.last_rx_packet
        if elapsed > _RX_WATCHDOG_S:
            log.warning(f"No RX packets for {elapsed:.1f}s — auto-reconnecting")
            loop = asyncio.get_event_loop()
            mode = controller.rx_channel_mode
            controller.stop(loop)
            await asyncio.sleep(1.0)
            controller = PipelineController(config, interface, rx_channel_mode=mode)
            controller.start(loop)
            await _sync_goxlr_state(interface)
            log.info("Auto-reconnect complete")


async def _interface_monitor():
    """Poll every 5 s and hot-swap the audio interface if GoXLR presence changes.

    Hot-swap only runs in auto mode; an explicit audio_interface setting is fixed.
    Independently of that, while in GoXLR mode this also watches for the
    Behringer (second mic) being plugged/unplugged and rebuilds just the RX
    pipeline to pick it up — that device is optional and must not require a
    full interface switch to recover.
    """
    global controller, interface
    log = logging.getLogger("main")
    _auto_mode = config.get("system", {}).get("audio_interface", "auto") == "auto"
    pfl_task: Optional[asyncio.Task] = None
    behringer_present = GoXLRInterface.behringer_available() if isinstance(interface, GoXLRInterface) else False
    try:
        if isinstance(interface, GoXLRInterface):
            pfl_task = asyncio.create_task(interface.monitor_pfl())
        while True:
            await asyncio.sleep(5)

            if isinstance(interface, GoXLRInterface):
                now_present = GoXLRInterface.behringer_available()
                if now_present != behringer_present:
                    behringer_present = now_present
                    log.info(f"Behringer second mic {'connected' if now_present else 'disconnected'}")
                    await _full_reconnect()

            if not _auto_mode:
                continue
            goxlr_now = GoXLRInterface.is_available()
            goxlr_was = isinstance(interface, GoXLRInterface)
            if goxlr_now == goxlr_was:
                continue
            await _cancel_task(pfl_task)
            pfl_task = None
            loop = asyncio.get_event_loop()
            if controller.is_active:
                log.info("Audio interface change detected while codec active — stopping pipeline")
                controller.stop(loop)
            interface = _make_interface(config)
            controller = PipelineController(config, interface)
            mode = "GoXLR" if goxlr_now else "Behringer"
            await global_state.update_metrics({"audio_interface": mode})
            await _sync_goxlr_state(interface)
            log.info(f"Audio interface switched to {mode}")
            behringer_present = GoXLRInterface.behringer_available() if goxlr_now else False
            if isinstance(interface, GoXLRInterface):
                pfl_task = asyncio.create_task(interface.monitor_pfl())
    finally:
        await _cancel_task(pfl_task)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, interface
    interface = _make_interface(config)
    controller = PipelineController(config, interface)
    mode = "GoXLR" if isinstance(interface, GoXLRInterface) else "Behringer"
    await global_state.update_metrics({"audio_interface": mode})
    await _sync_goxlr_state(interface)
    await global_state.update_metrics({"rx_volume": 100})
    monitor  = asyncio.create_task(_interface_monitor())
    watchdog = asyncio.create_task(_rx_watchdog())
    yield
    for task in (monitor, watchdog):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Broadcast Audio Codec Core", lifespan=lifespan)


class ConnectRequest(BaseModel):
    target_ip: Optional[str] = None


class RxModeRequest(BaseModel):
    mode: str

class HeadphoneVolumeRequest(BaseModel):
    pct: int  # 0–100

class RxVolumeRequest(BaseModel):
    pct: int  # 0–100


@app.post("/api/connect")
async def connect_codec(body: ConnectRequest = ConnectRequest()):
    global controller
    loop = asyncio.get_event_loop()
    if controller.is_active:
        return {"status": "error", "message": "Already running"}
    if body.target_ip:
        config["audio_network"]["target_ip"] = body.target_ip
    snapshot = await global_state.get_snapshot()
    saved_mode = snapshot.get("rx_channel_mode", "stereo")
    controller = PipelineController(config, interface, rx_channel_mode=saved_mode)
    controller.start(loop)
    return {"status": "success", "message": "Pipeline active"}


@app.post("/api/rx_mode")
async def set_rx_mode(body: RxModeRequest):
    if body.mode not in {"stereo", "left", "right"}:
        return {"status": "error", "message": "mode must be stereo, left, or right"}
    await global_state.update_metrics({"rx_channel_mode": body.mode})
    if isinstance(interface, GoXLRInterface):
        await _full_reconnect(body.mode)
    else:
        controller.set_rx_channel_mode(body.mode)
    return {"status": "success", "message": f"RX routing: {body.mode}"}


@app.post("/api/headphone_volume")
async def set_headphone_volume(body: HeadphoneVolumeRequest):
    if not isinstance(interface, GoXLRInterface):
        return {"status": "error", "message": "Not in GoXLR mode"}
    level = round(body.pct * 255 / 100)
    interface.set_headphone_volume(level)
    await global_state.update_metrics({"headphone_volume": level})
    return {"status": "success"}


@app.post("/api/rx_volume")
async def set_rx_volume(body: RxVolumeRequest):
    if not 0 <= body.pct <= 100:
        return {"status": "error", "message": "pct must be 0–100"}
    controller.set_rx_volume(body.pct)
    await global_state.update_metrics({"rx_volume": body.pct})
    return {"status": "success"}


@app.post("/api/disconnect")
async def disconnect_codec():
    loop = asyncio.get_event_loop()
    if not controller.is_active:
        return {"status": "error", "message": "Pipeline inactive"}
    controller.stop(loop)
    return {"status": "success", "message": "Pipeline halted"}


@app.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await asyncio.sleep(0.1)
            snapshot = await global_state.get_snapshot()
            await websocket.send_json(snapshot)
    except WebSocketDisconnect:
        pass


@app.get("/")
async def get_dashboard():
    with open(os.path.join(_BRICLITE_HOME, "web/templates/index.html")) as f:
        return HTMLResponse(content=f.read(), status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app",
                host=config["system"]["bind_address"],
                port=config["system"]["web_port"])

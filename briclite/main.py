import json
import logging
import asyncio
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

with open("/opt/briclite/config.json") as f:
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


async def _interface_monitor():
    """Poll every 5 s and hot-swap the audio interface if GoXLR presence changes."""
    global controller, interface
    log = logging.getLogger("main")
    pfl_task: Optional[asyncio.Task] = None
    try:
        if isinstance(interface, GoXLRInterface):
            pfl_task = asyncio.create_task(interface.monitor_pfl())
        while True:
            await asyncio.sleep(5)
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
    monitor = asyncio.create_task(_interface_monitor())
    yield
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Broadcast Audio Codec Core", lifespan=lifespan)


class ConnectRequest(BaseModel):
    target_ip: Optional[str] = None


class RxModeRequest(BaseModel):
    mode: str

class HeadphoneVolumeRequest(BaseModel):
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
    controller = PipelineController(config, interface)
    controller.start(loop)
    saved_mode = snapshot.get("rx_channel_mode", "stereo")
    if saved_mode != "stereo":
        controller.set_rx_channel_mode(saved_mode)
    return {"status": "success", "message": "Pipeline active"}


@app.post("/api/rx_mode")
async def set_rx_mode(body: RxModeRequest):
    if body.mode not in {"stereo", "left", "right"}:
        return {"status": "error", "message": "mode must be stereo, left, or right"}
    await global_state.update_metrics({"rx_channel_mode": body.mode})
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
    with open("/opt/briclite/web/templates/index.html") as f:
        return HTMLResponse(content=f.read(), status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app",
                host=config["system"]["bind_address"],
                port=config["system"]["web_port"])

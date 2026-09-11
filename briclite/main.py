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
from core.desired_state import DesiredState
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
desired_state = DesiredState()


def _persist_pfl_state(active: bool, saved_volume: Optional[int]) -> None:
    """Keep PFL intent across a Briclite or GoXLR-daemon process restart."""
    desired = desired_state.load()
    if not desired:
        return
    desired["studio_pfl"] = active
    desired["studio_saved_volume"] = saved_volume if active else None
    desired_state.save(desired)


async def _sync_goxlr_state(iface: AudioInterface) -> None:
    """Push GoXLR-specific values into global state after an interface is created."""
    if isinstance(iface, GoXLRInterface):
        headphone_vol = iface.get_headphone_volume()
        line_out_pct = round(iface.get_line_out_volume() * 100 / 255)
        await global_state.update_metrics({
            "headphone_volume": headphone_vol,
            "rx_volume": line_out_pct,
        })


def _make_interface(cfg: dict) -> AudioInterface:
    desired = desired_state.load()
    goxlr_options = {
        "studio_pfl": bool(desired.get("studio_pfl", False)),
        "studio_saved_volume": desired.get("studio_saved_volume"),
        "on_pfl_changed": _persist_pfl_state,
    }
    kind = cfg.get("system", {}).get("audio_interface", "auto")
    if kind == "goxlr":
        return GoXLRInterface(cfg, **goxlr_options)
    if kind == "behringer":
        return BehringerInterface(cfg)
    # auto: GoXLR takes priority when available
    if GoXLRInterface.is_available():
        logging.getLogger("main").info("GoXLR Mini detected — using GoXLR interface")
        return GoXLRInterface(cfg, **goxlr_options)
    logging.getLogger("main").info("No GoXLR Mini found — using Behringer interface")
    return BehringerInterface(cfg)


async def _save_desired_link() -> None:
    """Persist all operator-controlled values needed to recreate an active link."""
    snapshot = await global_state.get_snapshot()
    studio_pfl = False
    studio_saved_volume = None
    if isinstance(interface, GoXLRInterface):
        studio_pfl, studio_saved_volume = interface.studio_pfl_state()
    desired_state.save({
        "target_ip": config["audio_network"]["target_ip"],
        "rx_channel_mode": snapshot.get("rx_channel_mode", "stereo"),
        "rx_volume": snapshot.get("rx_volume", 100),
        "headphone_volume": snapshot.get("headphone_volume", 255),
        "audio_interface": "GoXLR" if isinstance(interface, GoXLRInterface) else "Behringer",
        "studio_pfl": studio_pfl,
        "studio_saved_volume": studio_saved_volume,
    })


async def _restore_desired_link() -> bool:
    """Start the pipeline only when a previous operator requested it stay live."""
    global controller, interface
    desired = desired_state.load()
    if not desired or controller.is_active:
        return False
    wanted_interface = desired.get("audio_interface")
    if wanted_interface == "GoXLR" and not isinstance(interface, GoXLRInterface):
        return False
    target_ip = desired.get("target_ip")
    if isinstance(target_ip, str) and target_ip:
        config["audio_network"]["target_ip"] = target_ip
    mode = desired.get("rx_channel_mode", "stereo")
    if mode not in {"stereo", "left", "right"}:
        mode = "stereo"
    volume = desired.get("rx_volume", 100)
    if not isinstance(volume, int) or not 0 <= volume <= 100:
        volume = 100
    await global_state.update_metrics({
        "rx_channel_mode": mode,
        "rx_volume": volume,
        "headphone_volume": desired.get("headphone_volume", 255),
    })
    loop = asyncio.get_event_loop()
    controller = PipelineController(
        config, interface, rx_channel_mode=mode,
        rx_volume_pct=100 if isinstance(interface, GoXLRInterface) else volume,
        on_pipeline_fault=_on_pipeline_fault,
    )
    controller.start(loop)
    logging.getLogger("main").info("Restored requested codec link after service startup")
    return True


async def _cancel_task(task: Optional[asyncio.Task]) -> None:
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


_RX_WATCHDOG_S = 10.0
_full_reconnect_lock = asyncio.Lock()

# Settle time given to a device (e.g. a Behringer that just underwent a USB
# reset) to finish re-enumerating before we try to reopen its ALSA handle.
_PIPELINE_FAULT_SETTLE_S = 1.5
_pipeline_fault_lock = asyncio.Lock()
_pipeline_fault_retry_pending = False


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
        snapshot = await global_state.get_snapshot()
        # GoXLR uses its hardware LineOut master for Speaker Volume, so its
        # software RX gain must remain at unity. Behringer mode carries the
        # same UI percentage in the software gain instead.
        volume_pct = 100 if isinstance(interface, GoXLRInterface) else snapshot.get("rx_volume", 100)
        controller.stop(loop)
        await asyncio.sleep(0.3)
        controller = PipelineController(config, interface, rx_channel_mode=mode,
                                         rx_volume_pct=volume_pct,
                                         on_pipeline_fault=_on_pipeline_fault)
        controller.start(loop)


async def _on_pipeline_fault() -> None:
    """Called by a PipelineController on either of two distinct failure
    signatures that both need the same fix (tear down and rebuild every ALSA
    handle):

    1. A GST bus error indicating the underlying ALSA device (e.g. the
       Behringer) reset or dropped out from under it — see
       pipeline_manager._DEVICE_ERROR_PATTERNS. A USB reset here leaves the
       ALSA card entry in place (so the presence poll in _interface_monitor
       sees no change) while the open PCM handle becomes permanently
       unusable and the element that held it spins forever re-issuing the
       same failing ioctl — this is what flooded the journal overnight on
       2026-09-10 (see TROUBLESHOOTING.md "Behringer USB resets").
    2. The RX stall watchdog (pipeline_manager._rx_stall_watchdog) noticing
       the hardware sink went quiet while RX packets kept arriving, with no
       bus message at all — e.g. GStreamer's audiomixer silently wedging.
       See TROUBLESHOOTING.md "PFL stutters and goes silent ~90s after
       Bleep" (found 2026-09-10, self-recovered in ~90s before this existed).

    If another fault arrives while a reconnect triggered by this function is
    already running, it's coalesced into a single follow-up retry rather than
    stacking overlapping reconnects or being silently dropped.
    """
    global _pipeline_fault_retry_pending
    log = logging.getLogger("watchdog")
    if _pipeline_fault_lock.locked():
        _pipeline_fault_retry_pending = True
        log.warning("Pipeline-fault reconnect already in progress — will retry once more when it finishes")
        return
    async with _pipeline_fault_lock:
        while True:
            log.warning(f"Pipeline fault — reconnecting in {_PIPELINE_FAULT_SETTLE_S}s")
            await asyncio.sleep(_PIPELINE_FAULT_SETTLE_S)
            await _full_reconnect()
            await _sync_goxlr_state(interface)
            log.info("Automatic reconnect after pipeline fault complete")
            if not _pipeline_fault_retry_pending:
                break
            _pipeline_fault_retry_pending = False


async def _rx_watchdog():
    """Reconnect automatically when no RX packets arrive for _RX_WATCHDOG_S seconds.

    The Comrex drops its session on a TX gap. Briclite has no way to know this
    has happened other than noticing that the Comrex has stopped sending back.
    A new PipelineController produces a new random SSRC, which makes the Comrex
    treat the resumed stream as a fresh incoming call and re-establish.

    Goes through _full_reconnect() (properly serialised via _full_reconnect_lock)
    rather than rebuilding the pipeline inline. Until 2026-09-10 this function
    and _interface_monitor's hotswap path each mutated the shared global
    `controller` directly with no locking at all — only _full_reconnect() was
    protected. Under rapid concurrent triggers (confirmed live: a USB port
    swap firing the device-error path, the stall watchdog, and an interface
    hotswap within the same few seconds) they raced on that global, and this
    watchdog's own loop silently stopped detecting further outages — a real
    RX outage then went undetected for 100+ seconds with no automatic
    recovery until a human intervened. See TROUBLESHOOTING.md.
    """
    log = logging.getLogger("watchdog")
    while True:
        await asyncio.sleep(3.0)
        if not controller.is_active:
            continue
        elapsed = time.monotonic() - controller.last_rx_packet
        if elapsed > _RX_WATCHDOG_S:
            log.warning(f"No RX packets for {elapsed:.1f}s — auto-reconnecting")
            await _full_reconnect(controller.rx_channel_mode)
            await _sync_goxlr_state(interface)
            log.info("Auto-reconnect complete")


async def _interface_monitor():
    """Poll every 5 s and hot-swap the audio interface if GoXLR presence changes.

    Hot-swap only runs in auto mode; an explicit audio_interface setting is fixed.
    Independently of that, while in GoXLR mode this also watches for the
    Behringer (second mic) being plugged/unplugged and rebuilds just the RX
    pipeline to pick it up — that device is optional and must not require a
    full interface switch to recover.

    Compares live presence against controller.rx_extra_sources_active (what
    the *running* pipeline actually has) rather than a separately-tracked
    last-seen value here. A rebuild triggered by something else — notably
    on_pipeline_fault's auto-reconnect, which can land while the Behringer is
    mid-reset and rebuild without it — would otherwise leave a locally
    remembered flag stale and the Behringer branch silently missing from the
    mix from then on, even after the device comes back. Confirmed 2026-09-10
    by deliberately deauthorizing/reauthorizing the Behringer mid-session and
    watching this exact desync happen with the old (locally-tracked) logic.
    """
    global controller, interface
    log = logging.getLogger("main")
    _auto_mode = config.get("system", {}).get("audio_interface", "auto") == "auto"
    pfl_task: Optional[asyncio.Task] = None
    try:
        if isinstance(interface, GoXLRInterface):
            pfl_task = asyncio.create_task(interface.monitor_pfl())
        while True:
            await asyncio.sleep(5)

            if isinstance(interface, GoXLRInterface) and controller.is_active:
                now_present = GoXLRInterface.behringer_available()
                if now_present != controller.rx_extra_sources_active:
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
            interface = _make_interface(config)
            await _full_reconnect()
            mode = "GoXLR" if goxlr_now else "Behringer"
            await global_state.update_metrics({"audio_interface": mode})
            await _sync_goxlr_state(interface)
            log.info(f"Audio interface switched to {mode}")
            if isinstance(interface, GoXLRInterface):
                pfl_task = asyncio.create_task(interface.monitor_pfl())
            if desired_state.load() and not controller.is_active:
                await _restore_desired_link()
    finally:
        await _cancel_task(pfl_task)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, interface
    desired = desired_state.load()
    target_ip = desired.get("target_ip")
    if isinstance(target_ip, str) and target_ip:
        config["audio_network"]["target_ip"] = target_ip
    interface = _make_interface(config)
    controller = PipelineController(config, interface, on_pipeline_fault=_on_pipeline_fault)
    mode = "GoXLR" if isinstance(interface, GoXLRInterface) else "Behringer"
    await global_state.update_metrics({"audio_interface": mode})
    await _sync_goxlr_state(interface)
    if not desired:
        await global_state.update_metrics({"rx_volume": 100})
    else:
        await _restore_desired_link()
    monitor  = asyncio.create_task(_interface_monitor())
    watchdog = asyncio.create_task(_rx_watchdog())
    yield
    for task in (monitor, watchdog):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if controller.is_active:
        controller.stop(asyncio.get_event_loop())


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
    await _save_desired_link()
    snapshot = await global_state.get_snapshot()
    saved_mode = snapshot.get("rx_channel_mode", "stereo")
    # In GoXLR mode the Speaker slider controls the hardware LineOut master;
    # keep the software receive gain at unity to avoid applying both gains.
    saved_volume = 100 if isinstance(interface, GoXLRInterface) else snapshot.get("rx_volume", 100)
    controller = PipelineController(config, interface, rx_channel_mode=saved_mode,
                                     rx_volume_pct=saved_volume,
                                     on_pipeline_fault=_on_pipeline_fault)
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
    if desired_state.load():
        await _save_desired_link()
    return {"status": "success", "message": f"RX routing: {body.mode}"}


@app.post("/api/headphone_volume")
async def set_headphone_volume(body: HeadphoneVolumeRequest):
    if not isinstance(interface, GoXLRInterface):
        return {"status": "error", "message": "Not in GoXLR mode"}
    level = round(body.pct * 255 / 100)
    interface.set_headphone_volume(level)
    await global_state.update_metrics({"headphone_volume": level})
    if desired_state.load():
        await _save_desired_link()
    return {"status": "success"}


@app.post("/api/rx_volume")
async def set_rx_volume(body: RxVolumeRequest):
    if not 0 <= body.pct <= 100:
        return {"status": "error", "message": "pct must be 0–100"}
    if isinstance(interface, GoXLRInterface):
        interface.set_line_out_volume(round(body.pct * 255 / 100))
    else:
        controller.set_rx_volume(body.pct)
    await global_state.update_metrics({"rx_volume": body.pct})
    if desired_state.load():
        await _save_desired_link()
    return {"status": "success"}


@app.post("/api/disconnect")
async def disconnect_codec():
    loop = asyncio.get_event_loop()
    if not controller.is_active:
        return {"status": "error", "message": "Pipeline inactive"}
    desired_state.clear()
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

"""WebSocket command handlers.

One function per top-level command.  Each takes a `Context` bundle of
the shared state it needs (processor, rtsp_state, recorder, visca_cam,
initial_device) and the command body; returns the reply string.
"""
import json
import os

from jetson.streaming.coords import rotated_to_original
from jetson.control.visca_dispatch import dispatch_visca
from jetson.config import list_models
from jetson.utils import get_available_sources, probe_cam_formats


class Context:
    """Bundle of shared state that WS handlers need.  One instance per server."""

    def __init__(self, processor, rtsp_state, recorder, visca_cam, initial_device):
        self.processor      = processor
        self.rtsp_state     = rtsp_state
        self.recorder       = recorder
        self.visca_cam      = visca_cam
        self.initial_device = initial_device


# ---- Rotation labels (used in reply strings) ----
ROTATE_LABELS = {0: "0°", 1: "90° CW", 2: "180°", 3: "270° CW"}


def handle_message(ctx: Context, message: str) -> str:
    """Main command dispatch.  Returns the reply string to send back."""

    # -------- Rotation --------
    if message.startswith("rotate:"):
        try:
            method = int(message.split(":")[1])
            if 0 <= method <= 3:
                ctx.rtsp_state.rotation = method
                if ctx.rtsp_state.vflip:
                    ctx.rtsp_state.vflip.set_property("method", method)
                return f"Rotation set to {ROTATE_LABELS[method]}"
            return "Error: invalid rotation value"
        except ValueError:
            return "Error: invalid rotate command"

    # -------- Tracker control --------
    if message == "clear_track":
        ctx.processor.clear_track()
        return "Tracker cleared"
    if message == "pause":
        ctx.processor.pause()
        return "Paused"
    if message == "resume":
        ctx.processor.resume()
        return "Resumed"
    if message.startswith("detect:"):
        val = message.split(":", 1)[1].strip().lower()
        on = val in ("on", "1", "true")
        ctx.processor.enable_detector(on)
        return f"Detection {'ON' if on else 'OFF'}"

    # -------- Click / drag --------
    if message.startswith("click:"):
        try:
            _, coords = message.split(":", 1)
            nx, ny = map(float, coords.split(","))
            nx = max(0.0, min(1.0, nx))
            ny = max(0.0, min(1.0, ny))
            ox, oy = rotated_to_original(nx, ny, ctx.rtsp_state.rotation)
            ctx.processor.set_click(ox, oy)
            return f"Click set at ({ox:.3f}, {oy:.3f}) [original space]"
        except (ValueError, IndexError):
            return "Error: invalid click command"

    if message.startswith("drag:"):
        try:
            _, vals = message.split(":", 1)
            nx, ny, nw, nh = map(float, vals.split(","))
            ox1, oy1 = rotated_to_original(nx, ny, ctx.rtsp_state.rotation)
            ox2, oy2 = rotated_to_original(nx + nw, ny + nh, ctx.rtsp_state.rotation)
            onx = min(ox1, ox2)
            ony = min(oy1, oy2)
            onw = abs(ox2 - ox1)
            onh = abs(oy2 - oy1)
            ctx.processor.set_drag(onx, ony, onw, onh)
            return f"Drag set at ({onx:.3f},{ony:.3f} {onw:.3f}x{onh:.3f})"
        except (ValueError, IndexError):
            return "Error: invalid drag command"

    # -------- Box size --------
    if message.startswith("boxsize:"):
        try:
            val = message.split(":", 1)[1]
            if "," in val:
                w, h = map(int, val.split(","))
                ctx.processor.set_box_size(w, h)
                return f"Box size set to {w}x{h}"
            size = int(val)
            ctx.processor.set_box_size(size)
            return f"Box size set to {size}x{size}"
        except ValueError:
            return "Error: invalid boxsize command"

    # -------- Config --------
    if message == "get_config":
        cfg = ctx.processor.get_config()
        return f"config:{json.dumps(cfg)}"
    if message.startswith("set_param:"):
        try:
            body = message[len("set_param:"):]
            path, val = body.split("=", 1)
            new_val = ctx.processor.set_param(path.strip(), val.strip())
            return f"Param {path.strip()} = {new_val}"
        except Exception as e:
            return f"Error: set_param failed: {e}"
    if message == "save_default":
        try:
            ctx.processor.save_default()
            return "Saved default config"
        except Exception as e:
            return f"Error: save failed: {e}"

    # -------- Sources & models --------
    if message == "list_sources":
        sources = get_available_sources(ctx.initial_device)
        return f"sources:{json.dumps(sources)}"
    if message.startswith("source:"):
        path = message.split(":", 1)[1]
        if path.startswith("/dev/video") or os.path.isfile(path):
            ctx.rtsp_state.source_request = path
            return f"Source changed to {path}"
        return f"Error: source not found: {path}"
    if message == "list_models":
        models = list_models()
        current = ctx.processor.get_config().get("model", {}).get("yolo_path", "")
        return f"models:{json.dumps({'models': models, 'current': current})}"

    # -------- Camera format probe --------
    if message.startswith("list_cam_formats"):
        dev = ctx.initial_device
        formats, err = probe_cam_formats(dev)
        payload = {"device": dev, "formats": formats}
        if err:
            payload["error"] = err
        return f"cam_formats:{json.dumps(payload)}"

    if message.startswith("cam_format:"):
        try:
            val = message.split(":", 1)[1].strip()
            if val == "auto":
                ctx.processor.set_param("camera.width", "0")
                ctx.processor.set_param("camera.height", "0")
                ctx.processor.set_param("camera.fps", "0")
            else:
                res, fps = val.split("@")
                cw, ch = res.split("x")
                ctx.processor.set_param("camera.width", cw)
                ctx.processor.set_param("camera.height", ch)
                ctx.processor.set_param("camera.fps", fps)
            ctx.rtsp_state.source_request = ctx.initial_device
            return f"Camera format set to {val} (restarting capture...)"
        except Exception as e:
            return f"Error: invalid cam_format: {e}"

    # -------- VISCA --------
    if message.startswith("visca:"):
        if ctx.visca_cam is None or not ctx.visca_cam.is_open:
            return "Error: VISCA camera not connected"
        return dispatch_visca(ctx.visca_cam, message[6:])

    # -------- Recording --------
    if message == "record:start":
        ctx.recorder.start()
        return f"Recording started: {ctx.recorder.directory}"
    if message == "record:stop":
        ctx.recorder.stop()
        return f"Recording stopped: {ctx.recorder.directory}"

    return f"Command received: {message}"

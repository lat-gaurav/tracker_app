import gi
import os
import sys
import signal
import asyncio
import threading
import time
import json
import glob
import cv2
import numpy as np

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib

import subprocess
import yaml
import websockets

from processor import FrameProcessor
from visca import VISCACamera

WS_PORT = 5001

DEVICE    = "/dev/video4"
PORT      = "8554"
MOUNT     = "/stream"
IP        = "192.168.144.102"
STREAM_W  = 1920       # fixed streaming canvas size
STREAM_H  = 1080       # content is centered with black padding if smaller
FPS       = 30

# Shared state
processor  = FrameProcessor()

# VISCA camera control (optional — gracefully skips if not connected)
visca_cam = None
VISCA_PORT = "/dev/ttyACM0"
VISCA_BAUD = 9600
try:
    visca_cam = VISCACamera(VISCA_PORT, VISCA_BAUD)
    visca_cam.open()
except Exception as e:
    print(f"[VISCA] Camera not available: {e} — camera control disabled")
_appsrc    = None
_vflip     = None
_rotation  = 0            # current videoflip method (0–3)
_cap_pipe  = None         # capture GStreamer pipeline
_source_request = None    # set by WS thread to trigger source switch

# ---- Recording state ----
_rec_active    = False
_rec_dir       = None
_rec_proc      = None     # ffmpeg subprocess (stdin receives raw BGR frames)
_rec_overlay   = None     # overlay.jsonl file handle
_rec_inputs    = None     # inputs.jsonl file handle
_rec_start_ts  = 0.0
_rec_frame_n   = 0


import queue as _queue_mod

_rec_queue = None   # queue.Queue for the recording thread


def _rec_writer_thread():
    """Background thread: pulls (frame_bytes, overlay_json) from the queue
    and writes to ffmpeg + overlay file.  Runs until it receives None."""
    global _rec_frame_n
    while True:
        q = _rec_queue
        if q is None:
            break
        try:
            item = q.get(timeout=0.5)
        except Exception:
            continue
        if item is None:
            break
        frame_bytes, overlay_json = item
        try:
            if _rec_proc and _rec_proc.stdin:
                _rec_proc.stdin.write(frame_bytes)
            _rec_frame_n += 1
            if _rec_overlay:
                _rec_overlay.write(overlay_json + "\n")
                _rec_overlay.flush()
        except Exception as e:
            print(f"[REC]   writer error: {e}")


def _start_recording():
    global _rec_active, _rec_dir, _rec_proc, _rec_overlay, _rec_inputs
    global _rec_start_ts, _rec_frame_n, _rec_queue
    if _rec_active:
        return
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    _rec_dir = f"recordings/{ts}"
    os.makedirs(_rec_dir, exist_ok=True)

    # Config snapshot
    cfg = processor.get_config()
    cfg.pop("_runtime", None)
    with open(f"{_rec_dir}/config.yaml", "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    # ffmpeg — raw BGR stdin → H.264 elementary stream file
    video_path = f"{_rec_dir}/video.h264"
    _rec_proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{STREAM_W}x{STREAM_H}", "-r", str(FPS),
         "-i", "-",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-crf", "23", "-f", "h264", video_path],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    _rec_overlay = open(f"{_rec_dir}/overlay.jsonl", "a")
    _rec_inputs  = open(f"{_rec_dir}/inputs.jsonl",  "a")
    _rec_start_ts = time.time()
    _rec_frame_n  = 0

    # Start writer thread (handles the slow pipe + file I/O off the capture thread)
    _rec_queue = _queue_mod.Queue(maxsize=60)  # ~2s buffer at 30fps
    threading.Thread(target=_rec_writer_thread, daemon=True).start()

    _rec_active = True
    print(f"[REC]   Started → {_rec_dir}")


def _stop_recording():
    global _rec_active, _rec_proc, _rec_overlay, _rec_inputs, _rec_queue
    if not _rec_active:
        return
    _rec_active = False
    # Signal writer thread to exit and wait for it to drain
    if _rec_queue:
        try:
            _rec_queue.put(None, timeout=1)
        except Exception:
            pass
        time.sleep(0.5)   # let writer thread process remaining items + exit
    _rec_queue = None
    if _rec_proc:
        try:
            _rec_proc.stdin.close()
            _rec_proc.wait(timeout=5)
        except Exception:
            _rec_proc.kill()
        _rec_proc = None
    if _rec_overlay:
        _rec_overlay.close()
        _rec_overlay = None
    if _rec_inputs:
        _rec_inputs.close()
        _rec_inputs = None
    print(f"[REC]   Stopped → {_rec_dir}")


def get_available_sources():
    cameras = sorted([f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("video")])
    videos = sorted(glob.glob("video_test/*.mp4"))
    return {"cameras": cameras, "videos": videos, "current": DEVICE}


def _fit_to_canvas(frame):
    """Center `frame` inside a STREAM_W x STREAM_H canvas with black padding.
    If the frame is larger than the canvas, scale down preserving aspect ratio.
    If smaller, leave at native pixel resolution (no upscale)."""
    h, w = frame.shape[:2]
    if w > STREAM_W or h > STREAM_H:
        scale = min(STREAM_W / w, STREAM_H / h)
        new_w = int(w * scale) & ~1
        new_h = int(h * scale) & ~1
        frame = cv2.resize(frame, (new_w, new_h))
        h, w = new_h, new_w
    if w == STREAM_W and h == STREAM_H:
        return frame
    canvas = np.zeros((STREAM_H, STREAM_W, 3), dtype=np.uint8)
    dx = (STREAM_W - w) // 2
    dy = (STREAM_H - h) // 2
    canvas[dy:dy+h, dx:dx+w] = frame
    return canvas


def _build_cam_pipeline(device):
    """Return a GStreamer pipeline string for a V4L2 camera.
    If camera.width/height/fps are set in config (non-zero), request that
    exact mode from the sensor.  Otherwise auto-negotiate.  Either way,
    the output is scaled to STREAM_W x STREAM_H @ FPS for the encoder."""
    cam_cfg = processor.cfg.get("camera", {})
    cw = int(cam_cfg.get("width", 0))
    ch = int(cam_cfg.get("height", 0))
    cf = int(cam_cfg.get("fps", 0))

    if cw > 0 and ch > 0 and cf > 0:
        # Explicit sensor mode requested
        src_caps = f'video/x-raw,width={cw},height={ch},framerate={cf}/1 ! '
        print(f"[CAP]   Camera caps: {cw}x{ch}@{cf}")
    else:
        src_caps = ''
        print(f"[CAP]   Camera caps: auto-negotiate")

    return (
        f'v4l2src device={device} ! '
        f'{src_caps}'
        'videoconvert ! videoscale ! '
        f'video/x-raw,format=BGR,width={STREAM_W},height={STREAM_H} ! '
        f'videorate ! video/x-raw,framerate={FPS}/1 ! '
        'appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false'
    )


def _handle_visca(cmd):
    """Parse and execute a VISCA sub-command. Returns reply string."""
    parts = cmd.split(":")
    action = parts[0]
    arg = int(parts[1]) if len(parts) > 1 else 0
    try:
        # Zoom
        if action == "zoom_tele":       visca_cam.zoom_tele(arg)
        elif action == "zoom_wide":     visca_cam.zoom_wide(arg)
        elif action == "zoom_stop":     visca_cam.zoom_stop()
        elif action == "zoom_direct":   visca_cam.zoom_direct(arg)
        elif action == "zoom_pos":
            pos = visca_cam.zoom_position_inq()
            return f"visca_reply:zoom_pos={pos}"
        elif action == "dzoom_on":      visca_cam.dzoom_on()
        elif action == "dzoom_off":     visca_cam.dzoom_off()
        # Focus
        elif action == "focus_auto":    visca_cam.focus_auto()
        elif action == "focus_manual":  visca_cam.focus_manual()
        elif action == "focus_far":     visca_cam.focus_far(arg)
        elif action == "focus_near":    visca_cam.focus_near(arg)
        elif action == "focus_stop":    visca_cam.focus_stop()
        elif action == "focus_one_push": visca_cam.focus_one_push()
        elif action == "focus_direct":  visca_cam.focus_direct(arg)
        elif action == "focus_pos":
            pos = visca_cam.focus_position_inq()
            return f"visca_reply:focus_pos={pos}"
        # Exposure
        elif action == "ae_auto":       visca_cam.ae_full_auto()
        elif action == "ae_manual":     visca_cam.ae_manual()
        elif action == "ae_shutter":    visca_cam.ae_shutter_priority()
        elif action == "ae_iris":       visca_cam.ae_iris_priority()
        elif action == "shutter":       visca_cam.shutter_direct(arg)
        elif action == "iris":          visca_cam.iris_direct(arg)
        elif action == "gain":          visca_cam.gain_direct(arg)
        elif action == "exp_comp_on":   visca_cam.exp_comp_on()
        elif action == "exp_comp_off":  visca_cam.exp_comp_off()
        elif action == "exp_comp":      visca_cam.exp_comp_direct(arg)
        elif action == "backlight_on":  visca_cam.backlight_on()
        elif action == "backlight_off": visca_cam.backlight_off()
        # White balance
        elif action == "wb_auto":       visca_cam.wb_auto()
        elif action == "wb_indoor":     visca_cam.wb_indoor()
        elif action == "wb_outdoor":    visca_cam.wb_outdoor()
        elif action == "wb_atw":        visca_cam.wb_atw()
        elif action == "wb_manual":     visca_cam.wb_manual()
        elif action == "wb_one_push":   visca_cam.wb_one_push_trigger()
        elif action == "rgain":         visca_cam.rgain_direct(arg)
        elif action == "bgain":         visca_cam.bgain_direct(arg)
        # Image processing
        elif action == "stabilizer_on":   visca_cam.stabilizer_on()
        elif action == "stabilizer_off":  visca_cam.stabilizer_off()
        elif action == "stabilizer_hold": visca_cam.stabilizer_hold()
        elif action == "wdr_on":        visca_cam.wdr_on()
        elif action == "wdr_off":       visca_cam.wdr_off()
        elif action == "ve_on":         visca_cam.ve_on()
        elif action == "defog_on":      visca_cam.defog_on(arg)
        elif action == "defog_off":     visca_cam.defog_off()
        elif action == "nr":            visca_cam.nr_direct(arg)
        elif action == "aperture":      visca_cam.aperture_direct(arg)
        elif action == "high_sens_on":  visca_cam.high_sensitivity_on()
        elif action == "high_sens_off": visca_cam.high_sensitivity_off()
        # ICR (day/night)
        elif action == "icr_on":        visca_cam.icr_on()
        elif action == "icr_off":       visca_cam.icr_off()
        elif action == "auto_icr_on":   visca_cam.auto_icr_on()
        elif action == "auto_icr_off":  visca_cam.auto_icr_off()
        # Other
        elif action == "flip_on":       visca_cam.picture_flip_on()
        elif action == "flip_off":      visca_cam.picture_flip_off()
        elif action == "mirror_on":     visca_cam.lr_reverse_on()
        elif action == "mirror_off":    visca_cam.lr_reverse_off()
        elif action == "freeze_on":     visca_cam.freeze_on()
        elif action == "freeze_off":    visca_cam.freeze_off()
        elif action == "bw_on":         visca_cam.bw_on()
        elif action == "bw_off":        visca_cam.bw_off()
        # Presets
        elif action == "preset_save":   visca_cam.memory_set(arg)
        elif action == "preset_recall": visca_cam.memory_recall(arg)
        elif action == "preset_reset":  visca_cam.memory_reset(arg)
        # System
        elif action == "lens_init":     visca_cam.lens_init()
        elif action == "cam_reset":     visca_cam.camera_reset()
        elif action == "power_on":      visca_cam.power_on()
        elif action == "power_off":     visca_cam.power_off()
        else:
            return f"Error: unknown VISCA command: {action}"
        return f"VISCA OK: {action}"
    except Exception as e:
        return f"VISCA Error: {e}"


def check_device(device):
    """Check if the default camera exists.  If not, try to fall back to the
    first available /dev/video* device instead of exiting."""
    global DEVICE
    if os.path.exists(device):
        print(f"[OK]    Camera device {device} found.")
        return
    available = sorted([f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("video")])
    print(f"[WARN]  Default camera {device} not found.")
    print(f"[INFO]  Available video devices: {available}")
    if available:
        DEVICE = available[0]
        print(f"[INFO]  Falling back to {DEVICE}")
    else:
        print(f"[WARN]  No cameras found — start anyway; select a source from ground.")


# ------------------------------------------------------------------ coord conversion

def rotated_to_original(nx, ny, method):
    """Convert normalized coords in rotated-video space to original-frame space."""
    if method == 0: return nx, ny
    if method == 1: return ny, 1 - nx       # 90° CW
    if method == 2: return 1 - nx, 1 - ny   # 180°
    if method == 3: return 1 - ny, nx        # 270° CW
    return nx, ny


# ------------------------------------------------------------------ capture thread

def capture_and_push():
    """
    Captures frames, runs them through the FrameProcessor, and pushes
    the result into the RTSP appsrc.
    - Cameras: GStreamer v4l2src pipeline (low-latency, drop stale frames)
    - Video files: cv2.VideoCapture (handles any codec, any resolution)
    Runs in its own daemon thread.
    """
    global _cap_pipe, _appsrc, _source_request, _rec_frame_n

    current_source = DEVICE
    cap_cv   = None   # cv2.VideoCapture for video files
    gst_sink = None   # GStreamer appsink for cameras

    def _stop():
        nonlocal cap_cv, gst_sink
        global _cap_pipe
        if _cap_pipe is not None:
            _cap_pipe.set_state(Gst.State.NULL)
            _cap_pipe = None
        if cap_cv is not None:
            cap_cv.release()
            cap_cv = None
        gst_sink = None

    def _start(source):
        nonlocal cap_cv, gst_sink
        global _cap_pipe
        _stop()
        if source.startswith("/dev/video"):
            if not os.path.exists(source):
                print(f"[CAP]   Device {source} not found — waiting for source change from ground")
                return
            try:
                _cap_pipe = Gst.parse_launch(_build_cam_pipeline(source))
                gst_sink = _cap_pipe.get_by_name('sink')
                _cap_pipe.set_state(Gst.State.PLAYING)
            except Exception as e:
                print(f"[CAP]   Camera pipeline failed: {e}")
                _cap_pipe = None
                gst_sink = None
                return
            cap_cv = None
            print(f"[CAP]   Camera started: {source}")
        else:
            cap_cv = cv2.VideoCapture(source)
            if not cap_cv.isOpened():
                print(f"[CAP]   ERROR: cannot open {source}")
                cap_cv = None
                return
            src_w = int(cap_cv.get(cv2.CAP_PROP_FRAME_WIDTH))
            src_h = int(cap_cv.get(cv2.CAP_PROP_FRAME_HEIGHT))
            src_fps = cap_cv.get(cv2.CAP_PROP_FPS) or 30
            print(f"[CAP]   Video file opened: {source} ({src_w}x{src_h} @ {src_fps:.0f}fps)")
            _cap_pipe = None
            gst_sink = None

    def _read_frame():
        """Returns a BGR frame at WIDTH x HEIGHT, or None."""
        if gst_sink is not None:
            sample = gst_sink.emit('try-pull-sample', 200 * 1000000)
            if sample is None:
                return None
            buf = sample.get_buffer()
            caps = sample.get_caps()
            s = caps.get_structure(0)
            w = s.get_value('width')
            h = s.get_value('height')
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3)).copy()
            buf.unmap(mapinfo)
            return frame
        elif cap_cv is not None:
            ok, frame = cap_cv.read()
            if not ok:
                # Loop: rewind to start
                cap_cv.set(cv2.CAP_PROP_POS_FRAMES, 0)
                print("[CAP]   Video looped")
                return None
            return frame
        return None

    _start(current_source)

    frame_duration_ns = int(1e9 / FPS)
    frame_interval    = 1.0 / FPS
    pts       = 0
    frame_n   = 0
    _first    = True

    while True:
        t_start = time.monotonic()

        # Hot-switch source if requested
        if _source_request is not None:
            new_source = _source_request
            _source_request = None
            print(f"[CAP]   Switching source: {current_source} -> {new_source}")
            processor.clear_track()
            current_source = new_source
            _start(current_source)
            pts = 0
            frame_n = 0
            _first = True
            continue

        frame = _read_frame()
        if frame is None:
            continue

        if _first:
            h, w = frame.shape[:2]
            print(f"[CAP]   First frame: {w}x{h} -> canvas {STREAM_W}x{STREAM_H}")
            _first = False

        # Fit into fixed canvas (centers + pads small content, scales down large content)
        frame = _fit_to_canvas(frame)

        processor.submit_frame(frame)

        # ---- Pause-and-select: decide which frame goes to display ----
        paused, catching_up, frozen_frame, cu_frame = processor.get_display_state()
        if catching_up:
            if cu_frame is not None:
                display_frame = cu_frame.copy()
                display_frame = processor.draw(display_frame)
            elif frozen_frame is not None:
                display_frame = frozen_frame.copy()
                display_frame = processor.draw(display_frame, paused_view=True)
            else:
                display_frame = processor.draw(frame)
            cv2.putText(display_frame, "CATCHING UP",
                        (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                        (0, 165, 255), 2, cv2.LINE_AA)
        elif paused:
            if frozen_frame is not None:
                display_frame = frozen_frame.copy()
            else:
                display_frame = frame.copy()
            display_frame = processor.draw(display_frame, paused_view=True)
            cv2.putText(display_frame, "PAUSED",
                        (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                        (0, 0, 255), 2, cv2.LINE_AA)
        else:
            display_frame = processor.draw(frame)

        # ---- Recording: enqueue clean frame + overlay snapshot (non-blocking) ----
        if _rec_active and _rec_queue is not None:
            try:
                with processor._lock:
                    ov = {
                        "ts": time.time(),
                        "frame": _rec_frame_n + 1,
                        "frame_seq": processor._frame_seq,
                        "tracker_bbox": list(processor._bbox) if processor._bbox else None,
                        "tracker_bbox_seq": processor._bbox_seq,
                        "track_ms": round(processor._track_ms, 1),
                        "ai_active": processor._track_count < processor._ai_assist_until_count,
                        "det_seq": processor._det_seq,
                        "detections": [
                            {"label": d["label"],
                             "conf": round(d["conf"], 3),
                             "aabb": list(cv2.boundingRect(d["poly"].astype(np.int32)))}
                            for d in processor._detections
                        ],
                    }
                # put_nowait: if queue is full (ffmpeg too slow), drop the frame
                # rather than blocking the capture loop
                _rec_queue.put_nowait((frame.tobytes(), json.dumps(ov)))
            except _queue_mod.Full:
                pass   # drop frame — ffmpeg can't keep up
            except Exception as e:
                print(f"[REC]   enqueue error: {e}")

        # Push processed frame to appsrc
        if _appsrc is not None:
            out_buf = Gst.Buffer.new_wrapped(display_frame.tobytes())
            out_buf.pts      = pts
            out_buf.duration = frame_duration_ns
            pts += frame_duration_ns
            ret = _appsrc.emit('push-buffer', out_buf)
            if ret == Gst.FlowReturn.FLUSHING or ret == Gst.FlowReturn.ERROR:
                print(f"[CAP]   push-buffer returned {ret} — pipeline gone, clearing appsrc")
                _appsrc = None
                pts = 0
            elif frame_n % 150 == 0:
                print(f"[CAP]   frame={frame_n} push-buffer ret={ret}")
        else:
            if frame_n % 150 == 0:
                print(f"[CAP]   frame={frame_n} appsrc=None (waiting for client)")

        frame_n += 1

        # Pace video files to ~30fps (cameras are paced by the sensor)
        if cap_cv is not None:
            elapsed = time.monotonic() - t_start
            sleep_s = frame_interval - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)


# ------------------------------------------------------------------ GStreamer bus callbacks

def on_bus_error(_bus, message):
    err, debug = message.parse_error()
    print(f"[ERROR] GStreamer: {err.message}")
    if debug:
        print(f"[DEBUG] {debug}")


def on_bus_warning(_bus, message):
    warn, debug = message.parse_warning()
    print(f"[WARN]  GStreamer: {warn.message}")
    if debug:
        print(f"[DEBUG] {debug}")


def on_bus_state_changed(_bus, message):
    if not message.src.get_name().startswith("pipeline"):
        return
    old, new, _ = message.parse_state_changed()
    old_name = Gst.Element.state_get_name(old)
    new_name = Gst.Element.state_get_name(new)
    if old_name != new_name:
        print(f"[STATE] Pipeline: {old_name} -> {new_name}")


# ------------------------------------------------------------------ RTSP callbacks

def on_media_configure(_factory, media):
    global _appsrc, _vflip
    media.set_property('suspend-mode', GstRtspServer.RTSPSuspendMode.NONE)
    pipeline = media.get_element()
    _appsrc  = pipeline.get_by_name('src')
    _vflip   = pipeline.get_by_name('vflip')
    # Apply current rotation in case client reconnects after rotation was set
    if _vflip:
        _vflip.set_property('method', _rotation)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", on_bus_error)
    bus.connect("message::warning", on_bus_warning)
    bus.connect("message::state-changed", on_bus_state_changed)
    print("[INFO]  Client connected — RTSP pipeline starting.")


def on_client_connected(_server, _client):
    print("[INFO]  New RTSP client connected.")


def on_client_closed(_client):
    # Do NOT null _appsrc here.
    # With suspend-mode=NONE the pipeline stays in PLAYING — the capture thread
    # must keep pushing frames so the encoder stays warm.  The next client will
    # reuse the same pipeline and see instant video without re-initialization.
    # _appsrc is only nulled if push-buffer itself signals a pipeline failure.
    print("[INFO]  RTSP client disconnected — pipeline stays hot.")


# ------------------------------------------------------------------ WebSocket server

ROTATE_LABELS = {0: "0°", 1: "90° CW", 2: "180°", 3: "270° CW"}


async def ws_handle(websocket):
    global _vflip, _rotation, _source_request
    addr = websocket.remote_address
    print(f"[WS]   Client connected: {addr}")

    async def status_sender():
        """Push tracking + detection stats to ground once per second."""
        _was_paused = False
        try:
            while True:
                await asyncio.sleep(1.0)
                paused_now, _, catching_up = processor.is_paused()
                if _was_paused and not paused_now:
                    await websocket.send("status:resumed")
                _was_paused = paused_now or catching_up
                tracking, t_ms = processor.get_track_info()
                if tracking:
                    await websocket.send(f"status:tracking {t_ms:.1f}ms")
                # Push auto-box-size notification if set
                with processor._lock:
                    auto_box = processor._auto_box_notify
                    processor._auto_box_notify = None
                if auto_box:
                    await websocket.send(f"status:boxsize_auto {auto_box[0]},{auto_box[1]}")
                # Recording status
                if _rec_active:
                    elapsed = int(time.time() - _rec_start_ts)
                    mm, ss = divmod(elapsed, 60)
                    await websocket.send(f"status:recording {mm:02d}:{ss:02d} frames={_rec_frame_n}")
                lost = processor.get_last_lost_reason()
                if lost:
                    await websocket.send(f"status:lost {lost}")
                det_on, d_ms, d_count, d_err = processor.get_det_info()
                if det_on:
                    if d_err:
                        await websocket.send(f"status:det_error {d_err}")
                    else:
                        await websocket.send(f"status:detection {d_ms:.1f}ms objects={d_count}")
        except asyncio.CancelledError:
            pass

    task = asyncio.ensure_future(status_sender())
    try:
        async for message in websocket:
            print(f"[WS]   Received: {message}")

            if message.startswith("rotate:"):
                try:
                    method = int(message.split(":")[1])
                    if 0 <= method <= 3:
                        _rotation = method
                        if _vflip:
                            _vflip.set_property("method", method)
                        reply = f"Rotation set to {ROTATE_LABELS[method]}"
                    else:
                        reply = "Error: invalid rotation value"
                except ValueError:
                    reply = "Error: invalid rotate command"

            elif message == "clear_track":
                processor.clear_track()
                reply = "Tracker cleared"

            elif message == "pause":
                processor.pause()
                reply = "Paused"

            elif message == "resume":
                processor.resume()
                reply = "Resumed"

            elif message.startswith("detect:"):
                val = message.split(":", 1)[1].strip().lower()
                on = val in ("on", "1", "true")
                processor.enable_detector(on)
                reply = f"Detection {'ON' if on else 'OFF'}"

            elif message.startswith("list_cam_formats"):
                # Probe current camera for supported formats
                import subprocess
                try:
                    dev = DEVICE  # current camera device
                    # Try to use the source-selected device
                    if hasattr(capture_and_push, '_current_source_ref'):
                        dev = capture_and_push._current_source_ref
                    out = subprocess.check_output(
                        ["v4l2-ctl", "--list-formats-ext", "-d", dev],
                        stderr=subprocess.STDOUT, timeout=5
                    ).decode()
                    # Parse into list of "WxH @ FPS" entries
                    formats = []
                    cur_w = cur_h = 0
                    for line in out.splitlines():
                        line = line.strip()
                        if "Size:" in line and "Discrete" in line:
                            # e.g. "Size: Discrete 1280x800"
                            parts = line.split()
                            res = parts[-1]  # "1280x800"
                            w, h = res.split("x")
                            cur_w, cur_h = int(w), int(h)
                        elif "Interval:" in line and "(" in line:
                            # e.g. "Interval: Discrete 0.033s (30.000 fps)"
                            fps_str = line.split("(")[1].split("fps")[0].strip()
                            fps = float(fps_str)
                            entry = f"{cur_w}x{cur_h}@{fps:.0f}"
                            if entry not in formats:
                                formats.append(entry)
                    reply = f"cam_formats:{json.dumps({'device': dev, 'formats': formats})}"
                except Exception as e:
                    reply = f"cam_formats:{json.dumps({'device': '', 'formats': [], 'error': str(e)})}"

            elif message.startswith("cam_format:"):
                # e.g. cam_format:1280x800@30  or  cam_format:auto
                try:
                    val = message.split(":", 1)[1].strip()
                    if val == "auto":
                        processor.set_param("camera.width", "0")
                        processor.set_param("camera.height", "0")
                        processor.set_param("camera.fps", "0")
                    else:
                        res, fps = val.split("@")
                        cw, ch = res.split("x")
                        processor.set_param("camera.width", cw)
                        processor.set_param("camera.height", ch)
                        processor.set_param("camera.fps", fps)
                    # Trigger capture pipeline restart to apply the new format
                    _source_request = DEVICE
                    reply = f"Camera format set to {val} (restarting capture...)"
                except Exception as e:
                    reply = f"Error: invalid cam_format: {e}"

            elif message == "list_models":
                from processor import list_models
                models = list_models()
                current = processor.get_config().get("model", {}).get("yolo_path", "")
                reply = f"models:{json.dumps({'models': models, 'current': current})}"

            elif message == "get_config":
                cfg = processor.get_config()
                reply = f"config:{json.dumps(cfg)}"

            elif message.startswith("set_param:"):
                try:
                    body = message[len("set_param:"):]
                    path, val = body.split("=", 1)
                    new_val = processor.set_param(path.strip(), val.strip())
                    reply = f"Param {path.strip()} = {new_val}"
                except Exception as e:
                    reply = f"Error: set_param failed: {e}"

            elif message == "save_default":
                try:
                    processor.save_default()
                    reply = "Saved default config"
                except Exception as e:
                    reply = f"Error: save failed: {e}"

            elif message == "list_sources":
                sources = get_available_sources()
                reply = f"sources:{json.dumps(sources)}"

            elif message.startswith("source:"):
                path = message.split(":", 1)[1]
                if path.startswith("/dev/video") or os.path.isfile(path):
                    _source_request = path
                    reply = f"Source changed to {path}"
                else:
                    reply = f"Error: source not found: {path}"

            elif message.startswith("boxsize:"):
                try:
                    val = message.split(":", 1)[1]
                    if "," in val:
                        w, h = map(int, val.split(","))
                        processor.set_box_size(w, h)
                        reply = f"Box size set to {w}x{h}"
                    else:
                        size = int(val)
                        processor.set_box_size(size)
                        reply = f"Box size set to {size}x{size}"
                except ValueError:
                    reply = "Error: invalid boxsize command"

            elif message.startswith("click:"):
                try:
                    _, coords = message.split(":", 1)
                    nx, ny = map(float, coords.split(","))
                    nx = max(0.0, min(1.0, nx))
                    ny = max(0.0, min(1.0, ny))
                    ox, oy = rotated_to_original(nx, ny, _rotation)
                    print(f"[WS]   click: rotated=({nx:.3f},{ny:.3f}) rotation={_rotation} -> original=({ox:.3f},{oy:.3f})")
                    processor.set_click(ox, oy)
                    print(f"[WS]   processor.set_click({ox:.3f}, {oy:.3f}) called")
                    reply = f"Click set at ({ox:.3f}, {oy:.3f}) [original space]"
                except (ValueError, IndexError):
                    reply = "Error: invalid click command"

            elif message.startswith("drag:"):
                try:
                    _, vals = message.split(":", 1)
                    nx, ny, nw, nh = map(float, vals.split(","))
                    ox1, oy1 = rotated_to_original(nx, ny, _rotation)
                    ox2, oy2 = rotated_to_original(nx + nw, ny + nh, _rotation)
                    onx = min(ox1, ox2)
                    ony = min(oy1, oy2)
                    onw = abs(ox2 - ox1)
                    onh = abs(oy2 - oy1)
                    processor.set_drag(onx, ony, onw, onh)
                    reply = f"Drag set at ({onx:.3f},{ony:.3f} {onw:.3f}x{onh:.3f})"
                except (ValueError, IndexError):
                    reply = "Error: invalid drag command"

            elif message.startswith("visca:"):
                if visca_cam is None or not visca_cam.is_open:
                    reply = "Error: VISCA camera not connected"
                else:
                    reply = _handle_visca(message[6:])

            elif message == "record:start":
                _start_recording()
                reply = f"Recording started: {_rec_dir}"

            elif message == "record:stop":
                _stop_recording()
                reply = f"Recording stopped: {_rec_dir}"

            else:
                reply = f"Command received: {message}"

            # Log every WS command to the recording input file
            if _rec_active and _rec_inputs:
                try:
                    _rec_inputs.write(json.dumps({"ts": time.time(), "cmd": message}) + "\n")
                    _rec_inputs.flush()
                except Exception:
                    pass

            await websocket.send(reply)
            print(f"[WS]   Sent: {reply}")
    except websockets.exceptions.ConnectionClosed:
        print(f"[WS]   Client disconnected: {addr}")
    finally:
        task.cancel()


async def ws_main():
    async with websockets.serve(ws_handle, "0.0.0.0", WS_PORT):
        print(f"[OK]    WebSocket server ready on port {WS_PORT}")
        await asyncio.Future()   # run forever


def start_ws_server():
    asyncio.run(ws_main())


# ------------------------------------------------------------------ main

def main():
    Gst.init(None)
    check_device(DEVICE)

    # Start capture thread
    cap_thread = threading.Thread(target=capture_and_push, daemon=True)
    cap_thread.start()

    # Start WebSocket server thread
    ws_thread = threading.Thread(target=start_ws_server, daemon=True)
    ws_thread.start()

    loop   = GLib.MainLoop()
    signal.signal(signal.SIGINT, lambda s, f: (print("\n[INFO]  Shutting down..."), loop.quit()))

    server = GstRtspServer.RTSPServer()
    server.set_service(PORT)
    server.connect("client-connected", on_client_connected)

    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_launch(
        f'( appsrc name=src is-live=true format=time block=false max-buffers=1 leaky-type=2 '
        f'caps=video/x-raw,format=BGR,width={STREAM_W},height={STREAM_H},framerate={FPS}/1 ! '
        'videoconvert ! video/x-raw,format=I420 ! '
        'videoflip name=vflip method=0 ! '
        'nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! '
        'nvv4l2h264enc bitrate=1500000 iframeinterval=30 preset-level=1 insert-sps-pps=1 ! '
        'h264parse config-interval=1 ! '
        'rtph264pay name=pay0 pt=96 aggregate-mode=zero-latency )'
    )
    factory.set_shared(True)
    factory.set_latency(0)
    factory.set_property('suspend-mode', 0)  # keep pipeline alive with no clients — encoder stays initialized
    factory.connect("media-configure", on_media_configure)

    server.get_mount_points().add_factory(MOUNT, factory)
    server.attach(None)

    print(f"[OK]    RTSP stream ready at: rtsp://{IP}:{PORT}{MOUNT}")
    print(f"[INFO]  Press Ctrl+C to stop.\n")

    loop.run()
    print("[INFO]  Server stopped.")

    # Force exit — daemon threads (capture, tracker, websocket) won't block,
    # but GLib/GStreamer may have lingering callbacks that prevent clean shutdown.
    import os
    os._exit(0)


if __name__ == "__main__":
    main()

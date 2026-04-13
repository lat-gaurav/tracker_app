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

import websockets

from processor import FrameProcessor

WS_PORT = 5001

DEVICE    = "/dev/video4"
PORT      = "8554"
MOUNT     = "/stream"
IP        = "192.168.144.102"
CAM_W     = 1280       # camera native resolution
CAM_H     = 800
STREAM_W  = 1920       # fixed streaming canvas size
STREAM_H  = 1080       # content is centered with black padding if smaller
FPS       = 30

# Shared state
processor  = FrameProcessor()
_appsrc    = None
_vflip     = None
_rotation  = 0            # current videoflip method (0–3)
_cap_pipe  = None         # capture GStreamer pipeline
_source_request = None    # set by WS thread to trigger source switch


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
    """Return a GStreamer pipeline string for a V4L2 camera."""
    return (
        f'v4l2src device={device} ! '
        f'video/x-raw,width={CAM_W},height={CAM_H},framerate={FPS}/1 ! '
        'videoconvert ! video/x-raw,format=BGR ! '
        'appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false'
    )


def check_device(device):
    if not os.path.exists(device):
        available = [f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("video")]
        print(f"[ERROR] Camera device {device} not found!")
        print(f"[INFO]  Available video devices: {sorted(available)}")
        sys.exit(1)
    print(f"[OK]    Camera device {device} found.")


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
    global _cap_pipe, _appsrc, _source_request

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
            _cap_pipe = Gst.parse_launch(_build_cam_pipeline(source))
            gst_sink = _cap_pipe.get_by_name('sink')
            _cap_pipe.set_state(Gst.State.PLAYING)
            cap_cv = None
            print(f"[CAP]   Camera started: {source} ({CAM_W}x{CAM_H})")
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
        frame = processor.draw(frame)

        # Push processed frame to appsrc
        if _appsrc is not None:
            out_buf = Gst.Buffer.new_wrapped(frame.tobytes())
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
        try:
            while True:
                await asyncio.sleep(1.0)
                tracking, t_ms = processor.get_track_info()
                if tracking:
                    await websocket.send(f"status:tracking {t_ms:.1f}ms")
                det_on, d_ms, d_count = processor.get_det_info()
                if det_on:
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

            elif message.startswith("detect:"):
                val = message.split(":", 1)[1].strip().lower()
                on = val in ("on", "1", "true")
                processor.enable_detector(on)
                reply = f"Detection {'ON' if on else 'OFF'}"

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

            else:
                reply = f"Command received: {message}"

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

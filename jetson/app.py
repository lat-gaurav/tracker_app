"""Jetson server entry point: wires processor, capture, RTSP, WebSocket, recorder."""
import os
import signal
import threading

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from jetson.constants import DEFAULT_DEVICE, VISCA_PORT, VISCA_BAUD
from jetson.processor import FrameProcessor
from jetson.streaming import RTSPState, RTSPServerManager, capture_and_push
from jetson.control import Recorder, WebSocketServer
from jetson.control.ws_handler import Context
from jetson.utils import check_device

from jetson.visca import VISCACamera


def _open_visca():
    """Open VISCA serial camera.  Returns (camera or None) — failure is
    non-fatal, just disables camera control."""
    try:
        cam = VISCACamera(VISCA_PORT, VISCA_BAUD)
        cam.open()
        return cam
    except Exception as e:
        print(f"[VISCA] Camera not available: {e} — camera control disabled")
        return None


def main():
    Gst.init(None)

    # Resolve camera device (falls back to first available if default missing)
    device = check_device(DEFAULT_DEVICE)

    # Core components
    processor   = FrameProcessor()
    rtsp_state  = RTSPState()
    recorder    = Recorder(processor)
    visca_cam   = _open_visca()

    # Capture thread
    cap_thread = threading.Thread(
        target=capture_and_push,
        args=(processor, rtsp_state, recorder, device),
        daemon=True,
    )
    cap_thread.start()

    # WebSocket server thread
    ctx = Context(processor, rtsp_state, recorder, visca_cam, device)
    ws_server = WebSocketServer(ctx)
    ws_server.start()

    # RTSP server (uses GLib main loop)
    rtsp = RTSPServerManager(rtsp_state)
    rtsp.start()

    loop = GLib.MainLoop()
    signal.signal(signal.SIGINT,
                  lambda s, f: (print("\n[INFO]  Shutting down..."), loop.quit()))

    print("[INFO]  Press Ctrl+C to stop.\n")
    loop.run()
    print("[INFO]  Server stopped.")

    # Force exit — daemon threads + GStreamer callbacks can leak
    os._exit(0)


if __name__ == "__main__":
    main()

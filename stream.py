'''
use this on the ground laptop side to view the stream 
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.101:8554/stream latency=0 drop-on-latency=true protocols=tcp ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink sync=false
GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0 \
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.101:8554/stream \
  latency=0 drop-on-latency=true protocols=tcp ! \
  rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! xvimagesink sync=false

'''


import gi
import os
import sys
import signal

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib

DEVICE = "/dev/video4"
PORT = "8554"
MOUNT = "/stream"
IP = "192.168.144.101"


def check_device(device):
    if not os.path.exists(device):
        available = [f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("video")]
        print(f"[ERROR] Camera device {device} not found!")
        print(f"[INFO]  Available video devices: {sorted(available)}")
        sys.exit(1)
    print(f"[OK]    Camera device {device} found.")


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
    # Only log state changes from the top-level bin
    if not message.src.get_name().startswith("pipeline"):
        return
    old, new, _ = message.parse_state_changed()
    old_name = Gst.Element.state_get_name(old)
    new_name = Gst.Element.state_get_name(new)
    if old_name != new_name:
        print(f"[STATE] Pipeline: {old_name} -> {new_name}")


def on_media_configure(_factory, media):
    pipeline = media.get_element()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", on_bus_error)
    bus.connect("message::warning", on_bus_warning)
    bus.connect("message::state-changed", on_bus_state_changed)
    print(f"[INFO]  Client connected — pipeline starting.")


def on_client_connected(_server, _client):
    print(f"[INFO]  New RTSP client connected.")


def on_client_closed(_client):
    print(f"[INFO]  RTSP client disconnected.")


def main():
    Gst.init(None)
    check_device(DEVICE)

    loop = GLib.MainLoop()
    signal.signal(signal.SIGINT, lambda s, f: (print("\n[INFO]  Shutting down..."), loop.quit()))

    server = GstRtspServer.RTSPServer()
    server.set_service(PORT)
    server.connect("client-connected", on_client_connected)

    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_launch(
        f'( v4l2src device={DEVICE} ! '
        'video/x-raw,width=1280,height=800,framerate=30/1 ! '
        'videoconvert ! video/x-raw,format=I420 ! '
        'x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 '
        'key-int-max=30 bframes=0 sliced-threads=true ! '
        'h264parse config-interval=1 ! '
        'rtph264pay name=pay0 pt=96 aggregate-mode=zero-latency )'
    )
    factory.set_shared(True)
    factory.set_latency(0)
    factory.connect("media-configure", on_media_configure)

    server.get_mount_points().add_factory(MOUNT, factory)
    server.attach(None)

    print(f"[OK]    RTSP stream ready at: rtsp://{IP}:{PORT}{MOUNT}")
    print(f"[INFO]  Press Ctrl+C to stop.\n")

    loop.run()
    print("[INFO]  Server stopped.")


if __name__ == "__main__":
    main()

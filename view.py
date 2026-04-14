import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GLib
import sys

RTSP_URL = "rtsp://192.168.144.101:8554/stream"

Gst.init(None)

pipeline = Gst.parse_launch(
    f'rtspsrc location={RTSP_URL} latency=0 protocols=udp buffer-mode=none ! '
    'rtph264depay ! '
    'h264parse ! '
    'queue max-size-buffers=1 leaky=downstream ! '
    'avdec_h264 lowres=0 skip-frame=default output-corrupt=false ! '
    'videoconvert ! '
    'autovideosink sync=false'
)

bus = pipeline.get_bus()
bus.add_signal_watch()

loop = GLib.MainLoop()

def on_message(bus, message):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("Stream ended.")
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"Error: {err.message}")
        print(f"Debug: {debug}")
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        warn, debug = message.parse_warning()
        print(f"Warning: {warn.message}")

bus.connect("message", on_message)

pipeline.set_state(Gst.State.PLAYING)
print(f"Connecting to {RTSP_URL} ...")

try:
    loop.run()
except KeyboardInterrupt:
    print("Stopped.")
finally:
    pipeline.set_state(Gst.State.NULL)

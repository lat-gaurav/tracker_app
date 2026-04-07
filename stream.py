import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib

Gst.init(None)

server = GstRtspServer.RTSPServer()
server.set_service("8554")

factory = GstRtspServer.RTSPMediaFactory()
factory.set_launch(
    '( v4l2src device=/dev/video0 ! '
    'videoconvert ! video/x-raw,format=I420 ! '
    'x264enc tune=zerolatency speed-preset=ultrafast bframes=0 vbv-buf-capacity=0 bitrate=2000 ! '
    'h264parse config-interval=-1 ! '
    'rtph264pay name=pay0 pt=96 aggregate-mode=zero-latency )'
)
factory.set_shared(True)
factory.set_latency(0)

server.get_mount_points().add_factory("/stream", factory)
server.attach(None)

print("RTSP stream ready at: rtsp://192.168.144.101:8554/stream")
GLib.MainLoop().run()

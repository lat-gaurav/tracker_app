"""RTSP server wrapper.  Holds the factory's appsrc and videoflip handles
so the capture thread can push frames and the WS thread can change rotation.
"""
import threading

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer

from jetson.constants import RTSP_PORT, RTSP_MOUNT, RTSP_IP
from jetson.streaming.pipelines import build_rtsp_launch


class RTSPState:
    """Shared state between capture + WS + RTSP callbacks.
    Always lock-free: appsrc/vflip are updated from media-configure (GLib
    main-loop thread), read from the capture thread; Python reference
    assignment is atomic."""

    def __init__(self):
        self.appsrc   = None
        self.vflip    = None
        self.rotation = 0     # videoflip method (0–3)
        self.source_request = None   # set by WS thread, consumed by capture


# ---- bus callbacks ----

def _on_bus_error(_bus, message):
    err, debug = message.parse_error()
    print(f"[ERROR] GStreamer: {err.message}")
    if debug:
        print(f"[DEBUG] {debug}")


def _on_bus_warning(_bus, message):
    warn, debug = message.parse_warning()
    print(f"[WARN]  GStreamer: {warn.message}")
    if debug:
        print(f"[DEBUG] {debug}")


def _on_bus_state_changed(_bus, message):
    if not message.src.get_name().startswith("pipeline"):
        return
    old, new, _ = message.parse_state_changed()
    old_name = Gst.Element.state_get_name(old)
    new_name = Gst.Element.state_get_name(new)
    if old_name != new_name:
        print(f"[STATE] Pipeline: {old_name} -> {new_name}")


def _on_client_connected(_server, _client):
    print("[INFO]  New RTSP client connected.")


class RTSPServerManager:
    """Creates and owns the RTSP server + media factory."""

    def __init__(self, state: RTSPState):
        self.state = state
        self.server  = None
        self.factory = None

    def start(self):
        self.server = GstRtspServer.RTSPServer()
        self.server.set_service(RTSP_PORT)
        self.server.connect("client-connected", _on_client_connected)

        self.factory = GstRtspServer.RTSPMediaFactory()
        self.factory.set_launch(build_rtsp_launch())
        self.factory.set_shared(True)
        self.factory.set_latency(0)
        # Keep pipeline alive with no clients — encoder stays initialised
        self.factory.set_property('suspend-mode', 0)
        self.factory.connect("media-configure", self._on_media_configure)

        self.server.get_mount_points().add_factory(RTSP_MOUNT, self.factory)
        self.server.attach(None)

        print(f"[OK]    RTSP stream ready at: rtsp://{RTSP_IP}:{RTSP_PORT}{RTSP_MOUNT}")

    def _on_media_configure(self, _factory, media):
        media.set_property('suspend-mode', GstRtspServer.RTSPSuspendMode.NONE)
        pipeline = media.get_element()
        self.state.appsrc = pipeline.get_by_name('src')
        self.state.vflip  = pipeline.get_by_name('vflip')
        if self.state.vflip:
            self.state.vflip.set_property('method', self.state.rotation)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", _on_bus_error)
        bus.connect("message::warning", _on_bus_warning)
        bus.connect("message::state-changed", _on_bus_state_changed)
        print("[INFO]  Client connected — RTSP pipeline starting.")

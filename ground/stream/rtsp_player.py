"""RTSP pipeline wrapper: rtspsrc -> avdec_h264 -> xvimagesink.

The pipeline attaches itself to a Qt widget's X11 window so frames
render directly inside the GUI.  A pad probe on the decoder output
drives the frame watchdog via a Qt signal.
"""
import time

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo


class RTSPPlayer:
    """Manages the GStreamer RTSP playback pipeline.

    All Qt-side interactions happen through the `signals` object:
      signals.frame_received    — one frame decoded
      signals.status_changed    — text for the status label
      signals.stream_playing    — pipeline reached PLAYING
      signals.reconnect         — watchdog tripped / pipeline error
    """

    def __init__(self, rtsp_url, signals, window_id_provider):
        """
        rtsp_url            : e.g. "rtsp://192.168.144.102:8554/stream"
        signals             : GstSignals instance
        window_id_provider  : callable returning the X11 window id of the
                              widget where xvimagesink should render.
        """
        self._url      = rtsp_url
        self._signals  = signals
        self._get_wid  = window_id_provider
        self._pipeline = None
        self._last_frame_ts    = 0.0
        self._first_frame_logged = False
        self._auto_reconnect   = False

    # ---- control ----

    @property
    def pipeline(self):
        return self._pipeline

    @property
    def auto_reconnect(self):
        return self._auto_reconnect

    @auto_reconnect.setter
    def auto_reconnect(self, value):
        self._auto_reconnect = value

    @property
    def last_frame_ts(self):
        return self._last_frame_ts

    def reset_last_frame_ts(self):
        self._last_frame_ts = time.monotonic()
        self._first_frame_logged = False

    def start(self):
        print(f"[GS] start_stream() called at t={time.monotonic():.2f}")
        self._pipeline = Gst.parse_launch(
            f'rtspsrc location={self._url} latency=200 drop-on-latency=true '
            'protocols=tcp tcp-timeout=15000000 do-rtsp-keep-alive=true ! '
            'rtph264depay ! h264parse ! avdec_h264 ! '
            'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 '
            'leaky=downstream ! '
            'videoconvert name=vc ! '
            'xvimagesink name=vsink sync=false handle-events=false'
        )

        vc = self._pipeline.get_by_name('vc')
        vc.get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, self._on_frame_probe)

        vsink = self._pipeline.get_by_name('vsink')
        GstVideo.VideoOverlay.set_window_handle(vsink, self._get_wid())

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        print(f"[GS] set_state(PLAYING) returned: {ret}")

    def stop(self):
        print(f"[GS] _teardown_pipeline() at t={time.monotonic():.2f}")
        self._last_frame_ts = 0.0
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

    def expose(self):
        """Re-paint xvimagesink at the current widget size."""
        if self._pipeline:
            vsink = self._pipeline.get_by_name('vsink')
            if vsink:
                vsink.expose()

    # ---- GStreamer callbacks ----

    def _on_frame_probe(self, _pad, _info):
        now = time.monotonic()
        if not self._first_frame_logged:
            print(f"[GS] First frame decoded at t={now:.2f}")
            self._first_frame_logged = True
        # Rate-limit watchdog timestamp to once per second
        if now - self._last_frame_ts > 1.0:
            self._last_frame_ts = now
        self._signals.frame_received.emit()
        return Gst.PadProbeReturn.OK

    def _on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.STATE_CHANGED:
            old, new, _ = message.parse_state_changed()
            src_name = message.src.get_name() if message.src else "?"
            old_name = Gst.Element.state_get_name(old)
            new_name = Gst.Element.state_get_name(new)
            print(f"[GS STATE] {src_name}: {old_name} -> {new_name}")
            if message.src == self._pipeline and new == Gst.State.PLAYING:
                self._signals.status_changed.emit("Streaming")
                self._signals.stream_playing.emit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            src_name = message.src.get_name() if message.src else "?"
            print(f"[GS ERROR] from {src_name}: {err.message}")
            if debug:
                print(f"[GS ERROR] debug: {debug}")
            if self._auto_reconnect:
                self._signals.reconnect.emit()
        elif t == Gst.MessageType.EOS:
            src_name = message.src.get_name() if message.src else "?"
            print(f"[GS EOS] from {src_name} at t={time.monotonic():.2f}")
            if self._auto_reconnect:
                self._signals.reconnect.emit()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            src_name = message.src.get_name() if message.src else "?"
            print(f"[GS WARN] from {src_name}: {warn.message}")

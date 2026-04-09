#  QT_QPA_PLATFORM=xcb python3 ground_station.py

import sys
import os
import threading
import time

os.environ.setdefault(
    'GST_PLUGIN_PATH',
    '/usr/lib/x86_64-linux-gnu/gstreamer-1.0'
)

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GLib, GstVideo

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer

Gst.init(None)

RTSP_URL             = "rtsp://192.168.144.101:8554/stream"
WATCHDOG_INTERVAL_MS = 500
WATCHDOG_TIMEOUT_S   = 3.0


class GstSignals(QObject):
    status_changed  = pyqtSignal(str)
    reconnect       = pyqtSignal()
    stream_playing  = pyqtSignal()


class GroundStation(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ground Station")
        self.resize(800, 550)

        self.pipeline       = None
        self.glib_loop      = None
        self.glib_thread    = None
        self.auto_reconnect = False
        self._last_frame_ts = 0.0

        self.signals = GstSignals()
        self.signals.status_changed.connect(self._on_status_changed)
        self.signals.reconnect.connect(self._do_reconnect)
        self.signals.stream_playing.connect(self._on_stream_playing)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_watchdog)

        # --- Layout ---
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Video widget — xvimagesink renders directly here (same as gst-launch autovideosink)
        self.video_widget = QWidget()
        self.video_widget.setMinimumSize(640, 480)
        self.video_widget.setStyleSheet("background-color: black;")
        main_layout.addWidget(self.video_widget)

        controls = QHBoxLayout()
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: gray;")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedWidth(120)
        self.connect_btn.clicked.connect(self.toggle_stream)
        controls.addWidget(self.status_label)
        controls.addStretch()
        controls.addWidget(self.connect_btn)
        main_layout.addLayout(controls)

    # ------------------------------------------------------------------ stream

    def toggle_stream(self):
        if self.pipeline is None:
            self.start_stream()
        else:
            self.stop_stream()

    def start_stream(self):
        self.auto_reconnect = True
        self._last_frame_ts = time.monotonic()

        self.pipeline = Gst.parse_launch(
            f'rtspsrc location={RTSP_URL} latency=0 drop-on-latency=true protocols=tcp '
            'tcp-timeout=3000000 do-rtsp-keep-alive=true ! '
            'rtph264depay ! h264parse ! avdec_h264 ! '
            'videoconvert name=vc ! '
            'xvimagesink name=vsink sync=false handle-events=false'
        )

        # Watchdog probe on videoconvert src pad — before the sink, zero render overhead
        vc = self.pipeline.get_by_name('vc')
        vc.get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, self._on_frame_probe
        )

        # Embed into Qt widget
        vsink = self.pipeline.get_by_name('vsink')
        GstVideo.VideoOverlay.set_window_handle(vsink, self.video_widget.winId())

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)
        self.connect_btn.setText("Disconnect")
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet("color: orange;")

        self.glib_loop = GLib.MainLoop()
        self.glib_thread = threading.Thread(target=self.glib_loop.run, daemon=True)
        self.glib_thread.start()
        # Watchdog starts only after PLAYING — see _on_bus_message

    def _teardown_pipeline(self):
        self._watchdog.stop()
        self._last_frame_ts = 0.0
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.glib_loop:
            self.glib_loop.quit()
            self.glib_loop = None

    def stop_stream(self):
        self.auto_reconnect = False
        self._teardown_pipeline()
        self.connect_btn.setText("Connect")
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: gray;")

    def _do_reconnect(self):
        self._teardown_pipeline()
        self.status_label.setText("Reconnecting...")
        self.status_label.setStyleSheet("color: orange;")
        QTimer.singleShot(500, self.start_stream)

    # ------------------------------------------------------------------ probe (GStreamer thread)

    def _on_frame_probe(self, _pad, _info):
        # Rate-limited to once per second — minimises GIL acquisition from GStreamer thread
        now = time.monotonic()
        if now - self._last_frame_ts > 1.0:
            self._last_frame_ts = now
        return Gst.PadProbeReturn.OK

    # ------------------------------------------------------------------ watchdog (Qt main thread)

    def _check_watchdog(self):
        if not self.auto_reconnect or self.pipeline is None:
            return
        age = time.monotonic() - self._last_frame_ts
        if age > WATCHDOG_TIMEOUT_S:
            print(f"[WATCHDOG] No frame for {age:.1f}s — reconnecting")
            self.signals.reconnect.emit()

    # ------------------------------------------------------------------ bus messages

    def _on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                _, new, _ = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    self.signals.status_changed.emit("Streaming")
                    self.signals.stream_playing.emit()
        elif t == Gst.MessageType.ERROR:
            err, _ = message.parse_error()
            print(f"[ERROR] {err.message}")
            if self.auto_reconnect:
                self.signals.reconnect.emit()
        elif t == Gst.MessageType.EOS:
            if self.auto_reconnect:
                self.signals.reconnect.emit()

    # ------------------------------------------------------------------ qt slots

    def _on_status_changed(self, text):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: green;")

    def _on_stream_playing(self):
        self._last_frame_ts = time.monotonic()
        if not self._watchdog.isActive():
            self._watchdog.start()

    def closeEvent(self, event):
        self.stop_stream()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GroundStation()
    window.show()
    sys.exit(app.exec())

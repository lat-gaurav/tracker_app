#  QT_QPA_PLATFORM=xcb python3 ground_station.py

import sys
import os
import threading
import time
from collections import deque
import json
import websocket   # websocket-client

os.environ.setdefault(
    'GST_PLUGIN_PATH',
    '/usr/lib/x86_64-linux-gnu/gstreamer-1.0'
)

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GLib, GstVideo

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit, QComboBox,
    QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont

Gst.init(None)

RTSP_URL             = "rtsp://192.168.144.102:8554/stream"
WS_URL               = "ws://192.168.144.102:5001"
EXPECTED_FPS         = 30
WATCHDOG_INTERVAL_MS = 500
WATCHDOG_TIMEOUT_S   = 8.0


class GstSignals(QObject):
    status_changed = pyqtSignal(str)
    reconnect      = pyqtSignal()
    stream_playing = pyqtSignal()
    frame_received = pyqtSignal()
    ws_message     = pyqtSignal(str)       # message received from Jetson WebSocket
    ws_status      = pyqtSignal(str)       # WebSocket connection status


class WSClient:
    """WebSocket client running in a background thread."""

    def __init__(self, url, on_message, on_status):
        self._url       = url
        self._on_message = on_message
        self._on_status  = on_status
        self._ws        = None
        self._thread    = None
        self._running   = False

    def connect(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._running = False
        if self._ws:
            self._ws.close()

    def send(self, text):
        if self._ws and self._running:
            try:
                self._ws.send(text)
            except Exception as e:
                print(f"[WS] Send error: {e}")

    def _run(self):
        self._on_status("Connecting...")
        try:
            self._ws = websocket.WebSocketApp(
                self._url,
                on_open    = lambda ws:       self._on_status("Connected"),
                on_message = lambda ws, msg:  self._on_message(msg),
                on_error   = lambda ws, err:  self._on_status(f"Error: {err}"),
                on_close   = lambda ws, c, m: self._on_status("Disconnected"),
            )
            self._ws.run_forever(reconnect=3)
        except Exception as e:
            self._on_status(f"Error: {e}")


class ClickableVideoWidget(QWidget):
    """QWidget that captures mouse clicks and maps them to video coordinates."""

    # Native video dimensions per rotation method
    VIDEO_DIMS = {
        0: (1920, 1080),   # no rotation
        1: (1080, 1920),   # 90° CW  — dimensions swap
        2: (1920, 1080),   # 180°
        3: (1080, 1920),   # 270° CW — dimensions swap
    }

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click    = on_click
        self._rotate_method = 0
        # Ensure the widget has its own native X11 window so xvimagesink can embed
        # and correctly receive ConfigureNotify events on resize.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_rotation(self, method):
        self._rotate_method = method

    def mousePressEvent(self, event):
        if event.button().name != 'LeftButton':
            return
        vw, vh = self.VIDEO_DIMS[self._rotate_method]
        ww, wh = self.width(), self.height()

        # Calculate actual rendered video rect (aspect-ratio preserved, centred)
        scale     = min(ww / vw, wh / vh)
        render_w  = vw * scale
        render_h  = vh * scale
        offset_x  = (ww - render_w) / 2
        offset_y  = (wh - render_h) / 2

        cx = event.position().x()
        cy = event.position().y()

        # Ignore clicks on black bars
        if cx < offset_x or cx > offset_x + render_w:
            return
        if cy < offset_y or cy > offset_y + render_h:
            return

        nx = (cx - offset_x) / render_w
        ny = (cy - offset_y) / render_h
        self._on_click(nx, ny)


class GroundStation(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ground Station")
        self.resize(1280, 800)

        self.pipeline       = None
        self.auto_reconnect = False

        # GLib main loop — created once, lives for the full app lifetime.
        # Running it in a dedicated daemon thread means every pipeline's bus
        # messages are dispatched on the same stable context.  Re-creating the
        # loop on every reconnect caused a race where the old thread hadn't
        # exited before the new loop.run() started on the same default context.
        self._glib_loop = GLib.MainLoop()
        _gl = threading.Thread(target=self._glib_loop.run, daemon=True)
        _gl.start()
        self._last_frame_ts = 0.0

        # Health tracking
        self._frame_times     = deque()   # timestamps of recent frames (for FPS)
        self._reconnect_count = 0
        self._connect_ts      = 0.0       # when current session started

        # Jetson offline detection
        self._failed_attempts = 0         # consecutive reconnects that never reached PLAYING
        self._backoff_delays  = [0.5, 1, 2, 5, 10]  # seconds between retries

        self.signals = GstSignals()
        self.signals.status_changed.connect(self._on_status_changed)
        self.signals.reconnect.connect(self._do_reconnect)
        self.signals.stream_playing.connect(self._on_stream_playing)
        self.signals.frame_received.connect(self._on_frame_received)
        self.signals.ws_message.connect(self._on_ws_message)
        self.signals.ws_status.connect(self._on_ws_status)

        self._ws_client = WSClient(
            WS_URL,
            on_message = lambda msg: self.signals.ws_message.emit(msg),
            on_status  = lambda s:   self.signals.ws_status.emit(s),
        )
        self._ws_client.connect()

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_watchdog)

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._update_health)

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(800)
        self._blink_timer.timeout.connect(self._blink_offline)
        self._blink_state = True

        # --- Layout ---
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(4)

        # Video stack — page 0: live stream | page 1: offline screen
        self.video_stack = QStackedWidget()
        self.video_stack.setMinimumSize(640, 360)
        self.video_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Page 0 — xvimagesink renders here
        self.video_widget = ClickableVideoWidget(self._on_video_click)
        self.video_widget.setStyleSheet("background-color: black;")
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_stack.addWidget(self.video_widget)

        # Page 1 — offline screen
        offline_widget = QWidget()
        offline_widget.setStyleSheet("background-color: black;")
        offline_layout = QVBoxLayout(offline_widget)
        offline_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_title = QLabel("NO SIGNAL")
        self._offline_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_title.setFont(QFont("Monospace", 28, QFont.Weight.Bold))
        self._offline_title.setStyleSheet("color: #ff3333;")
        self._offline_sub = QLabel("Jetson stream offline — retrying...")
        self._offline_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_sub.setStyleSheet("color: #888888; font-size: 13px;")
        self._offline_attempt_label = QLabel("")
        self._offline_attempt_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_attempt_label.setStyleSheet("color: #555555; font-size: 11px;")
        offline_layout.addWidget(self._offline_title)
        offline_layout.addSpacing(8)
        offline_layout.addWidget(self._offline_sub)
        offline_layout.addWidget(self._offline_attempt_label)
        self.video_stack.addWidget(offline_widget)

        main_layout.addWidget(self.video_stack, 1)   # stretch factor so video row consumes all spare vertical space

        # Health bar
        health_bar = QHBoxLayout()
        self.dot_label    = QLabel("●")
        self.fps_label    = QLabel("-- fps")
        self.health_label = QLabel("Health: --%")
        self.uptime_label = QLabel("Uptime: --:--")
        self.reconnect_label = QLabel("Reconnects: 0")
        self.track_label = QLabel("Track: --")
        for lbl in (self.dot_label, self.fps_label, self.health_label,
                    self.uptime_label, self.reconnect_label, self.track_label):
            lbl.setStyleSheet("color: gray; font-size: 11px;")
        self.dot_label.setStyleSheet("color: gray; font-size: 14px;")
        health_bar.addWidget(self.dot_label)
        health_bar.addSpacing(4)
        health_bar.addWidget(self.fps_label)
        health_bar.addSpacing(12)
        health_bar.addWidget(self.health_label)
        health_bar.addSpacing(12)
        health_bar.addWidget(self.uptime_label)
        health_bar.addSpacing(12)
        health_bar.addWidget(self.reconnect_label)
        health_bar.addSpacing(12)
        health_bar.addWidget(self.track_label)
        health_bar.addStretch()
        main_layout.addLayout(health_bar)

        # Command bar
        cmd_bar = QHBoxLayout()
        self.ws_status_label = QLabel("WS: --")
        self.ws_status_label.setStyleSheet("color: gray; font-size: 11px;")
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("Type a command...")
        self.cmd_input.returnPressed.connect(self._send_command)
        self.send_btn = QPushButton("Send")
        self.send_btn.setFixedWidth(70)
        self.send_btn.clicked.connect(self._send_command)

        # Rotate button — cycles 0° → 90° → 180° → 270° → 0°
        self._rotate_steps  = [0, 1, 2, 3]
        self._rotate_labels = ["Rotate: 0°", "Rotate: 90°", "Rotate: 180°", "Rotate: 270°"]
        self._rotate_index  = 0
        self.rotate_btn = QPushButton(self._rotate_labels[0])
        self.rotate_btn.setFixedWidth(130)
        self.rotate_btn.clicked.connect(self._send_rotate)

        # Box size control
        self.box_label = QLabel("Box:")
        self.box_label.setStyleSheet("font-size: 11px;")
        self.box_input = QLineEdit("20")
        self.box_input.setFixedWidth(45)
        self.box_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.box_input.returnPressed.connect(self._send_box_size)

        self.ws_response_label = QLabel("")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

        cmd_bar.addWidget(self.ws_status_label)
        cmd_bar.addSpacing(8)
        cmd_bar.addWidget(self.cmd_input)
        cmd_bar.addWidget(self.send_btn)
        cmd_bar.addSpacing(8)
        cmd_bar.addWidget(self.rotate_btn)
        cmd_bar.addSpacing(8)
        cmd_bar.addWidget(self.box_label)
        cmd_bar.addWidget(self.box_input)
        main_layout.addLayout(cmd_bar)

        main_layout.addWidget(self.ws_response_label)

        # Controls bar
        controls = QHBoxLayout()
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: gray;")

        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(250)
        self.source_combo.addItem("Camera: /dev/video4", "/dev/video4")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedWidth(120)
        self.connect_btn.clicked.connect(self.toggle_stream)
        controls.addWidget(self.status_label)
        controls.addSpacing(8)
        controls.addWidget(self.source_combo)
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
        self._frame_times.clear()
        self._first_frame_logged = False
        print(f"[GS] start_stream() called at t={time.monotonic():.2f}")

        self.pipeline = Gst.parse_launch(
            f'rtspsrc location={RTSP_URL} latency=200 drop-on-latency=true protocols=tcp '
            'tcp-timeout=15000000 do-rtsp-keep-alive=true ! '
            'rtph264depay ! h264parse ! avdec_h264 ! '
            'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            'videoconvert name=vc ! '
            'xvimagesink name=vsink sync=false handle-events=false'
        )

        vc = self.pipeline.get_by_name('vc')
        vc.get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, self._on_frame_probe
        )

        vsink = self.pipeline.get_by_name('vsink')
        GstVideo.VideoOverlay.set_window_handle(vsink, self.video_widget.winId())

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[GS] set_state(PLAYING) returned: {ret}")
        self.connect_btn.setText("Disconnect")
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet("color: orange;")

    def _teardown_pipeline(self):
        print(f"[GS] _teardown_pipeline() at t={time.monotonic():.2f}")
        self._watchdog.stop()
        self._health_timer.stop()
        self._last_frame_ts = 0.0
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def stop_stream(self):
        self.auto_reconnect = False
        self._failed_attempts = 0
        self._blink_timer.stop()
        self._teardown_pipeline()
        self.video_stack.setCurrentIndex(0)
        self.connect_btn.setText("Connect")
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: gray;")
        self._reset_health_display()

    def _do_reconnect(self):
        self._reconnect_count += 1
        self._failed_attempts += 1
        self._teardown_pipeline()
        self.reconnect_label.setText(f"Reconnects: {self._reconnect_count}")

        # After 3 consecutive failures, assume Jetson stream is down
        if self._failed_attempts >= 3:
            self.status_label.setText("Jetson Offline")
            self.status_label.setStyleSheet("color: red;")
            self.dot_label.setStyleSheet("color: red; font-size: 14px;")
            self._offline_attempt_label.setText(
                f"Attempt {self._failed_attempts}  —  next retry in "
                f"{self._backoff_delays[min(self._failed_attempts-1, len(self._backoff_delays)-1)]}s"
            )
            self.video_stack.setCurrentIndex(1)   # show offline screen
            self._blink_timer.start()
        else:
            self.status_label.setText("Reconnecting...")
            self.status_label.setStyleSheet("color: orange;")

        # Exponential backoff — cap at last entry
        idx = min(self._failed_attempts - 1, len(self._backoff_delays) - 1)
        delay_ms = int(self._backoff_delays[idx] * 1000)
        print(f"[RECONNECT] Attempt {self._failed_attempts}, retrying in {delay_ms}ms")
        QTimer.singleShot(delay_ms, self.start_stream)

    # ------------------------------------------------------------------ probe (GStreamer thread)

    def _on_frame_probe(self, _pad, _info):
        now = time.monotonic()
        if not getattr(self, '_first_frame_logged', False):
            print(f"[GS] First frame decoded at t={now:.2f}")
            self._first_frame_logged = True
        # Rate-limit: update watchdog timestamp at most once per second
        if now - self._last_frame_ts > 1.0:
            self._last_frame_ts = now
        # Emit signal for FPS tracking (deque append is fast)
        self.signals.frame_received.emit()
        return Gst.PadProbeReturn.OK

    # ------------------------------------------------------------------ watchdog

    def _check_watchdog(self):
        if not self.auto_reconnect or self.pipeline is None:
            return
        age = time.monotonic() - self._last_frame_ts
        if age > WATCHDOG_TIMEOUT_S:
            print(f"[WATCHDOG] No frame for {age:.1f}s — reconnecting")
            self.signals.reconnect.emit()

    # ------------------------------------------------------------------ health

    def _on_frame_received(self):
        now = time.monotonic()
        self._frame_times.append(now)
        # Keep only frames from the last 2 seconds
        cutoff = now - 2.0
        while self._frame_times and self._frame_times[0] < cutoff:
            self._frame_times.popleft()

    def _update_health(self):
        if not self.auto_reconnect or self.pipeline is None:
            return

        # FPS — count frames in last 1 second
        now = time.monotonic()
        recent = sum(1 for t in self._frame_times if t > now - 1.0)
        fps = recent

        # Health % relative to expected FPS
        health = min(100, int(fps / EXPECTED_FPS * 100))

        # Uptime
        elapsed = int(now - self._connect_ts)
        uptime = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

        # Colour
        if health >= 80:
            dot_color, text_color = "#00cc44", "#00cc44"
        elif health >= 40:
            dot_color, text_color = "#ffaa00", "#ffaa00"
        else:
            dot_color, text_color = "#ff3333", "#ff3333"

        self.dot_label.setStyleSheet(f"color: {dot_color}; font-size: 14px;")
        for lbl in (self.fps_label, self.health_label, self.uptime_label):
            lbl.setStyleSheet(f"color: {text_color}; font-size: 11px;")

        self.fps_label.setText(f"{fps} fps")
        self.health_label.setText(f"Health: {health}%")
        self.uptime_label.setText(f"Uptime: {uptime}")

    def _blink_offline(self):
        self._blink_state = not self._blink_state
        color = "#ff3333" if self._blink_state else "#661111"
        self._offline_title.setStyleSheet(f"color: {color};")

    def _reset_health_display(self):
        for lbl in (self.dot_label, self.fps_label, self.health_label,
                    self.uptime_label, self.reconnect_label):
            lbl.setStyleSheet("color: gray; font-size: 11px;")
        self.dot_label.setStyleSheet("color: gray; font-size: 14px;")
        self.fps_label.setText("-- fps")
        self.health_label.setText("Health: --%")
        self.uptime_label.setText("Uptime: --:--")
        self.reconnect_label.setText("Reconnects: 0")
        self._reconnect_count = 0

    # ------------------------------------------------------------------ bus messages

    def _on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.STATE_CHANGED:
            old, new, _ = message.parse_state_changed()
            src_name = message.src.get_name() if message.src else "?"
            old_name = Gst.Element.state_get_name(old)
            new_name = Gst.Element.state_get_name(new)
            print(f"[GS STATE] {src_name}: {old_name} -> {new_name}")
            if message.src == self.pipeline and new == Gst.State.PLAYING:
                self.signals.status_changed.emit("Streaming")
                self.signals.stream_playing.emit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            src_name = message.src.get_name() if message.src else "?"
            print(f"[GS ERROR] from {src_name}: {err.message}")
            if debug:
                print(f"[GS ERROR] debug: {debug}")
            if self.auto_reconnect:
                self.signals.reconnect.emit()
        elif t == Gst.MessageType.EOS:
            src_name = message.src.get_name() if message.src else "?"
            print(f"[GS EOS] from {src_name} at t={time.monotonic():.2f}")
            if self.auto_reconnect:
                self.signals.reconnect.emit()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            src_name = message.src.get_name() if message.src else "?"
            print(f"[GS WARN] from {src_name}: {warn.message}")

    # ------------------------------------------------------------------ qt slots

    def _on_status_changed(self, text):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: green;")

    def _on_stream_playing(self):
        self._last_frame_ts   = time.monotonic()
        self._connect_ts      = time.monotonic()
        self._failed_attempts = 0
        self._frame_times.clear()
        self._blink_timer.stop()
        self.video_stack.setCurrentIndex(0)   # back to live video
        if not self._watchdog.isActive():
            self._watchdog.start()
        if not self._health_timer.isActive():
            self._health_timer.start()

    def _send_rotate(self):
        self._rotate_index = (self._rotate_index + 1) % len(self._rotate_steps)
        method = self._rotate_steps[self._rotate_index]
        self._ws_client.send(f"rotate:{method}")
        self.rotate_btn.setText(self._rotate_labels[self._rotate_index])
        self.video_widget.set_rotation(method)   # keep coordinate mapping in sync
        self.ws_response_label.setText(f"Sent: rotate:{method}")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _send_box_size(self):
        try:
            size = int(self.box_input.text())
            size = max(10, min(200, size))
            self.box_input.setText(str(size))
            self._ws_client.send(f"boxsize:{size}")
            self.ws_response_label.setText(f"Sent: boxsize:{size}")
            self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        except ValueError:
            self.box_input.setText("20")

    def _on_source_changed(self, index):
        if index < 0:
            return
        path = self.source_combo.itemData(index)
        if path:
            self._ws_client.send(f"source:{path}")
            self.ws_response_label.setText(f"Sent: source:{os.path.basename(path)}")
            self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _on_video_click(self, nx, ny):
        self._ws_client.send(f"click:{nx:.4f},{ny:.4f}")
        self.ws_response_label.setText(f"Sent: click ({nx:.3f}, {ny:.3f})")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _send_command(self):
        text = self.cmd_input.text().strip()
        if not text:
            return
        self._ws_client.send(text)
        self.ws_response_label.setText(f"Sent: {text}")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        self.cmd_input.clear()

    def _on_ws_message(self, msg):
        if msg.startswith("status:"):
            info = msg[7:]
            self.track_label.setText(f"Track: {info}")
            self.track_label.setStyleSheet("color: #00cc44; font-size: 11px;")
            return
        if msg.startswith("sources:"):
            data = json.loads(msg[8:])
            current = data.get("current", "")
            self.source_combo.blockSignals(True)
            self.source_combo.clear()
            select_idx = 0
            for cam in data.get("cameras", []):
                self.source_combo.addItem(f"Camera: {cam}", cam)
                if cam == current:
                    select_idx = self.source_combo.count() - 1
            for vid in data.get("videos", []):
                name = os.path.basename(vid)
                self.source_combo.addItem(f"Video: {name}", vid)
                if vid == current:
                    select_idx = self.source_combo.count() - 1
            self.source_combo.setCurrentIndex(select_idx)
            self.source_combo.blockSignals(False)
            return
        self.ws_response_label.setText(f"Jetson: {msg}")
        self.ws_response_label.setStyleSheet("color: #00cc44; font-size: 11px;")

    def _on_ws_status(self, status):
        self.ws_status_label.setText(f"WS: {status}")
        if "Connected" in status:
            self.ws_status_label.setStyleSheet("color: #00cc44; font-size: 11px;")
            self._ws_client.send("list_sources")
        elif "Error" in status or "Disconnected" in status:
            self.ws_status_label.setStyleSheet("color: #ff3333; font-size: 11px;")
        else:
            self.ws_status_label.setStyleSheet("color: orange; font-size: 11px;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Tell xvimagesink to repaint at the new widget size
        if self.pipeline:
            vsink = self.pipeline.get_by_name('vsink')
            if vsink:
                vsink.expose()

    def closeEvent(self, event):
        self._ws_client.disconnect()
        self.stop_stream()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GroundStation()
    window.show()
    sys.exit(app.exec())

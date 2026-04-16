#  QT_QPA_PLATFORM=xcb python3 ground_station.py


# echo '
# # Ground station environment
# export GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0
# export QT_QPA_PLATFORM=xcb
# ' >> ~/.bashrc

# source ~/.bashrc

# cd ~/tracker_app && python3 ground_station.py 2>&1 | grep -v "gst-plugin-scanner\|GStreamer-WARNING\|GStreamer-CRITICAL\|GLib-\|wl_buffer\|queue.*destroyed\|cannot register"





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
    QSizePolicy, QCheckBox, QTabWidget, QFormLayout, QGridLayout, QFrame,
    QMessageBox, QSlider, QScrollArea
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

        # Top section: video (expanding) + right-side config panel
        top_section = QHBoxLayout()
        top_section.setSpacing(6)
        top_section.addWidget(self.video_stack, 1)

        # Right-side config panel (will be filled later)
        self._config_panel = QWidget()
        self._config_panel.setFixedWidth(340)
        self._config_panel_layout = QVBoxLayout(self._config_panel)
        self._config_panel_layout.setContentsMargins(0, 0, 0, 0)
        self._config_panel_layout.setSpacing(6)
        top_section.addWidget(self._config_panel)

        main_layout.addLayout(top_section, 1)

        # Health bar
        health_bar = QHBoxLayout()
        self.dot_label    = QLabel("●")
        self.fps_label    = QLabel("-- fps")
        self.health_label = QLabel("Health: --%")
        self.uptime_label = QLabel("Uptime: --:--")
        self.reconnect_label = QLabel("Reconnects: 0")
        self.track_label = QLabel("Track: --")
        self.det_label   = QLabel("Det: --")
        self.rec_label   = QLabel("")
        for lbl in (self.dot_label, self.fps_label, self.health_label,
                    self.uptime_label, self.reconnect_label, self.track_label,
                    self.det_label, self.rec_label):
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
        health_bar.addSpacing(12)
        health_bar.addWidget(self.det_label)
        health_bar.addSpacing(12)
        health_bar.addWidget(self.rec_label)
        health_bar.addStretch()
        main_layout.addLayout(health_bar)

        # Tracked dict: dotted-path -> widget, for populating from config
        self._param_widgets = {}
        self._config = {}

        # ================================================================
        #  Widget creation (placed into the bottom bar further below)
        # ================================================================
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: gray; font-size: 12px;")
        self.ws_status_label = QLabel("WS: --")
        self.ws_status_label.setStyleSheet("color: gray; font-size: 11px;")

        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(240)
        self.source_combo.addItem("Camera: /dev/video4", "/dev/video4")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

        self._rotate_steps  = [0, 1, 2, 3]
        self._rotate_labels = ["Rotate: 0°", "Rotate: 90°", "Rotate: 180°", "Rotate: 270°"]
        self._rotate_index  = 0
        self.rotate_btn = QPushButton(self._rotate_labels[0])
        self.rotate_btn.setFixedWidth(110)
        self.rotate_btn.clicked.connect(self._send_rotate)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedWidth(110)
        self.connect_btn.clicked.connect(self.toggle_stream)

        self.box_input = QLineEdit("20")
        self.box_input.setFixedWidth(50)
        self.box_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # editingFinished: fires on Enter OR focus-loss, so clicking Save /
        # any other widget without pressing Enter still commits the value.
        self.box_input.editingFinished.connect(self._send_box_size)

        self.box_wh_check = QCheckBox("W×H")
        self.box_wh_check.setStyleSheet("font-size: 11px;")
        self.box_wh_check.toggled.connect(self._on_box_wh_toggled)

        self.box_w_input = QLineEdit("20"); self.box_w_input.setFixedWidth(50); self.box_w_input.setAlignment(Qt.AlignmentFlag.AlignCenter); self.box_w_input.setEnabled(False); self.box_w_input.editingFinished.connect(self._send_box_size)
        self.box_h_input = QLineEdit("20"); self.box_h_input.setFixedWidth(50); self.box_h_input.setAlignment(Qt.AlignmentFlag.AlignCenter); self.box_h_input.setEnabled(False); self.box_h_input.editingFinished.connect(self._send_box_size)

        self.stop_track_btn = QPushButton("Stop Track")
        self.stop_track_btn.setFixedWidth(100)
        self.stop_track_btn.clicked.connect(self._stop_track)

        self._detect_on = False
        self.detect_btn = QPushButton("Detect: OFF")
        self.detect_btn.setFixedWidth(110)
        self.detect_btn.setCheckable(True)
        self.detect_btn.clicked.connect(self._toggle_detect)

        self._recording = False
        self.rec_btn = QPushButton("REC")
        self.rec_btn.setFixedWidth(60)
        self.rec_btn.setCheckable(True)
        self.rec_btn.setStyleSheet("font-weight: bold;")
        self.rec_btn.clicked.connect(self._toggle_record)

        # ================================================================
        #  Right-side config panel (tabs + save button)
        # ================================================================
        self.config_tabs = QTabWidget()
        self.config_tabs.addTab(self._build_tracker_tab(),   "Tracker")
        self.config_tabs.addTab(self._build_detection_tab(), "Detection")
        self.config_tabs.addTab(self._build_camera_tab(),    "Camera")
        self.config_tabs.addTab(self._build_advanced_tab(),  "Advanced")
        self._config_panel_layout.addWidget(self.config_tabs, 1)

        self.save_default_btn = QPushButton("Save as Default (Jetson)")
        self.save_default_btn.setToolTip(
            "Write the current Jetson-side config to config/default.yaml so it "
            "is loaded on the next stream.py start.")
        self.save_default_btn.clicked.connect(self._save_default)
        self._config_panel_layout.addWidget(self.save_default_btn)

        # Camera format selector (below Save Default, in the config panel)
        cam_fmt_row = QHBoxLayout()
        self.cam_fmt_combo = QComboBox()
        self.cam_fmt_combo.setMinimumWidth(180)
        self.cam_fmt_combo.addItem("Auto", "auto")
        self.cam_fmt_combo.currentIndexChanged.connect(self._on_cam_fmt_changed)
        probe_btn = QPushButton("Probe")
        probe_btn.setFixedWidth(60)
        probe_btn.setToolTip("Query the camera for supported resolutions/framerates")
        probe_btn.clicked.connect(lambda: self._ws_client.send("list_cam_formats"))
        cam_fmt_row.addWidget(QLabel("Cam format:"))
        cam_fmt_row.addWidget(self.cam_fmt_combo)
        cam_fmt_row.addWidget(probe_btn)
        self._config_panel_layout.addLayout(cam_fmt_row)

        # Response / last-message bar (just above bottom controls)
        self.ws_response_label = QLabel("")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        main_layout.addWidget(self.ws_response_label)

        # ================================================================
        #  Bottom control bar — primary operator controls, always visible
        # ================================================================
        bottom_bar = QHBoxLayout()
        bottom_bar.addWidget(QLabel("Source:"))
        bottom_bar.addWidget(self.source_combo)
        bottom_bar.addSpacing(8)
        bottom_bar.addWidget(self.rotate_btn)
        bottom_bar.addSpacing(12)
        bottom_bar.addWidget(QLabel("Box:"))
        bottom_bar.addWidget(self.box_input)
        bottom_bar.addWidget(self.box_wh_check)
        bottom_bar.addWidget(QLabel("W")); bottom_bar.addWidget(self.box_w_input)
        bottom_bar.addWidget(QLabel("H")); bottom_bar.addWidget(self.box_h_input)
        bottom_bar.addSpacing(12)
        bottom_bar.addWidget(self.stop_track_btn)
        bottom_bar.addSpacing(4)
        bottom_bar.addWidget(self.detect_btn)
        bottom_bar.addSpacing(4)
        bottom_bar.addWidget(self.rec_btn)
        bottom_bar.addStretch()
        bottom_bar.addWidget(self.ws_status_label)
        bottom_bar.addSpacing(8)
        bottom_bar.addWidget(self.status_label)
        bottom_bar.addSpacing(8)
        bottom_bar.addWidget(self.connect_btn)
        main_layout.addLayout(bottom_bar)

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

    # ================================================================
    #  Config-tab builders + helpers
    # ================================================================

    def _param_line_edit(self, path, placeholder="", width=80):
        w = QLineEdit()
        w.setFixedWidth(width)
        w.setPlaceholderText(placeholder)
        w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # editingFinished fires on Enter OR focus-loss, so clicking Save
        # without first pressing Enter still sends the new value.
        def commit(p=path, w=w):
            t = w.text().strip()
            if t:
                self._set_param(p, t)
        w.editingFinished.connect(commit)
        self._param_widgets[path] = w
        return w

    def _param_check(self, path, text=""):
        c = QCheckBox(text)

        def on_toggle(on, p=path, w=c):
            # AI features need Detect: ON — block the toggle and warn otherwise.
            if on and (p.startswith("tracker.ai_assist.")
                       or p.startswith("tracker.ai_acquisition.")):
                if not self._detect_on:
                    QMessageBox.warning(
                        self, "Detection is OFF",
                        "AI features need YOLO detection running.\n\n"
                        "Turn ON the Detect button (bottom bar) first, then "
                        "enable this option.")
                    w.blockSignals(True)
                    w.setChecked(False)
                    w.blockSignals(False)
                    return
            self._set_param(p, "true" if on else "false")

        c.toggled.connect(on_toggle)
        self._param_widgets[path] = c
        return c

    def _section_header(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #cccccc; font-size: 11px; font-weight: bold; "
            "border-top: 1px solid #333; padding-top: 6px; margin-top: 4px;"
        )
        return lbl

    def _build_tracker_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(10, 8, 10, 8)
        form.setSpacing(6)

        # --- Init box & assists ---
        form.addRow(self._section_header("Init box"))
        form.addRow("Default box W:", self._param_line_edit("tracker.box_w_default"))
        form.addRow("Default box H:", self._param_line_edit("tracker.box_h_default"))
        form.addRow("Min box size:",  self._param_line_edit("tracker.box_min"))
        form.addRow("Max box size:",  self._param_line_edit("tracker.box_max"))
        form.addRow("AI box size:",   self._param_check("tracker.ai_box_size.enabled"))

        self.ai_box_class = QComboBox()
        self.ai_box_class.addItem("Vehicle", "vehicle")
        self.ai_box_class.addItem("Person",  "person")
        self.ai_box_class.currentIndexChanged.connect(
            lambda i: self._set_param("tracker.ai_box_size.class",
                                      self.ai_box_class.itemData(i)) if i >= 0 else None)
        self._param_widgets["tracker.ai_box_size.class"] = self.ai_box_class
        form.addRow("Size from class:", self.ai_box_class)
        form.addRow("Acq. Assist:",   self._param_check("tracker.acq_assist.enabled"))
        form.addRow("Acq. margin:",   self._param_line_edit("tracker.acq_assist.margin"))
        form.addRow("AI Acquisition:",       self._param_check("tracker.ai_acquisition.enabled"))
        form.addRow("Near distance (px):",   self._param_line_edit("tracker.ai_acquisition.near_val"))
        init_note = QLabel("AI box size: auto W/H from median vehicle detection. "
                           "Acq. Assist: refine to corner cluster. "
                           "AI Acquisition: snap to nearest YOLO detection on click.")
        init_note.setStyleSheet("color: #888888; font-size: 10px;")
        init_note.setWordWrap(True)
        form.addRow("", init_note)

        # --- AI Track Assist (periodic snap) ---
        form.addRow(self._section_header("AI Track Assist"))
        form.addRow("Enabled:",            self._param_check("tracker.ai_assist.enabled"))
        form.addRow("Interval (frames):",  self._param_line_edit("tracker.ai_assist.interval"))
        form.addRow("Min IoU:",            self._param_line_edit("tracker.ai_assist.iou_min"))
        form.addRow("Min conf (assist):",  self._param_line_edit("tracker.ai_assist.conf_min"))
        ai_note = QLabel("Periodically snaps tracker to overlapping YOLO detection. "
                         "'Min conf (assist)' is a LOWER threshold than the display "
                         "one — low-confidence detections help assist without "
                         "cluttering the screen.  ▽ on bbox = active.  Requires Detect: ON.")
        ai_note.setStyleSheet("color: #888888; font-size: 10px;")
        ai_note.setWordWrap(True)
        form.addRow("", ai_note)

        # --- Jump detector ---
        form.addRow(self._section_header("Jump detector"))
        form.addRow("Enabled:",            self._param_check("jump_detector.enabled"))
        form.addRow("Distance threshold:", self._param_line_edit("jump_detector.dist_thresh"))
        form.addRow("Size threshold:",     self._param_line_edit("jump_detector.size_thresh"))
        form.addRow("IoU threshold:",      self._param_line_edit("jump_detector.iou_thresh"))

        # --- Kalman filter ---
        form.addRow(self._section_header("Kalman filter (SORT)"))
        form.addRow("Enabled:",         self._param_check("kalman.enabled"))
        form.addRow("Process noise:",   self._param_line_edit("kalman.process_noise"))
        form.addRow("Measure noise:",   self._param_line_edit("kalman.measure_noise"))
        kalman_note = QLabel("Applied on next Stop Track → click.")
        kalman_note.setStyleSheet("color: #888888; font-size: 10px;")
        form.addRow("", kalman_note)
        return w

    def _build_detection_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(10, 8, 10, 8)
        form.setSpacing(6)

        # Model section (populated by list_models WS response)
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(240)
        self.model_combo.addItem("(waiting for list_models…)", "")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        form.addRow("Model:", self.model_combo)

        restart_hint = QLabel("(reloads live; ~3-5s engine load on first inference)")
        restart_hint.setStyleSheet("color: #888888; font-size: 10px;")
        form.addRow("", restart_hint)

        form.addRow("Confidence threshold:", self._param_line_edit("detection.conf_thresh"))
        form.addRow("Top-N per class:",      self._param_line_edit("detection.top_n"))
        form.addRow("Vehicle class names:",  self._param_line_edit("detection.vehicle_names", "car,truck,…", width=240))
        form.addRow("Person class names:",   self._param_line_edit("detection.person_names",  "person,pedestrian", width=240))
        return w

    def _on_model_changed(self, index):
        if index < 0:
            return
        path = self.model_combo.itemData(index)
        if path:
            self._set_param("model.yolo_path", path)

    def _visca(self, cmd):
        """Send a VISCA command via WebSocket."""
        self._ws_client.send(f"visca:{cmd}")
        self.ws_response_label.setText(f"Sent: visca:{cmd}")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _build_camera_tab(self):
        """Sony FCB-EV9520L VISCA camera control tab."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(8, 6, 8, 6)
        form.setSpacing(4)

        # ---- Zoom ----
        form.addRow(self._section_header("Zoom"))
        # Slider: Wide (0) ←→ Tele (16384)
        zs = QHBoxLayout()
        zs.addWidget(QLabel("W"))
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(0, 16384)
        self._zoom_slider.setValue(0)
        self._zoom_slider.sliderReleased.connect(
            lambda: self._visca(f"zoom_direct:{self._zoom_slider.value()}"))
        self._zoom_lbl = QLabel("0")
        self._zoom_lbl.setFixedWidth(40)
        self._zoom_slider.valueChanged.connect(
            lambda v: self._zoom_lbl.setText(str(v)))
        zs.addWidget(self._zoom_slider, 1)
        zs.addWidget(QLabel("T"))
        zs.addWidget(self._zoom_lbl)
        form.addRow(zs)

        zb = QHBoxLayout()
        b = QPushButton("W"); b.setFixedWidth(30)
        b.pressed.connect(lambda: self._visca("zoom_wide:5"))
        b.released.connect(lambda: self._visca("zoom_stop"))
        zb.addWidget(b)
        for label, pos in [("1x",0),("5x",3000),("10x",6000),
                           ("15x",9000),("20x",12000),("30x",16384)]:
            b = QPushButton(label); b.setFixedWidth(32)
            b.clicked.connect(lambda _, p=pos: (
                self._visca(f"zoom_direct:{p}"),
                self._zoom_slider.setValue(p)))
            zb.addWidget(b)
        b = QPushButton("T"); b.setFixedWidth(30)
        b.pressed.connect(lambda: self._visca("zoom_tele:5"))
        b.released.connect(lambda: self._visca("zoom_stop"))
        zb.addWidget(b)
        b = QPushButton("D-Zm"); b.setFixedWidth(40); b.setCheckable(True)
        b.toggled.connect(lambda on: self._visca("dzoom_on" if on else "dzoom_off"))
        zb.addWidget(b)
        form.addRow(zb)

        # ---- Focus ----
        form.addRow(self._section_header("Focus"))
        fr = QHBoxLayout()
        for lbl, cmd in [("AF","focus_auto"),("MF","focus_manual"),("1-Push","focus_one_push")]:
            b = QPushButton(lbl); b.setFixedWidth(50)
            b.clicked.connect(lambda _,c=cmd: self._visca(c))
            fr.addWidget(b)
        fr.addSpacing(8)
        b = QPushButton("Near"); b.setFixedWidth(42)
        b.pressed.connect(lambda: self._visca("focus_near:4"))
        b.released.connect(lambda: self._visca("focus_stop"))
        fr.addWidget(b)
        b = QPushButton("Far"); b.setFixedWidth(42)
        b.pressed.connect(lambda: self._visca("focus_far:4"))
        b.released.connect(lambda: self._visca("focus_stop"))
        fr.addWidget(b)
        fr.addStretch()
        form.addRow(fr)

        # ---- Exposure ----
        form.addRow(self._section_header("Exposure"))
        er = QHBoxLayout()
        for lbl, cmd in [("Auto","ae_auto"),("Shtr","ae_shutter"),
                         ("Iris","ae_iris"),("Man","ae_manual")]:
            b = QPushButton(lbl); b.setFixedWidth(42)
            b.clicked.connect(lambda _,c=cmd: self._visca(c))
            er.addWidget(b)
        er.addSpacing(6)
        b = QPushButton("BL+"); b.setFixedWidth(32)
        b.clicked.connect(lambda: self._visca("backlight_on"))
        er.addWidget(b)
        b = QPushButton("BL-"); b.setFixedWidth(32)
        b.clicked.connect(lambda: self._visca("backlight_off"))
        er.addWidget(b)
        er.addStretch()
        form.addRow(er)

        # ---- White Balance ----
        form.addRow(self._section_header("White Balance"))
        wr = QHBoxLayout()
        for lbl, cmd in [("Auto","wb_auto"),("In","wb_indoor"),("Out","wb_outdoor"),
                         ("ATW","wb_atw"),("1P","wb_one_push")]:
            b = QPushButton(lbl); b.setFixedWidth(38)
            b.clicked.connect(lambda _,c=cmd: self._visca(c))
            wr.addWidget(b)
        wr.addStretch()
        form.addRow(wr)

        # ---- Image ----
        form.addRow(self._section_header("Image"))
        ig = QGridLayout(); ig.setSpacing(3)
        for lbl, cmd, r, c in [
            ("Stab ON","stabilizer_on",0,0), ("Stab OFF","stabilizer_off",0,1),
            ("WDR ON","wdr_on",0,2), ("WDR OFF","wdr_off",0,3),
            ("VE ON","ve_on",1,0), ("Defog Lo","defog_on:1",1,1),
            ("Defog Hi","defog_on:3",1,2), ("Defog OFF","defog_off",1,3),
            ("HiSens+","high_sens_on",2,0), ("HiSens-","high_sens_off",2,1),
        ]:
            b = QPushButton(lbl); b.setFixedHeight(24)
            b.clicked.connect(lambda _,cm=cmd: self._visca(cm))
            ig.addWidget(b, r, c)
        form.addRow(ig)

        # NR + Aperture sliders
        sl = QHBoxLayout()
        sl.addWidget(QLabel("NR"))
        ns = QSlider(Qt.Orientation.Horizontal); ns.setRange(0,5); ns.setValue(2)
        nl = QLabel("2"); nl.setFixedWidth(15)
        ns.valueChanged.connect(lambda v: (nl.setText(str(v)), self._visca(f"nr:{v}")))
        sl.addWidget(ns); sl.addWidget(nl)
        sl.addSpacing(8)
        sl.addWidget(QLabel("Apt"))
        ap = QSlider(Qt.Orientation.Horizontal); ap.setRange(0,15); ap.setValue(8)
        al = QLabel("8"); al.setFixedWidth(15)
        ap.valueChanged.connect(lambda v: (al.setText(str(v)), self._visca(f"aperture:{v}")))
        sl.addWidget(ap); sl.addWidget(al)
        form.addRow(sl)

        # ---- Day/Night ----
        form.addRow(self._section_header("Day / Night"))
        dr = QHBoxLayout()
        for lbl, cmd in [("Auto ICR","auto_icr_on"),("ICR OFF","auto_icr_off"),
                         ("Night","icr_on"),("Day","icr_off")]:
            b = QPushButton(lbl); b.setFixedWidth(58)
            b.clicked.connect(lambda _,c=cmd: self._visca(c))
            dr.addWidget(b)
        dr.addStretch()
        form.addRow(dr)

        # ---- Other ----
        form.addRow(self._section_header("Other"))
        og = QGridLayout(); og.setSpacing(3)
        for lbl, cmd, r, c in [
            ("Flip ON","flip_on",0,0), ("Flip OFF","flip_off",0,1),
            ("Mirror+","mirror_on",0,2), ("Mirror-","mirror_off",0,3),
            ("B&W","bw_on",1,0), ("Color","bw_off",1,1),
            ("Freeze","freeze_on",1,2), ("Unfreeze","freeze_off",1,3),
            ("Lens Init","lens_init",2,0), ("Reset","cam_reset",2,1),
        ]:
            b = QPushButton(lbl); b.setFixedHeight(24)
            b.clicked.connect(lambda _,cm=cmd: self._visca(cm))
            og.addWidget(b, r, c)
        form.addRow(og)

        # ---- Presets ----
        form.addRow(self._section_header("Presets"))
        pr = QHBoxLayout()
        self._preset_spin = QLineEdit("0"); self._preset_spin.setFixedWidth(25)
        self._preset_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pr.addWidget(self._preset_spin)
        for lbl, pfx in [("Save","preset_save"),("Recall","preset_recall"),("Rst","preset_reset")]:
            b = QPushButton(lbl); b.setFixedWidth(48)
            b.clicked.connect(lambda _,cp=pfx: self._visca(
                f"{cp}:{int(self._preset_spin.text())&0xF}"))
            pr.addWidget(b)
        pr.addStretch()
        form.addRow(pr)

        scroll.setWidget(inner)
        return scroll

    def _build_advanced_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        row = QHBoxLayout()
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("Raw WebSocket command (e.g. set_param:jump_detector.enabled=false)")
        self.cmd_input.returnPressed.connect(self._send_command)
        self.send_btn = QPushButton("Send")
        self.send_btn.setFixedWidth(70)
        self.send_btn.clicked.connect(self._send_command)
        row.addWidget(self.cmd_input)
        row.addWidget(self.send_btn)
        v.addLayout(row)

        hint = QLabel("Commands: set_param:PATH=VALUE | get_config | save_default | "
                      "list_sources | source:PATH | rotate:N | click:NX,NY | "
                      "boxsize:N[,M] | detect:on|off | clear_track")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888; font-size: 10px;")
        v.addWidget(hint)
        return w

    def _on_cam_fmt_changed(self, index):
        if index < 0:
            return
        val = self.cam_fmt_combo.itemData(index)
        if val:
            self._ws_client.send(f"cam_format:{val}")
            self.ws_response_label.setText(f"Sent: cam_format:{val}")
            self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _save_default(self):
        """Force any currently-focused QLineEdit to commit its value (by
        pulling focus away), then tell the Jetson to persist the config."""
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit):
            fw.clearFocus()         # triggers editingFinished → set_param
        # Small delay so the set_param packet goes out before save_default
        QTimer.singleShot(50, lambda: self._ws_client.send("save_default"))
        self.ws_response_label.setText("Sent: save_default")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _set_param(self, path, value):
        self._ws_client.send(f"set_param:{path}={value}")
        self.ws_response_label.setText(f"Sent: set_param:{path}={value}")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")

    def _apply_config_to_ui(self, cfg):
        self._config = cfg

        # Sync Detect button from the Jetson's actual runtime state
        rt = cfg.pop("_runtime", {})
        det_on_jetson = rt.get("detection_on", False)
        if det_on_jetson != self._detect_on:
            self._detect_on = det_on_jetson
            self.detect_btn.setText(f"Detect: {'ON' if self._detect_on else 'OFF'}")

        for path, widget in self._param_widgets.items():
            try:
                val = cfg
                for k in path.split("."):
                    val = val[k]
            except (KeyError, TypeError):
                continue
            if isinstance(widget, QCheckBox):
                widget.blockSignals(True)
                widget.setChecked(bool(val))
                widget.blockSignals(False)
            elif isinstance(widget, QComboBox):
                widget.blockSignals(True)
                idx = widget.findData(val)
                if idx >= 0:
                    widget.setCurrentIndex(idx)
                widget.blockSignals(False)
            elif isinstance(widget, QLineEdit):
                widget.blockSignals(True)
                if isinstance(val, list):
                    widget.setText(",".join(str(x) for x in val))
                else:
                    widget.setText(str(val))
                widget.blockSignals(False)

        # Sync the bottom-bar Box / W / H inputs to the saved defaults so the
        # operator sees the same number the Jetson is actually using.
        bw = cfg.get("tracker", {}).get("box_w_default")
        bh = cfg.get("tracker", {}).get("box_h_default")
        if bw is not None and bh is not None:
            self.box_input.blockSignals(True)
            self.box_w_input.blockSignals(True)
            self.box_h_input.blockSignals(True)
            # If W==H, treat as square; otherwise switch to W×H mode automatically
            if bw == bh:
                self.box_input.setText(str(bw))
                self.box_w_input.setText(str(bw))
                self.box_h_input.setText(str(bh))
                self.box_wh_check.blockSignals(True)
                self.box_wh_check.setChecked(False)
                self.box_wh_check.blockSignals(False)
                self.box_input.setEnabled(True)
                self.box_w_input.setEnabled(False)
                self.box_h_input.setEnabled(False)
            else:
                self.box_input.setText(str(bw))
                self.box_w_input.setText(str(bw))
                self.box_h_input.setText(str(bh))
                self.box_wh_check.blockSignals(True)
                self.box_wh_check.setChecked(True)
                self.box_wh_check.blockSignals(False)
                self.box_input.setEnabled(False)
                self.box_w_input.setEnabled(True)
                self.box_h_input.setEnabled(True)
            self.box_input.blockSignals(False)
            self.box_w_input.blockSignals(False)
            self.box_h_input.blockSignals(False)

    def _stop_track(self):
        self._ws_client.send("clear_track")
        self.ws_response_label.setText("Sent: clear_track")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        self.track_label.setText("Track: --")
        self.track_label.setStyleSheet("color: gray; font-size: 11px;")

    def _toggle_detect(self):
        self._detect_on = not self._detect_on
        self.detect_btn.setText(f"Detect: {'ON' if self._detect_on else 'OFF'}")
        self._ws_client.send(f"detect:{'on' if self._detect_on else 'off'}")
        self.ws_response_label.setText(f"Sent: detect:{'on' if self._detect_on else 'off'}")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        if not self._detect_on:
            self.det_label.setText("Det: --")
            self.det_label.setStyleSheet("color: gray; font-size: 11px;")
            # Auto-disable any AI features that depend on detection
            for ai_path in ("tracker.ai_assist.enabled",
                            "tracker.ai_acquisition.enabled"):
                w = self._param_widgets.get(ai_path)
                if w is not None and w.isChecked():
                    w.blockSignals(True)
                    w.setChecked(False)
                    w.blockSignals(False)
                    self._set_param(ai_path, "false")

    def _toggle_record(self):
        self._recording = not self._recording
        if self._recording:
            self._ws_client.send("record:start")
            self.rec_btn.setStyleSheet("background-color: #cc0000; color: white; font-weight: bold;")
            self.rec_label.setText("REC")
            self.rec_label.setStyleSheet("color: #ff0000; font-weight: bold; font-size: 11px;")
        else:
            self._ws_client.send("record:stop")
            self.rec_btn.setStyleSheet("font-weight: bold;")
            self.rec_label.setText("")

    def _on_box_wh_toggled(self, checked):
        """Toggle between square (Box) and separate W/H modes."""
        self.box_input.setEnabled(not checked)
        self.box_w_input.setEnabled(checked)
        self.box_h_input.setEnabled(checked)
        if checked:
            # Seed W/H from the current square Box value for convenience
            try:
                s = int(self.box_input.text())
                self.box_w_input.setText(str(s))
                self.box_h_input.setText(str(s))
            except ValueError:
                pass
        self._send_box_size()

    def _send_box_size(self):
        # Pull live min/max from the config we got from the Jetson; fall back
        # to safe wide bounds if config hasn't arrived yet.
        tcfg = self._config.get("tracker", {})
        mn = int(tcfg.get("box_min", 10))
        mx = int(tcfg.get("box_max", 400))
        try:
            if self.box_wh_check.isChecked():
                w = max(mn, min(mx, int(self.box_w_input.text())))
                h = max(mn, min(mx, int(self.box_h_input.text())))
                self.box_w_input.setText(str(w))
                self.box_h_input.setText(str(h))
                self._ws_client.send(f"boxsize:{w},{h}")
                self.ws_response_label.setText(f"Sent: boxsize:{w},{h}")
            else:
                size = max(mn, min(mx, int(self.box_input.text())))
                self.box_input.setText(str(size))
                self._ws_client.send(f"boxsize:{size}")
                self.ws_response_label.setText(f"Sent: boxsize:{size}")
            self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        except ValueError:
            # Restore last known values from cfg
            bw = tcfg.get("box_w_default", 20)
            bh = tcfg.get("box_h_default", 20)
            self.box_input.setText(str(bw))
            self.box_w_input.setText(str(bw))
            self.box_h_input.setText(str(bh))

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
        if msg.startswith("visca_reply:") or msg.startswith("VISCA OK:") or msg.startswith("VISCA Error:"):
            self.ws_response_label.setText(f"Camera: {msg}")
            color = "#00cc44" if "OK" in msg or "reply" in msg else "#ff3333"
            self.ws_response_label.setStyleSheet(f"color: {color}; font-size: 11px;")
            return
        if msg.startswith("status:tracking "):
            info = msg[len("status:tracking "):]
            self.track_label.setText(f"Track: {info}")
            self.track_label.setStyleSheet("color: #00cc44; font-size: 11px;")
            return
        if msg.startswith("status:detection "):
            info = msg[len("status:detection "):]
            self.det_label.setText(f"Det: {info}")
            self.det_label.setStyleSheet("color: #00cc44; font-size: 11px;")
            return
        if msg.startswith("status:det_error "):
            err = msg[len("status:det_error "):]
            self.det_label.setText(f"Det ERR: {err[:80]}")
            self.det_label.setStyleSheet("color: #ff3333; font-size: 11px;")
            return
        if msg.startswith("status:recording "):
            info = msg[len("status:recording "):]
            self.rec_label.setText(f"REC {info}")
            self.rec_label.setStyleSheet("color: #ff0000; font-weight: bold; font-size: 11px;")
            return
        if msg.startswith("status:boxsize_auto "):
            try:
                parts = msg[len("status:boxsize_auto "):].split(",")
                aw, ah = parts[0].strip(), parts[1].strip()
                for path, val in [("tracker.box_w_default", aw), ("tracker.box_h_default", ah)]:
                    w = self._param_widgets.get(path)
                    if w:
                        w.blockSignals(True); w.setText(val); w.blockSignals(False)
                self.box_input.blockSignals(True); self.box_input.setText(aw); self.box_input.blockSignals(False)
                self.box_w_input.blockSignals(True); self.box_w_input.setText(aw); self.box_w_input.blockSignals(False)
                self.box_h_input.blockSignals(True); self.box_h_input.setText(ah); self.box_h_input.blockSignals(False)
                self.ws_response_label.setText(f"AI box size: {aw}x{ah}")
                self.ws_response_label.setStyleSheet("color: #00cccc; font-size: 11px;")
            except Exception:
                pass
            return
        if msg.startswith("status:lost "):
            reason = msg[len("status:lost "):]
            self.track_label.setText(f"Lost: {reason}")
            self.track_label.setStyleSheet("color: #ff8800; font-size: 11px;")
            return
        if msg.startswith("config:"):
            try:
                cfg = json.loads(msg[7:])
                self._apply_config_to_ui(cfg)
            except Exception as e:
                print(f"[GS] config parse failed: {e}")
            return
        if msg.startswith("models:"):
            try:
                data = json.loads(msg[7:])
                models  = data.get("models", [])
                current = data.get("current", "")
                self.model_combo.blockSignals(True)
                self.model_combo.clear()
                select_idx = 0
                for i, m in enumerate(models):
                    self.model_combo.addItem(os.path.basename(m), m)
                    if m == current:
                        select_idx = i
                if not models:
                    self.model_combo.addItem("(no models found)", "")
                self.model_combo.setCurrentIndex(select_idx)
                self.model_combo.blockSignals(False)
            except Exception as e:
                print(f"[GS] models parse failed: {e}")
            return
        if msg.startswith("cam_formats:"):
            try:
                data = json.loads(msg[12:])
                fmts = data.get("formats", [])
                err  = data.get("error", "")
                dev  = data.get("device", "")
                self.cam_fmt_combo.blockSignals(True)
                self.cam_fmt_combo.clear()
                self.cam_fmt_combo.addItem("Auto", "auto")
                # Read current config to highlight the active format
                cc = self._config.get("camera", {})
                cur_w = int(cc.get("width", 0))
                cur_h = int(cc.get("height", 0))
                cur_f = int(cc.get("fps", 0))
                cur_str = f"{cur_w}x{cur_h}@{cur_f}" if cur_w else "auto"
                select_idx = 0
                for i, fmt in enumerate(fmts):
                    self.cam_fmt_combo.addItem(fmt, fmt)
                    if fmt == cur_str:
                        select_idx = i + 1   # +1 because Auto is index 0
                self.cam_fmt_combo.setCurrentIndex(select_idx)
                self.cam_fmt_combo.blockSignals(False)
                if err:
                    self.ws_response_label.setText(f"Format probe error: {err}")
                    self.ws_response_label.setStyleSheet("color: #ff3333; font-size: 11px;")
                else:
                    self.ws_response_label.setText(f"Found {len(fmts)} formats for {dev}")
                    self.ws_response_label.setStyleSheet("color: #00cc44; font-size: 11px;")
            except Exception as e:
                print(f"[GS] cam_formats parse failed: {e}")
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
            self._ws_client.send("list_models")
            self._ws_client.send("get_config")
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

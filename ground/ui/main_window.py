"""GroundStation main window — the orchestrator.

Assembles the UI (video area, config panel, health bar, bottom bar),
wires widget signals to handlers in actions.py, and routes incoming
WebSocket messages to the UI via ws_router.route_message."""
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QTabWidget, QSizePolicy,
)

from gi.repository import GLib

from ground.constants import WS_URL
from ground.net import WSClient, GstSignals
from ground.ui import actions
from ground.ui.video_widget import ClickableVideoWidget
from ground.ui.offline_screen import OfflineScreen
from ground.ui.health_bar import HealthBar
from ground.ui.bottom_bar import build_bottom_bar
from ground.ui.tabs import (
    build_tracker_tab, build_detection_tab, build_camera_tab, build_advanced_tab,
)
from ground.ui.config_sync import apply_config_to_widgets, sync_bottom_box_inputs
from ground.ui.ws_router import route_message
from ground.ui.stream_controller import StreamController


class GroundStation(QMainWindow):
    """Main window — UI assembly + high-level slot bindings."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ground Station")
        self.resize(1280, 800)

        # GLib main loop (GStreamer bus dispatch)
        self._glib_loop = GLib.MainLoop()
        threading.Thread(target=self._glib_loop.run, daemon=True).start()

        # ---- State ----
        self.param_widgets = {}
        self.config = {}
        self.paused = False
        self.recording = False
        self.detect_on = False
        self._rotate_index = 0
        self._rotate_labels = ["Rotate: 0°", "Rotate: 90°",
                               "Rotate: 180°", "Rotate: 270°"]

        # ---- Signals + WebSocket ----
        self.signals = GstSignals()
        self.signals.ws_message.connect(self._on_ws_message)
        self.signals.ws_status.connect(self._on_ws_status)
        self.signals.status_changed.connect(self._on_status_changed)

        self.ws_client = WSClient(
            WS_URL,
            on_message=lambda msg: self.signals.ws_message.emit(msg),
            on_status =lambda s:   self.signals.ws_status.emit(s),
        )
        self.ws_client.connect()

        self._build_ui()
        self._wire_signals()

    # ================================================================
    #  UI construction
    # ================================================================

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        top = QHBoxLayout(); top.setSpacing(6)
        top.addWidget(self._build_video_area(), 1)
        top.addWidget(self._build_config_panel())
        root.addLayout(top, 1)

        self.health = HealthBar()
        root.addLayout(self.health)

        self.ws_response_label = QLabel("")
        self.ws_response_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        root.addWidget(self.ws_response_label)

        bar, self.bottom = build_bottom_bar()
        root.addLayout(bar)

    def _build_video_area(self):
        self.video_stack = QStackedWidget()
        self.video_stack.setMinimumSize(640, 360)
        self.video_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.video_widget = ClickableVideoWidget(
            lambda nx, ny: actions.on_video_click(self, nx, ny),
            lambda nx, ny, nw, nh: actions.on_video_drag(self, nx, ny, nw, nh))
        self.video_widget.setStyleSheet("background-color: black;")
        self.video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_stack.addWidget(self.video_widget)

        self.offline = OfflineScreen()
        self.video_stack.addWidget(self.offline)
        return self.video_stack

    def _build_config_panel(self):
        panel = QWidget()
        panel.setFixedWidth(340)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        tabs = QTabWidget()
        tabs.addTab(
            build_tracker_tab(self.param_widgets,
                              lambda p, v: actions.set_param(self, p, v),
                              lambda: self.detect_on),
            "Tracker")
        self.detection_tab, det_widget = build_detection_tab(
            self.param_widgets, lambda p, v: actions.set_param(self, p, v))
        tabs.addTab(det_widget, "Detection")
        tabs.addTab(build_camera_tab(lambda cmd: actions.visca(self, cmd)),
                    "Camera")
        self.advanced_tab, adv_widget = build_advanced_tab(
            lambda t: actions.send_raw(self, t))
        tabs.addTab(adv_widget, "Advanced")
        layout.addWidget(tabs, 1)

        save_btn = QPushButton("Save as Default (Jetson)")
        save_btn.setToolTip(
            "Write the current Jetson-side config to config/default.yaml "
            "so it is loaded on the next stream.py start.")
        save_btn.clicked.connect(lambda: actions.save_default(self))
        layout.addWidget(save_btn)

        # Camera-format picker row
        row = QHBoxLayout()
        self.cam_fmt_combo = QComboBox()
        self.cam_fmt_combo.setMinimumWidth(180)
        self.cam_fmt_combo.addItem("Auto", "auto")
        self.cam_fmt_combo.currentIndexChanged.connect(
            lambda i: actions.on_cam_fmt_changed(self, i))
        probe_btn = QPushButton("Probe"); probe_btn.setFixedWidth(60)
        probe_btn.setToolTip("Query the camera for supported resolutions/framerates")
        probe_btn.clicked.connect(lambda: self.ws_client.send("list_cam_formats"))
        row.addWidget(QLabel("Cam format:"))
        row.addWidget(self.cam_fmt_combo)
        row.addWidget(probe_btn)
        layout.addLayout(row)

        return panel

    # ================================================================
    #  Signal wiring — bottom bar + stream controller
    # ================================================================

    def _wire_signals(self):
        b = self.bottom
        b.source_combo.currentIndexChanged.connect(
            lambda i: actions.on_source_changed(self, i))
        b.rotate_btn.clicked.connect(lambda: actions.send_rotate(self))
        b.connect_btn.clicked.connect(self._toggle_stream)
        b.box_input.editingFinished.connect(lambda: actions.send_box_size(self))
        b.box_wh_check.toggled.connect(
            lambda c: actions.on_box_wh_toggled(self, c))
        b.box_w_input.editingFinished.connect(lambda: actions.send_box_size(self))
        b.box_h_input.editingFinished.connect(lambda: actions.send_box_size(self))
        b.stop_track_btn.clicked.connect(lambda: actions.stop_track(self))
        b.detect_btn.clicked.connect(lambda: actions.toggle_detect(self))
        b.pause_btn.clicked.connect(lambda: actions.toggle_pause(self))
        b.rec_btn.clicked.connect(lambda: actions.toggle_record(self))

        self.stream_ctl = StreamController(
            signals=self.signals,
            video_widget=self.video_widget,
            window_for_timers=self,
            on_stream_playing=_StreamCallbacks(self),
            on_offline_screen=self._show_offline,
            on_reconnect_display=self._show_reconnecting,
        )
        self.signals.reconnect.connect(lambda: self.health.reset())

    # ================================================================
    #  Stream toggling + offline display
    # ================================================================

    def _toggle_stream(self):
        if self.stream_ctl.player.pipeline is None:
            self.stream_ctl.start_stream()
            self.bottom.connect_btn.setText("Disconnect")
            self.bottom.status_label.setText("Connecting...")
            self.bottom.status_label.setStyleSheet("color: orange;")
        else:
            self.stream_ctl.stop_stream()
            self.video_stack.setCurrentIndex(0)
            self.bottom.connect_btn.setText("Connect")
            self.bottom.status_label.setText("Disconnected")
            self.bottom.status_label.setStyleSheet("color: gray;")
            self.health.reset()
            self.stream_ctl.reset_reconnect_count()
            self.health.reconnect_label.setText("Reconnects: 0")

    def _show_offline(self, attempt, delay_s):
        self.bottom.status_label.setText("Jetson Offline")
        self.bottom.status_label.setStyleSheet("color: red;")
        self.health.set_offline()
        self.offline.set_attempt(
            f"Attempt {attempt}  —  next retry in {delay_s}s")
        self.video_stack.setCurrentIndex(1)
        if not hasattr(self, "_blink_timer"):
            self._blink_timer = QTimer(self)
            self._blink_timer.setInterval(800)
            self._blink_timer.timeout.connect(self.offline.blink)
        self._blink_timer.start()

    def _show_reconnecting(self, attempt):
        self.bottom.status_label.setText("Reconnecting...")
        self.bottom.status_label.setStyleSheet("color: orange;")

    def _on_status_changed(self, text):
        self.bottom.status_label.setText(text)
        self.bottom.status_label.setStyleSheet("color: green;")

    # ================================================================
    #  Config sync + WS routing
    # ================================================================

    def apply_config(self, cfg):
        """Called from ws_router when 'config:{...}' arrives."""
        self.config = cfg
        rt = cfg.pop("_runtime", {})
        det_on_jetson = rt.get("detection_on", False)
        if det_on_jetson != self.detect_on:
            self.detect_on = det_on_jetson
            self.bottom.detect_btn.setText(
                f"Detect: {'ON' if self.detect_on else 'OFF'}")
        apply_config_to_widgets(self.param_widgets, cfg)
        tcfg = cfg.get("tracker", {})
        sync_bottom_box_inputs(
            tcfg.get("box_w_default"), tcfg.get("box_h_default"),
            self.bottom.box_input, self.bottom.box_w_input, self.bottom.box_h_input,
            self.bottom.box_wh_check)

    def _on_ws_message(self, msg):
        route_message(self, msg)

    def _on_ws_status(self, status):
        self.bottom.ws_status_label.setText(f"WS: {status}")
        if "Connected" in status:
            self.bottom.ws_status_label.setStyleSheet(
                "color: #00cc44; font-size: 11px;")
            self.ws_client.send("list_sources")
            self.ws_client.send("list_models")
            self.ws_client.send("get_config")
        elif "Error" in status or "Disconnected" in status:
            self.bottom.ws_status_label.setStyleSheet(
                "color: #ff3333; font-size: 11px;")
        else:
            self.bottom.ws_status_label.setStyleSheet(
                "color: orange; font-size: 11px;")

    # ================================================================
    #  Qt lifecycle
    # ================================================================

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.stream_ctl.expose()

    def closeEvent(self, event):
        self.ws_client.disconnect()
        self.stream_ctl.stop_stream()
        event.accept()


class _StreamCallbacks:
    """Adapter so StreamController can call back without a circular init."""
    def __init__(self, window):
        self._w = window

    def on_playing(self):
        self._w.video_stack.setCurrentIndex(0)
        if hasattr(self._w, "_blink_timer"):
            self._w._blink_timer.stop()

    def update_health(self, fps, health_pct, uptime):
        self._w.health.update_stream_health(fps, health_pct, uptime)

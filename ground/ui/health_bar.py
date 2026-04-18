"""Health bar: dot, FPS, health %, uptime, reconnects, track/det/rec status."""
from PyQt6.QtWidgets import QHBoxLayout, QLabel


class HealthBar(QHBoxLayout):
    """Row of status labels shown below the video."""

    def __init__(self):
        super().__init__()

        self.dot_label       = QLabel("●")
        self.fps_label       = QLabel("-- fps")
        self.health_label    = QLabel("Health: --%")
        self.uptime_label    = QLabel("Uptime: --:--")
        self.reconnect_label = QLabel("Reconnects: 0")
        self.track_label     = QLabel("Track: --")
        self.det_label       = QLabel("Det: --")
        self.rec_label       = QLabel("")

        for lbl in (self.fps_label, self.health_label, self.uptime_label,
                    self.reconnect_label, self.track_label, self.det_label,
                    self.rec_label):
            lbl.setStyleSheet("color: gray; font-size: 11px;")
        self.dot_label.setStyleSheet("color: gray; font-size: 14px;")

        self.addWidget(self.dot_label);       self.addSpacing(4)
        self.addWidget(self.fps_label);       self.addSpacing(12)
        self.addWidget(self.health_label);    self.addSpacing(12)
        self.addWidget(self.uptime_label);    self.addSpacing(12)
        self.addWidget(self.reconnect_label); self.addSpacing(12)
        self.addWidget(self.track_label);     self.addSpacing(12)
        self.addWidget(self.det_label);       self.addSpacing(12)
        self.addWidget(self.rec_label)
        self.addStretch()

    # ---- mutation helpers (called from MainWindow slots) ----

    def update_stream_health(self, fps, health_pct, uptime_text):
        """Refresh FPS/health/uptime with colour based on health."""
        if health_pct >= 80:
            dot_color = text_color = "#00cc44"
        elif health_pct >= 40:
            dot_color = text_color = "#ffaa00"
        else:
            dot_color = text_color = "#ff3333"

        self.dot_label.setStyleSheet(f"color: {dot_color}; font-size: 14px;")
        for lbl in (self.fps_label, self.health_label, self.uptime_label):
            lbl.setStyleSheet(f"color: {text_color}; font-size: 11px;")
        self.fps_label.setText(f"{fps} fps")
        self.health_label.setText(f"Health: {health_pct}%")
        self.uptime_label.setText(f"Uptime: {uptime_text}")

    def reset(self):
        for lbl in (self.dot_label, self.fps_label, self.health_label,
                    self.uptime_label, self.reconnect_label):
            lbl.setStyleSheet("color: gray; font-size: 11px;")
        self.dot_label.setStyleSheet("color: gray; font-size: 14px;")
        self.fps_label.setText("-- fps")
        self.health_label.setText("Health: --%")
        self.uptime_label.setText("Uptime: --:--")
        self.reconnect_label.setText("Reconnects: 0")

    def set_offline(self):
        self.dot_label.setStyleSheet("color: red; font-size: 14px;")

"""Stream lifecycle controller: manages the RTSP player, watchdog,
reconnect logic, and frame-rate health metrics."""
import time
from collections import deque

from PyQt6.QtCore import QTimer

from ground.constants import (
    EXPECTED_FPS, WATCHDOG_INTERVAL_MS, WATCHDOG_TIMEOUT_S, RTSP_URL,
)
from ground.stream import RTSPPlayer


class StreamController:
    """Owns the RTSPPlayer + all reconnect/health timers.  The MainWindow
    just calls start_stream() / stop_stream() and hooks its widgets up
    to the signals."""

    BACKOFF_DELAYS = [0.5, 1, 2, 5, 10]   # seconds between retries

    def __init__(self, signals, video_widget, window_for_timers,
                 on_stream_playing, on_offline_screen, on_reconnect_display):
        self._signals          = signals
        self._video_widget     = video_widget
        self.player = RTSPPlayer(RTSP_URL, signals,
                                 window_id_provider=lambda: int(video_widget.winId()))

        self._frame_times     = deque()   # for FPS
        self._reconnect_count = 0
        self._failed_attempts = 0
        self._connect_ts      = 0.0

        # Callbacks into the main window
        self._on_stream_playing    = on_stream_playing
        self._on_offline_screen    = on_offline_screen
        self._on_reconnect_display = on_reconnect_display

        # Timers
        self._watchdog = QTimer(window_for_timers)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_watchdog)

        self._health_timer = QTimer(window_for_timers)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._update_health)

        # Signal wiring
        signals.frame_received.connect(self._on_frame_received)
        signals.reconnect.connect(self._do_reconnect)
        signals.stream_playing.connect(self._handle_stream_playing)

    # ---- public API ----

    def start_stream(self):
        self.player.auto_reconnect = True
        self.player.reset_last_frame_ts()
        self._frame_times.clear()
        self.player.start()

    def stop_stream(self):
        self.player.auto_reconnect = False
        self._failed_attempts = 0
        self._watchdog.stop()
        self._health_timer.stop()
        self.player.stop()

    def expose(self):
        self.player.expose()

    @property
    def reconnect_count(self):
        return self._reconnect_count

    def reset_reconnect_count(self):
        self._reconnect_count = 0

    # ---- reconnect / watchdog ----

    def _do_reconnect(self):
        self._reconnect_count += 1
        self._failed_attempts += 1
        self._watchdog.stop()
        self._health_timer.stop()
        self.player.stop()

        if self._failed_attempts >= 3:
            idx = min(self._failed_attempts - 1, len(self.BACKOFF_DELAYS) - 1)
            delay = self.BACKOFF_DELAYS[idx]
            self._on_offline_screen(self._failed_attempts, delay)
        else:
            self._on_reconnect_display(self._failed_attempts)

        idx = min(self._failed_attempts - 1, len(self.BACKOFF_DELAYS) - 1)
        delay_ms = int(self.BACKOFF_DELAYS[idx] * 1000)
        print(f"[RECONNECT] Attempt {self._failed_attempts}, retrying in {delay_ms}ms")
        QTimer.singleShot(delay_ms, self.start_stream)

    def _check_watchdog(self):
        if not self.player.auto_reconnect or self.player.pipeline is None:
            return
        age = time.monotonic() - self.player.last_frame_ts
        if age > WATCHDOG_TIMEOUT_S:
            print(f"[WATCHDOG] No frame for {age:.1f}s — reconnecting")
            self._signals.reconnect.emit()

    # ---- health ----

    def _on_frame_received(self):
        now = time.monotonic()
        self._frame_times.append(now)
        cutoff = now - 2.0
        while self._frame_times and self._frame_times[0] < cutoff:
            self._frame_times.popleft()

    def _update_health(self):
        if not self.player.auto_reconnect or self.player.pipeline is None:
            return
        now = time.monotonic()
        fps = sum(1 for t in self._frame_times if t > now - 1.0)
        health = min(100, int(fps / EXPECTED_FPS * 100))
        elapsed = int(now - self._connect_ts)
        uptime = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        self._on_stream_playing.update_health(fps, health, uptime)

    # ---- stream playing signal ----

    def _handle_stream_playing(self):
        self._connect_ts      = time.monotonic()
        self._failed_attempts = 0
        self._frame_times.clear()
        if not self._watchdog.isActive():
            self._watchdog.start()
        if not self._health_timer.isActive():
            self._health_timer.start()
        self._on_stream_playing.on_playing()

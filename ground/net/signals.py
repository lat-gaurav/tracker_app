"""Qt signal bridge for cross-thread messaging."""
from PyQt6.QtCore import QObject, pyqtSignal


class GstSignals(QObject):
    """Signals that cross from GStreamer / WS threads into the Qt main thread."""
    status_changed = pyqtSignal(str)     # stream state text
    reconnect      = pyqtSignal()        # RTSP watchdog tripped
    stream_playing = pyqtSignal()        # RTSP pipeline reached PLAYING
    frame_received = pyqtSignal()        # one frame emerged from the decoder
    ws_message     = pyqtSignal(str)     # message received from Jetson
    ws_status      = pyqtSignal(str)     # WS connection state text

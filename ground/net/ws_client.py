"""Thin WebSocket client running in a background thread.

Delivers received messages and status changes via the two callbacks
supplied at construction time.  The callbacks typically forward the
data to Qt via pyqtSignal.emit() so the UI thread handles it.
"""
import threading

import websocket   # pip: websocket-client


class WSClient:
    def __init__(self, url, on_message, on_status):
        self._url        = url
        self._on_message = on_message
        self._on_status  = on_status
        self._ws         = None
        self._thread     = None
        self._running    = False

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

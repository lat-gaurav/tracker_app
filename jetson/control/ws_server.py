"""WebSocket server: listens for commands from the ground station and
periodically pushes status updates (tracking/detection/recording/pause).
"""
import asyncio
import threading
import time

import websockets

from jetson.constants import WS_PORT
from jetson.control.ws_handler import Context, handle_message


class WebSocketServer:
    """Async WebSocket server running in its own thread.
    One Context (shared processor/recorder/etc.) serves every client."""

    STATUS_INTERVAL = 1.0   # seconds between status pushes

    def __init__(self, context: Context):
        self.ctx = context
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- private ----

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        async with websockets.serve(self._handler, "0.0.0.0", WS_PORT):
            print(f"[OK]    WebSocket server ready on port {WS_PORT}")
            await asyncio.Future()   # run forever

    async def _handler(self, websocket):
        addr = websocket.remote_address
        print(f"[WS]   Client connected: {addr}")

        status_task = asyncio.ensure_future(self._status_sender(websocket))
        try:
            async for message in websocket:
                print(f"[WS]   Received: {message}")
                reply = handle_message(self.ctx, message)
                self.ctx.recorder.log_input(message)
                await websocket.send(reply)
                print(f"[WS]   Sent: {reply}")
        except websockets.exceptions.ConnectionClosed:
            print(f"[WS]   Client disconnected: {addr}")
        finally:
            status_task.cancel()

    async def _status_sender(self, websocket):
        """Push tracking + detection + recording stats at STATUS_INTERVAL."""
        was_paused = False
        try:
            while True:
                await asyncio.sleep(self.STATUS_INTERVAL)

                # Pause / resume notifications
                paused_now, _, catching_up = self.ctx.processor.is_paused()
                if was_paused and not paused_now:
                    await websocket.send("status:resumed")
                was_paused = paused_now or catching_up

                # Tracking
                tracking, t_ms = self.ctx.processor.get_track_info()
                if tracking:
                    await websocket.send(f"status:tracking {t_ms:.1f}ms")

                # Auto-box-size notification
                with self.ctx.processor._lock:
                    auto_box = self.ctx.processor._auto_box_notify
                    self.ctx.processor._auto_box_notify = None
                if auto_box:
                    await websocket.send(
                        f"status:boxsize_auto {auto_box[0]},{auto_box[1]}")

                # Recording
                if self.ctx.recorder.is_active():
                    elapsed = int(time.time() - self.ctx.recorder.start_ts)
                    mm, ss = divmod(elapsed, 60)
                    await websocket.send(
                        f"status:recording {mm:02d}:{ss:02d} "
                        f"frames={self.ctx.recorder.frame_count}")

                # Track-lost reason
                lost = self.ctx.processor.get_last_lost_reason()
                if lost:
                    await websocket.send(f"status:lost {lost}")

                # Detection
                det_on, d_ms, d_count, d_err = self.ctx.processor.get_det_info()
                if det_on:
                    if d_err:
                        await websocket.send(f"status:det_error {d_err}")
                    else:
                        await websocket.send(
                            f"status:detection {d_ms:.1f}ms objects={d_count}")
        except asyncio.CancelledError:
            pass

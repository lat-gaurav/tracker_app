import cv2
import numpy as np
import threading
import time


def _create_tracker():
    if hasattr(cv2, "TrackerCSRT_create"):
        return cv2.TrackerCSRT_create()
    if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create"):
        return cv2.legacy.TrackerCSRT_create()
    raise RuntimeError("CSRT tracker not available — install opencv-contrib-python")


class FrameProcessor:
    """
    CSRT object tracker running in its own thread.

    Capture thread calls submit_frame() + draw() at full 30fps.
    Tracker thread runs CSRT update at whatever rate the CPU allows.
    The two never block each other.
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self._latest_frame  = None   # latest frame for tracker (written by capture, read by tracker)
        self._pending_click = None   # (ox, oy) tuple or "clear" sentinel
        self._box_size      = 20     # init box side length in pixels
        self._bbox          = None   # last known (x, y, w, h) — shared between threads
        self._track_ms      = 0.0    # latest update() time in ms

        self._track_count = 0

        self._tracker_thread = threading.Thread(target=self._tracker_loop, daemon=True)
        self._tracker_thread.start()

    # ------------------------------------------------------------------ capture thread API

    def submit_frame(self, frame: np.ndarray):
        """Store a copy of the current frame for the tracker thread."""
        with self._lock:
            self._latest_frame = frame.copy()

    def draw(self, frame: np.ndarray) -> np.ndarray:
        """Draw the last known bbox on the frame. Fast — no tracking here."""
        with self._lock:
            bbox = self._bbox
        if bbox is not None:
            frame = self._draw_bbox(frame, bbox)
        return frame

    # ------------------------------------------------------------------ setters (WS thread)

    def set_click(self, ox: float, oy: float):
        with self._lock:
            self._pending_click = (ox, oy)
        print(f"[PROC]  set_click({ox:.3f}, {oy:.3f})")

    def set_box_size(self, size: int):
        with self._lock:
            self._box_size = max(10, min(200, size))
        print(f"[PROC]  set_box_size({self._box_size})")

    def clear_track(self):
        with self._lock:
            self._pending_click = "clear"
            self._bbox = None
            self._track_ms = 0.0
        print("[PROC]  clear_track()")

    def get_track_info(self):
        """Returns (is_tracking, ms_per_update). Thread-safe."""
        with self._lock:
            return self._bbox is not None, self._track_ms

    # ------------------------------------------------------------------ tracker thread

    def _tracker_loop(self):
        tracker = None

        while True:
            with self._lock:
                frame = self._latest_frame
                self._latest_frame = None

            if frame is None:
                time.sleep(0.005)
                continue

            # Only consume pending click when we have a frame to act on
            with self._lock:
                pending  = self._pending_click
                self._pending_click = None
                box_size = self._box_size

            h, w = frame.shape[:2]

            # Clear command
            if pending == "clear":
                tracker = None
                continue

            # New click → init tracker on this frame
            if pending is not None:
                ox, oy = pending
                cx = int(ox * w)
                cy = int(oy * h)
                half = box_size // 2
                x = max(0, min(cx - half, w - box_size))
                y = max(0, min(cy - half, h - box_size))
                bbox = (x, y, box_size, box_size)
                tracker = _create_tracker()
                tracker.init(frame, bbox)
                with self._lock:
                    self._bbox = bbox
                print(f"[PROC]  tracker init at ({cx},{cy}) bbox={bbox} on {w}x{h}")
                continue

            # Update existing tracker
            if tracker is not None:
                t0 = time.monotonic()
                success, bbox = tracker.update(frame)
                dt_ms = (time.monotonic() - t0) * 1000

                if success:
                    self._track_count += 1
                    with self._lock:
                        self._bbox = tuple(int(v) for v in bbox)
                        self._track_ms = dt_ms
                    if self._track_count == 1 or self._track_count % 30 == 0:
                        print(f"[PROC]  update #{self._track_count}  {dt_ms:.1f}ms  bbox={self._bbox}")
                else:
                    print(f"[PROC]  tracker LOST ({dt_ms:.1f}ms)")
                    tracker = None
                    with self._lock:
                        self._bbox = None
                        self._track_ms = 0.0

    # ------------------------------------------------------------------ drawing

    def _draw_bbox(self, frame: np.ndarray, bbox: tuple) -> np.ndarray:
        x, y, bw, bh = bbox
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        cx = x + bw // 2
        cy = y + bh // 2
        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)
        return frame

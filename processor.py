import cv2
import numpy as np
import threading


class FrameProcessor:
    """
    Pure OpenCV image processing — no GStreamer, no network.
    All operations work in the original (pre-rotation) frame coordinate space.
    Add new processing steps here as methods and call them from process().
    """

    def __init__(self):
        self._lock       = threading.Lock()
        self._click      = None   # (ox, oy) normalized 0-1 in original frame space
        self._draw_count = 0

    # ------------------------------------------------------------------ setters

    def set_click(self, ox: float, oy: float):
        with self._lock:
            self._click = (ox, oy)
        print(f"[PROC]  set_click({ox:.3f}, {oy:.3f})")

    def clear_click(self):
        with self._lock:
            self._click = None
        print("[PROC]  clear_click()")

    # ------------------------------------------------------------------ main pipeline

    def process(self, frame: np.ndarray) -> np.ndarray:
        with self._lock:
            click = self._click

        if click is not None:
            self._draw_count += 1
            if self._draw_count == 1 or self._draw_count % 90 == 0:
                h, w = frame.shape[:2]
                print(f"[PROC]  drawing click {click} on {w}x{h} frame (draw_count={self._draw_count})")
            frame = self._draw_click_marker(frame, click)

        return frame

    # ------------------------------------------------------------------ drawing

    def _draw_click_marker(self, frame: np.ndarray, click: tuple) -> np.ndarray:
        h, w  = frame.shape[:2]
        cx    = int(click[0] * w)
        cy    = int(click[1] * h)
        color = (30, 30, 255)   # BGR red
        cv2.circle(frame, (cx, cy), 18, color, 2)
        cv2.circle(frame, (cx, cy),  4, color, -1)
        cv2.line(frame, (cx - 28, cy), (cx + 28, cy), color, 2)
        cv2.line(frame, (cx, cy - 28), (cx, cy + 28), color, 2)
        return frame

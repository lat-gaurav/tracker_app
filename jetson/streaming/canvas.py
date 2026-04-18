"""Canvas helpers: fit arbitrary frames into the fixed streaming canvas."""
import cv2
import numpy as np

from jetson.constants import STREAM_W, STREAM_H


def fit_to_canvas(frame):
    """Centre `frame` inside a STREAM_W × STREAM_H canvas with black padding.
    If the frame is larger than the canvas, scale down preserving aspect ratio.
    If smaller, leave at native pixel resolution (no upscale)."""
    h, w = frame.shape[:2]
    if w > STREAM_W or h > STREAM_H:
        scale = min(STREAM_W / w, STREAM_H / h)
        new_w = int(w * scale) & ~1
        new_h = int(h * scale) & ~1
        frame = cv2.resize(frame, (new_w, new_h))
        h, w = new_h, new_w
    if w == STREAM_W and h == STREAM_H:
        return frame
    canvas = np.zeros((STREAM_H, STREAM_W, 3), dtype=np.uint8)
    dx = (STREAM_W - w) // 2
    dy = (STREAM_H - h) // 2
    canvas[dy:dy+h, dx:dx+w] = frame
    return canvas

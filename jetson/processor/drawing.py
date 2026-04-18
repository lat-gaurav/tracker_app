"""Frame overlay drawing: tracker bbox + detection polygons."""
import cv2
import numpy as np


def draw_bbox(frame, bbox, ai_active=False):
    """Draw tracker bbox with centre dot.  Adds a cyan triangle above the
    box when `ai_active` is True (AI Assist indicator)."""
    x, y, bw, bh = bbox
    cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
    cx = x + bw // 2
    cy = y + bh // 2
    cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)
    if ai_active:
        size = 9
        tri = np.array([
            [cx - size, max(0, y - size - 4)],
            [cx + size, max(0, y - size - 4)],
            [cx,        max(0, y - 2)],
        ], dtype=np.int32)
        cv2.drawContours(frame, [tri], 0, (0, 255, 255), -1)   # cyan fill
        cv2.drawContours(frame, [tri], 0, (0, 0, 0),       1)  # black outline
    return frame


def draw_detection(frame, det, vehicle_names):
    """Draw a YOLO OBB polygon.  Red for vehicles, blue for persons."""
    label_lc = str(det["label"]).lower()
    veh_set = set(n.lower() for n in vehicle_names)
    if any(v in label_lc for v in veh_set):
        color = (0, 0, 255)     # red — vehicles
    else:
        color = (255, 0, 0)     # blue — persons
    poly = det["poly"].astype(np.int32)
    cv2.polylines(frame, [poly], isClosed=True, color=color, thickness=2)
    return frame

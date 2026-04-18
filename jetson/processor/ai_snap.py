"""AI Acquisition + AI Track Assist: snap tracker bbox to YOLO detections.

Both functions are age-gated: if the latest detections are older than
MAX_DET_AGE frames relative to the current frame seq, they are rejected
to avoid snapping to stale positions on moving targets.
"""
import cv2
import numpy as np

from jetson.tracking.jump_detector import JumpDetector

MAX_DET_AGE = 4   # frames; older detections are rejected


def ai_acquisition_snap(processor, click_x, click_y, near_val, current_seq=0):
    """On a fresh click, return the AABB of the YOLO detection whose centre
    is closest to (click_x, click_y) IF within `near_val` pixels.  Uses the
    wider _detections_assist list so low-confidence candidates still qualify.
    Returns None when nothing qualifies or detections are too stale.
    """
    with processor._lock:
        det_age = current_seq - processor._det_seq if current_seq else 0
        if det_age > MAX_DET_AGE:
            return None
        dets = list(processor._detections_assist)

    if not dets:
        return None
    best_dist = float("inf")
    best_aabb = None
    for det in dets:
        poly = det["poly"].astype(np.int32)
        ax, ay, aw, ah = cv2.boundingRect(poly)
        dcx = ax + aw / 2.0
        dcy = ay + ah / 2.0
        d = float(np.hypot(dcx - click_x, dcy - click_y))
        if d < best_dist and d <= near_val:
            best_dist = d
            best_aabb = (ax, ay, aw, ah)
    return best_aabb


def ai_assist_snap(processor, frame, tracker_bbox, iou_min, current_seq=0):
    """If any YOLO detection overlaps the tracker bbox by IoU >= iou_min,
    return (detection_aabb, iou) — caller will reinit the tracker on it.
    Returns None when no detection qualifies or detections are too stale.
    """
    with processor._lock:
        det_age = current_seq - processor._det_seq if current_seq else 0
        if det_age > MAX_DET_AGE:
            return None
        dets = list(processor._detections_assist)

    if not dets:
        return None

    h_frame, w_frame = frame.shape[:2]
    best_iou = 0.0
    best_aabb = None
    for det in dets:
        poly = det["poly"].astype(np.int32)
        ax, ay, aw, ah = cv2.boundingRect(poly)
        ax = max(0, min(ax, w_frame - 1))
        ay = max(0, min(ay, h_frame - 1))
        aw = max(1, min(aw, w_frame - ax))
        ah = max(1, min(ah, h_frame - ay))
        iou = JumpDetector._iou((ax, ay, aw, ah), tracker_bbox)
        if iou > best_iou:
            best_iou = iou
            best_aabb = (ax, ay, aw, ah)

    if best_aabb is not None and best_iou >= iou_min:
        return best_aabb, best_iou
    return None


def get_auto_box_size(processor, cls, max_side):
    """Median W and H from the last 5 detection frames' AABBs for the given
    class ('vehicle' or 'person').  Returns (w, h) or None.  Each dimension
    is capped at `max_side`."""
    with processor._lock:
        history = processor._det_veh_sizes if cls == "vehicle" else processor._det_per_sizes
        all_sizes = [s for frame in history for s in frame]
    if len(all_sizes) < 2:
        return None
    ws = sorted(s[0] for s in all_sizes)
    hs = sorted(s[1] for s in all_sizes)
    return min(ws[len(ws) // 2], max_side), min(hs[len(hs) // 2], max_side)

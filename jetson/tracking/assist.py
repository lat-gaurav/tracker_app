"""Acquisition assist: refine a raw click bbox to the dominant corner cluster."""
import cv2
import numpy as np


def acq_assist_refine(frame, raw_bbox, margin, box_min, box_max):
    """Expand `raw_bbox` by `margin` (fraction of bbox), find Shi-Tomasi
    corners in the expanded region, drop outliers, and fit a tight bbox
    around the remaining corner cluster.  Returns refined (x, y, w, h) or
    None if not enough features were found (caller falls back to raw_bbox).
    """
    x, y, bw, bh = raw_bbox
    h_frame, w_frame = frame.shape[:2]

    # Expanded ROI
    ex = max(0, int(x - bw * margin))
    ey = max(0, int(y - bh * margin))
    ew = min(w_frame - ex, int(bw * (1 + 2 * margin)))
    eh = min(h_frame - ey, int(bh * (1 + 2 * margin)))
    if ew < 8 or eh < 8:
        return None

    region = frame[ey:ey + eh, ex:ex + ew]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)

    corners = cv2.goodFeaturesToTrack(
        gray, maxCorners=80, qualityLevel=0.01, minDistance=4
    )
    if corners is None or len(corners) < 4:
        print("[PROC]  acq_assist: too few corners — using raw bbox")
        return None

    # Convert from region-local to frame-absolute coords
    corners = corners.reshape(-1, 2) + np.array([ex, ey])

    # Drop outliers far from the click centre
    cx_click, cy_click = x + bw / 2.0, y + bh / 2.0
    dists = np.linalg.norm(corners - [cx_click, cy_click], axis=1)
    median = float(np.median(dists))
    kept = corners[dists < 2.0 * median]
    if len(kept) < 4:
        print("[PROC]  acq_assist: outlier filter left too few — using raw bbox")
        return None

    # Tight axis-aligned bbox around remaining corners
    xs, ys = kept[:, 0], kept[:, 1]
    nx = int(xs.min())
    ny = int(ys.min())
    nw = int(xs.max() - nx) + 1
    nh = int(ys.max() - ny) + 1

    # Enforce min/max size and stay inside the frame
    nw = max(box_min, min(box_max, nw))
    nh = max(box_min, min(box_max, nh))
    nx = max(0, min(nx, w_frame - nw))
    ny = max(0, min(ny, h_frame - nh))

    print(f"[PROC]  acq_assist: {len(kept)}/{len(corners)} corners  "
          f"raw=({x},{y},{bw},{bh}) -> refined=({nx},{ny},{nw},{nh})")
    return (nx, ny, nw, nh)

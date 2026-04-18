"""Side-effects triggered by config changes.

When `set_param` is called, certain paths require cross-field updates
(clamping, triggering tracker reinit, clearing cached YOLO, etc.).
This module centralises those rules so FrameProcessor.set_param() stays
short.
"""
from jetson.tracking import TRACKER_TYPES


def apply_side_effects(processor, dotted_path, cur):
    """Apply validation and cross-field updates after a config mutation.
    Returns the (possibly clamped or normalised) final value.
    Called with processor._lock already held.
    """
    mn = processor.cfg["tracker"]["box_min"]
    mx = processor.cfg["tracker"]["box_max"]

    # 1. Clamp tracker box defaults to [box_min, box_max]
    if dotted_path in ("tracker.box_w_default", "tracker.box_h_default"):
        clamped = max(mn, min(mx, int(cur)))
        if clamped != cur:
            print(f"[CONFIG] clamping {dotted_path} {cur} -> {clamped}")
            processor.cfg["tracker"][dotted_path.split(".")[-1]] = clamped
            cur = clamped

    # 2. Box default -> live runtime box + resize request
    if dotted_path == "tracker.box_w_default":
        processor._box_w = int(cur)
        if processor._bbox is not None:
            processor._pending_resize = True
    elif dotted_path == "tracker.box_h_default":
        processor._box_h = int(cur)
        if processor._bbox is not None:
            processor._pending_resize = True

    # 3. Tracker type change
    if dotted_path == "tracker.type":
        val = str(cur).lower()
        if val not in TRACKER_TYPES:
            raise ValueError(f"Unknown tracker type {val!r}. "
                             f"Choose from: {TRACKER_TYPES}")
        processor.cfg["tracker"]["type"] = val
        cur = val
        if processor._bbox is not None:
            processor._pending_resize = True
        print(f"[CONFIG] tracker type -> {val}"
              + (" [reinit pending]" if processor._bbox else ""))

    # 4. box_min/box_max -> re-clamp live values
    if dotted_path in ("tracker.box_min", "tracker.box_max"):
        new_w = max(mn, min(mx, processor._box_w))
        new_h = max(mn, min(mx, processor._box_h))
        if ((new_w, new_h) != (processor._box_w, processor._box_h)
                and processor._bbox is not None):
            processor._pending_resize = True
        processor._box_w, processor._box_h = new_w, new_h

    # 5. YOLO model path change -> drop cached model
    if dotted_path == "model.yolo_path":
        if processor._yolo_model is not None:
            print(f"[CONFIG] model path changed — dropping cached YOLO for reload")
        processor._yolo_model = None
        processor._det_names  = {}

    return cur

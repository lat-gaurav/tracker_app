"""YOLO model loader.  Falls back to models/<basename> if the configured
path doesn't exist (handles pre-reorg YAML paths)."""
import os
import time

from jetson.constants import MODELS_DIR


def load_yolo(yolo_path):
    """Load and return an Ultralytics YOLO model from `yolo_path`.
    If the path doesn't exist, look for the same filename in MODELS_DIR.
    Returns (model, names_dict).  Raises on unrecoverable load failure."""
    from ultralytics import YOLO

    path = yolo_path
    if not os.path.exists(path):
        alt = os.path.join(MODELS_DIR, os.path.basename(path))
        if os.path.exists(alt):
            print(f"[PROC]  model not found at {path!r}, using {alt!r}")
            path = alt

    print(f"[PROC]  loading YOLO model: {path}")
    t0 = time.monotonic()
    model = YOLO(path)
    dt = time.monotonic() - t0
    names = model.names if hasattr(model, "names") else {}
    print(f"[PROC]  YOLO loaded in {dt:.1f}s  classes={names}")
    return model, names

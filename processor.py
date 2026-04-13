import cv2
import numpy as np
import threading
import time


YOLO_MODEL_PATH = "yolov26nobbnew_merged_1024.pt"
DET_CONF_THRESH = 0.45
DET_TOP_N       = 5

# Class-name → category mapping (case-insensitive matching).
# Actual names are read from the model at load time; these hints are
# used to sort detections into "vehicle" vs "person" groups.
VEHICLE_NAMES = {"car", "truck", "bus", "van", "motor", "motorcycle", "bicycle", "vehicle"}
PERSON_NAMES  = {"person", "people", "pedestrian"}


def _create_tracker():
    if hasattr(cv2, "TrackerCSRT_create"):
        return cv2.TrackerCSRT_create()
    if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create"):
        return cv2.legacy.TrackerCSRT_create()
    raise RuntimeError("CSRT tracker not available — install opencv-contrib-python")


class FrameProcessor:
    """
    Runs the CSRT object tracker and YOLO OBB detector, each in its own thread.

    Capture thread calls submit_frame() + draw() at full FPS.
    Each consumer (tracker, detector) has its own frame slot and consumes the
    latest frame available when it's ready to start a new iteration.  Slow
    consumers just skip intermediate frames — they never block the capture.
    """

    def __init__(self):
        self._lock          = threading.Lock()

        # Tracker state
        self._tracker_frame = None
        self._pending_click = None
        self._box_w         = 20
        self._box_h         = 20
        self._bbox          = None
        self._track_ms      = 0.0
        self._track_count   = 0

        # Detector state
        self._detector_frame   = None
        self._detector_enabled = False
        self._detections       = []    # list of detection dicts
        self._det_ms           = 0.0
        self._det_count        = 0     # number of boxes in last successful run
        self._yolo_model       = None  # lazy-loaded
        self._det_names        = {}

        # Start worker threads
        self._tracker_thread  = threading.Thread(target=self._tracker_loop,  daemon=True)
        self._detector_thread = threading.Thread(target=self._detector_loop, daemon=True)
        self._tracker_thread.start()
        self._detector_thread.start()

        # Pre-load the YOLO engine in the background so toggling Detect ON
        # later is instant.  Runs in its own thread so it doesn't delay
        # stream.py startup.
        threading.Thread(target=self._preload_yolo, daemon=True).start()

    def _preload_yolo(self):
        try:
            self._load_yolo()
        except Exception as e:
            print(f"[PROC]  YOLO preload FAILED: {e}")

    # ------------------------------------------------------------------ capture thread API

    def submit_frame(self, frame: np.ndarray):
        """Store the current frame for tracker and detector threads.
        One shared copy since neither consumer mutates the frame."""
        copy = frame.copy()
        with self._lock:
            self._tracker_frame = copy
            if self._detector_enabled:
                self._detector_frame = copy

    def draw(self, frame: np.ndarray) -> np.ndarray:
        """Draw detections + tracker bbox on the frame. Called per frame (30fps)."""
        with self._lock:
            bbox       = self._bbox
            detections = self._detections

        # Draw detections underneath so the tracker bbox stays on top
        for det in detections:
            frame = self._draw_detection(frame, det)

        if bbox is not None:
            frame = self._draw_bbox(frame, bbox)
        return frame

    # ------------------------------------------------------------------ setters (WS thread)

    def set_click(self, ox: float, oy: float):
        with self._lock:
            self._pending_click = (ox, oy)
        print(f"[PROC]  set_click({ox:.3f}, {oy:.3f})")

    def set_box_size(self, w: int, h: int = None):
        """Set tracker init box. If h is None, uses w for both (square)."""
        if h is None:
            h = w
        w = max(10, min(400, w))
        h = max(10, min(400, h))
        with self._lock:
            self._box_w = w
            self._box_h = h
        print(f"[PROC]  set_box_size(w={w}, h={h})")

    def clear_track(self):
        with self._lock:
            self._pending_click = "clear"
            self._bbox = None
            self._track_ms = 0.0
        print("[PROC]  clear_track()")

    def enable_detector(self, on: bool):
        with self._lock:
            self._detector_enabled = bool(on)
            if not on:
                self._detections = []
                self._det_ms = 0.0
                self._detector_frame = None
        print(f"[PROC]  detector {'ENABLED' if on else 'disabled'}")

    # ------------------------------------------------------------------ telemetry

    def get_track_info(self):
        with self._lock:
            return self._bbox is not None, self._track_ms

    def get_det_info(self):
        with self._lock:
            return self._detector_enabled, self._det_ms, self._det_count

    # ------------------------------------------------------------------ tracker thread

    def _tracker_loop(self):
        tracker = None

        while True:
            with self._lock:
                frame = self._tracker_frame
                self._tracker_frame = None

            if frame is None:
                time.sleep(0.005)
                continue

            with self._lock:
                pending = self._pending_click
                self._pending_click = None
                bw = self._box_w
                bh = self._box_h

            h, w = frame.shape[:2]

            if pending == "clear":
                tracker = None
                continue

            if pending is not None:
                ox, oy = pending
                cx = int(ox * w)
                cy = int(oy * h)
                x = max(0, min(cx - bw // 2, w - bw))
                y = max(0, min(cy - bh // 2, h - bh))
                bbox = (x, y, bw, bh)
                tracker = _create_tracker()
                tracker.init(frame, bbox)
                with self._lock:
                    self._bbox = bbox
                print(f"[PROC]  tracker init at ({cx},{cy}) bbox={bbox} on {w}x{h}")
                continue

            if tracker is not None:
                t0 = time.monotonic()
                success, bbox = tracker.update(frame)
                dt_ms = (time.monotonic() - t0) * 1000

                if success:
                    self._track_count += 1
                    with self._lock:
                        # Re-check — user may have pressed Stop Track while
                        # this update was running.  Discard the stale result.
                        if self._pending_click == "clear":
                            continue
                        self._bbox = tuple(int(v) for v in bbox)
                        self._track_ms = dt_ms
                    if self._track_count == 1 or self._track_count % 30 == 0:
                        print(f"[PROC]  track update #{self._track_count}  {dt_ms:.1f}ms  bbox={self._bbox}")
                else:
                    print(f"[PROC]  tracker LOST ({dt_ms:.1f}ms)")
                    tracker = None
                    with self._lock:
                        self._bbox = None
                        self._track_ms = 0.0

    # ------------------------------------------------------------------ detector thread

    def _load_yolo(self):
        if self._yolo_model is not None:
            return   # already loaded (preload finished, don't reload)
        from ultralytics import YOLO
        print(f"[PROC]  loading YOLO model: {YOLO_MODEL_PATH}")
        t0 = time.monotonic()
        model = YOLO(YOLO_MODEL_PATH)
        dt = time.monotonic() - t0
        self._yolo_model = model
        self._det_names  = model.names if hasattr(model, 'names') else {}
        print(f"[PROC]  YOLO loaded in {dt:.1f}s  classes={self._det_names}")

    def _detector_loop(self):
        det_count = 0

        while True:
            with self._lock:
                frame   = self._detector_frame
                enabled = self._detector_enabled
                self._detector_frame = None

            if not enabled or frame is None:
                time.sleep(0.01)
                continue

            if self._yolo_model is None:
                try:
                    self._load_yolo()
                except Exception as e:
                    print(f"[PROC]  YOLO load FAILED: {e}")
                    with self._lock:
                        self._detector_enabled = False
                    continue

            # Run inference on GPU (ultralytics auto-uses CUDA if available)
            t0 = time.monotonic()
            try:
                results = self._yolo_model.predict(
                    frame, conf=DET_CONF_THRESH, verbose=False
                )
            except Exception as e:
                print(f"[PROC]  YOLO predict FAILED: {e}")
                time.sleep(0.1)
                continue
            dt_ms = (time.monotonic() - t0) * 1000

            detections = self._parse_detections(results)

            det_count += 1
            with self._lock:
                # Re-check enabled — user may have toggled off mid-inference.
                # If so, discard this result so no stale boxes linger on screen.
                if not self._detector_enabled:
                    continue
                self._detections = detections
                self._det_ms     = dt_ms
                self._det_count  = len(detections)

            if det_count == 1 or det_count % 15 == 0:
                print(f"[PROC]  det #{det_count}  {dt_ms:.1f}ms  {len(detections)} objects")

    def _parse_detections(self, results):
        """Extract OBB polys, keep top DET_TOP_N vehicles + top DET_TOP_N persons."""
        vehicles = []
        persons  = []
        for result in results:
            if result.obb is None or len(result.obb) == 0:
                continue
            polys   = result.obb.xyxyxyxy.cpu().numpy()  # (N, 4, 2)
            confs   = result.obb.conf.cpu().numpy()
            classes = result.obb.cls.cpu().numpy().astype(int)
            names   = result.names
            for poly, conf, cls in zip(polys, confs, classes):
                label = str(names[cls]).lower()
                det = {"poly": poly, "conf": float(conf), "label": names[cls]}
                if any(v in label for v in VEHICLE_NAMES):
                    vehicles.append(det)
                elif any(p in label for p in PERSON_NAMES):
                    persons.append(det)
        vehicles.sort(key=lambda d: d["conf"], reverse=True)
        persons.sort(key=lambda d: d["conf"], reverse=True)
        return vehicles[:DET_TOP_N] + persons[:DET_TOP_N]

    # ------------------------------------------------------------------ drawing

    def _draw_bbox(self, frame: np.ndarray, bbox: tuple) -> np.ndarray:
        x, y, bw, bh = bbox
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        cx = x + bw // 2
        cy = y + bh // 2
        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)
        return frame

    def _draw_detection(self, frame: np.ndarray, det: dict) -> np.ndarray:
        label_lc = str(det["label"]).lower()
        if any(v in label_lc for v in VEHICLE_NAMES):
            color = (0, 0, 255)     # BGR red — vehicles
        else:
            color = (255, 0, 0)     # BGR blue — persons
        poly = det["poly"].astype(np.int32)
        cv2.polylines(frame, [poly], isClosed=True, color=color, thickness=2)
        return frame

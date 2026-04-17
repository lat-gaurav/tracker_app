import cv2
import os
import yaml
import numpy as np
import threading
import time
from collections import OrderedDict


CONFIG_PATH = "config/default.yaml"

MODELS_DIR = "models"

DEFAULT_CONFIG = {
    "model": {
        "yolo_path": "models/yolov26nobbnew_merged_1024.engine",
    },
    "tracker": {
        "type": "csrt-fast",    # "csrt", "csrt-fast", "csrt-faster", "csrt-ultra", "kcf", or "mosse"
        "box_w_default": 20,
        "box_h_default": 20,
        "box_min": 10,
        "box_max": 400,
        "acq_assist": {
            "enabled": False,
            "margin": 0.30,
        },
        "ai_assist": {
            "enabled": False,
            "interval": 30,
            "iou_min": 0.10,
            "conf_min": 0.25,
        },
        "ai_acquisition": {
            "enabled": False,
            "near_val": 150,
        },
        "ai_box_size": {
            "enabled": False,
            "class": "vehicle",   # "vehicle" or "person"
        },
    },
    "camera": {
        "width": 0,     # 0 = auto-negotiate
        "height": 0,
        "fps": 0,
    },
    "detection": {
        "enabled_on_start": False,
        "conf_thresh": 0.45,
        "top_n": 5,
        "vehicle_names": ["car", "truck", "bus", "van", "motor", "motorcycle", "bicycle", "vehicle"],
        "person_names":  ["person", "people", "pedestrian"],
    },
    "jump_detector": {
        "enabled": True,
        "dist_thresh": 0.35,
        "size_thresh": 1.5,
        "iou_thresh":  0.35,
    },
    "kalman": {
        "enabled": True,
        "process_noise": 0.01,
        "measure_noise": 0.1,
    },
}


# ------------------------------------------------------------------ config helpers

def _deep_update(base, overrides):
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def _deep_copy(d):
    if isinstance(d, dict):
        return {k: _deep_copy(v) for k, v in d.items()}
    if isinstance(d, list):
        return list(d)
    return d


def load_config():
    config = _deep_copy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                user = yaml.safe_load(f) or {}
            _deep_update(config, user)
            print(f"[CONFIG] Loaded {CONFIG_PATH}")
        except Exception as e:
            print(f"[CONFIG] Failed to load {CONFIG_PATH}: {e}")
    else:
        print(f"[CONFIG] {CONFIG_PATH} not found, using built-in defaults")
    return config


def list_models():
    """Return sorted list of model file paths in MODELS_DIR."""
    if not os.path.isdir(MODELS_DIR):
        return []
    out = []
    for f in sorted(os.listdir(MODELS_DIR)):
        if f.lower().endswith((".pt", ".engine", ".onnx")):
            out.append(os.path.join(MODELS_DIR, f))
    return out


def save_config(config, path=CONFIG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=None)
    print(f"[CONFIG] Saved to {path}")


def _set_nested(d, dotted_path, value):
    """Set d[a][b][c] = value from 'a.b.c'. Coerces value to existing type."""
    keys = dotted_path.split(".")
    for k in keys[:-1]:
        d = d[k]
    last = keys[-1]
    existing = d.get(last)
    try:
        if isinstance(existing, bool):
            d[last] = str(value).lower() in ("1", "true", "yes", "on")
        elif isinstance(existing, int) and not isinstance(existing, bool):
            d[last] = int(float(value))
        elif isinstance(existing, float):
            d[last] = float(value)
        elif isinstance(existing, list):
            # comma-separated
            d[last] = [s.strip() for s in str(value).split(",") if s.strip()]
        else:
            d[last] = value
    except (ValueError, TypeError) as e:
        raise ValueError(f"Bad value for {dotted_path}: {value!r} ({e})")


# ------------------------------------------------------------------ tracker factory

# CSRT speed presets — tuned parameters that trade features for speed.
def _csrt_params(preset="default"):
    p = cv2.TrackerCSRT_Params()
    if preset == "fast":
        p.template_size = 100
        p.use_color_names = False
        p.use_segmentation = False
        p.number_of_scales = 17
        p.admm_iterations = 2
    elif preset == "faster":
        p.template_size = 80
        p.use_color_names = False
        p.use_gray = False
        p.use_segmentation = False
        p.number_of_scales = 9
        p.admm_iterations = 2
        p.num_hog_channels_used = 9
    elif preset == "ultra":
        p.template_size = 50
        p.use_color_names = False
        p.use_gray = False
        p.use_segmentation = False
        p.use_channel_weights = False
        p.number_of_scales = 5
        p.admm_iterations = 1
        p.num_hog_channels_used = 4
    return p

TRACKER_TYPES = [
    "csrt", "csrt-fast", "csrt-faster", "csrt-ultra", "kcf", "mosse",
]

def _create_tracker(tracker_type="csrt"):
    tracker_type = tracker_type.lower()
    if tracker_type.startswith("csrt"):
        preset = tracker_type.split("-", 1)[1] if "-" in tracker_type else "default"
        params = _csrt_params(preset)
        if hasattr(cv2, "TrackerCSRT_create"):
            return cv2.TrackerCSRT_create(params)
        if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create"):
            return cv2.legacy.TrackerCSRT_create(params)
        raise RuntimeError("CSRT not available")
    if tracker_type == "kcf":
        if hasattr(cv2, "TrackerKCF_create"):
            return cv2.TrackerKCF_create()
        if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerKCF_create"):
            return cv2.legacy.TrackerKCF_create()
        raise RuntimeError("KCF not available")
    if tracker_type == "mosse":
        if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerMOSSE_create"):
            return cv2.legacy.TrackerMOSSE_create()
        raise RuntimeError("MOSSE not available")
    raise ValueError(f"Unknown tracker type {tracker_type!r}. Choose from: {TRACKER_TYPES}")


# ------------------------------------------------------------------ SORT Kalman

class SORTKalman:
    """
    7-state constant-velocity Kalman filter.
    State: [cx, cy, s, r, vcx, vcy, vs]
        cx, cy = box centre
        s      = area (w*h)
        r      = aspect ratio (w/h)  — assumed constant (no velocity)
        v*     = velocities
    """

    def __init__(self, process_noise=0.01, measure_noise=0.1):
        self.q = process_noise
        self.r = measure_noise
        self.initialized = False
        self.F = np.eye(7)
        self.F[0, 4] = 1.0
        self.F[1, 5] = 1.0
        self.F[2, 6] = 1.0
        self.H = np.zeros((4, 7))
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0
        self.H[2, 2] = 1.0
        self.H[3, 3] = 1.0

    def init(self, bbox):
        x, y, w, h = bbox
        cx = x + w / 2.0
        cy = y + h / 2.0
        s  = max(w * h, 1.0)
        r  = (w / h) if h > 0 else 1.0
        self.x = np.array([cx, cy, s, r, 0, 0, 0], dtype=np.float64)
        self.Q = np.eye(7) * self.q
        self.Q[4:, 4:] *= 10.0            # more noise on velocities
        self.R = np.eye(4) * self.r
        self.P = np.eye(7) * 10.0
        self.initialized = True

    def predict(self, steps=1):
        if not self.initialized:
            return None
        steps = max(1, int(steps))
        if steps == 1:
            self.x = self.F @ self.x
            self.P = self.F @ self.P @ self.F.T + self.Q
        else:
            F_n = np.linalg.matrix_power(self.F, steps)
            self.x = F_n @ self.x
            Q_acc = np.zeros_like(self.Q)
            F_k = np.eye(7)
            for _ in range(steps):
                Q_acc += F_k @ self.Q @ F_k.T
                F_k = F_k @ self.F
            self.P = F_n @ self.P @ F_n.T + Q_acc
        return self._state_to_bbox(self.x)

    def update(self, bbox):
        if not self.initialized:
            self.init(bbox)
            return
        x, y, w, h = bbox
        z = np.array([x + w / 2.0, y + h / 2.0, max(w * h, 1.0),
                      (w / h) if h > 0 else 1.0])
        y_resid = z - (self.H @ self.x)
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y_resid
        self.P = (np.eye(7) - K @ self.H) @ self.P

    @staticmethod
    def _state_to_bbox(state):
        cx, cy, s, r = float(state[0]), float(state[1]), float(state[2]), float(state[3])
        if s <= 0: s = 1.0
        if r <= 0: r = 1.0
        w = float(np.sqrt(max(s * r, 1.0)))
        h = s / w if w > 0 else 1.0
        return (cx - w / 2.0, cy - h / 2.0, w, h)


# ------------------------------------------------------------------ Jump detector

class JumpDetector:
    def __init__(self, config):
        self.cfg    = config
        self.kalman = None

    def reset(self):
        self.kalman = None

    def init_tracker(self, bbox):
        if self.cfg["kalman"]["enabled"]:
            self.kalman = SORTKalman(
                self.cfg["kalman"]["process_noise"],
                self.cfg["kalman"]["measure_noise"],
            )
            self.kalman.init(bbox)
        else:
            self.kalman = None

    def check(self, bbox, frames_skipped=1):
        """Returns (is_jump, metrics_dict_or_None).  Updates Kalman only if not a jump."""
        if self.kalman is None or not self.cfg["jump_detector"]["enabled"]:
            if self.kalman:
                self.kalman.update(bbox)
            return False, None
        pred = self.kalman.predict(steps=frames_skipped)
        if pred is None:
            self.kalman.update(bbox)
            return False, None
        pcx, pcy = pred[0] + pred[2] / 2.0, pred[1] + pred[3] / 2.0
        ncx, ncy = bbox[0] + bbox[2] / 2.0, bbox[1] + bbox[3] / 2.0
        dist = float(np.hypot(pcx - ncx, pcy - ncy))
        diag = max(float(np.hypot(pred[2], pred[3])), 1.0)
        dist_ratio = dist / diag
        pa = max(pred[2] * pred[3], 1.0)
        na = max(bbox[2] * bbox[3], 1.0)
        size_ratio = max(na / pa, pa / na)
        iou = self._iou(pred, bbox)
        dt = self.cfg["jump_detector"]["dist_thresh"]
        sz = self.cfg["jump_detector"]["size_thresh"]
        io = self.cfg["jump_detector"]["iou_thresh"]
        is_jump = (dist_ratio > dt) and (size_ratio > sz or iou < io)
        if not is_jump:
            self.kalman.update(bbox)
        return is_jump, {
            "dist_ratio": dist_ratio, "size_ratio": size_ratio, "iou": iou,
        }

    @staticmethod
    def _iou(a, b):
        ax1, ay1, aw, ah = a
        ax2, ay2 = ax1 + aw, ay1 + ah
        bx1, by1, bw, bh = b
        bx2, by2 = bx1 + bw, by1 + bh
        iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        ih = max(0.0, min(ay2, by2) - max(ay1, by1))
        inter = iw * ih
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0


# ------------------------------------------------------------------ FrameProcessor

class FrameProcessor:
    """
    CSRT tracker + YOLO OBB detector, each in its own thread.
    Capture calls submit_frame() + draw() at full FPS.  Each consumer picks up
    the latest frame when ready.  Slow consumers just skip intermediate frames.
    """

    FRAME_RING_SIZE = 300
    MAX_DET_AGE = 4
    _CATCHUP_DISPLAY_INTERVAL = 5

    def __init__(self):
        self._lock = threading.Lock()

        # Config (live-updatable)
        self.cfg = load_config()

        # ---- Frame sequencing ----
        self._frame_seq = 0
        self._frame_ring = OrderedDict()
        self._frame_ts = {}

        # Tracker state
        self._tracker_frame = None
        self._tracker_frame_seq = 0
        self._pending_click = None
        self._pending_click_seq = 0
        self._pending_drag_rect = None
        self._box_w         = self.cfg["tracker"]["box_w_default"]
        self._box_h         = self.cfg["tracker"]["box_h_default"]
        self._bbox          = None
        self._bbox_seq      = 0
        self._track_ms      = 0.0
        self._track_count   = 0
        self._jump          = JumpDetector(self.cfg)
        self._last_lost_reason = ""
        self._ai_assist_until_count = 0   # show triangle while track_count < this
        self._pending_resize = False      # resize active tracker to new _box_w/_box_h

        # Pause-and-select state
        self._paused = False
        self._paused_seq = 0
        self._catching_up = False
        self._paused_frame = None
        self._paused_bbox = None
        self._paused_detections = []
        self._paused_ai_active = False
        self._catchup_frame = None

        # Detector state
        self._detector_frame   = None
        self._detector_frame_seq = 0
        self._detector_enabled = bool(self.cfg.get("detection", {}).get("enabled_on_start", False))
        self._detections       = []   # shown on screen (>= detection.conf_thresh)
        self._detections_assist = []  # wider set for AI-assist matching only
        self._det_seq          = 0
        self._det_ms           = 0.0
        self._det_count        = 0
        self._yolo_model       = None
        self._det_names        = {}
        self._det_error        = ""   # last error message (surfaces to ground)

        # AI box size estimation — rolling window of AABB sizes per class
        from collections import deque as _deque
        self._det_veh_sizes = _deque(maxlen=5)   # last 5 frames of vehicle (w,h)
        self._det_per_sizes = _deque(maxlen=5)   # last 5 frames of person  (w,h)
        self._auto_box_notify = None              # (w, h) to push to ground

        # Worker threads
        threading.Thread(target=self._tracker_loop,  daemon=True).start()
        threading.Thread(target=self._detector_loop, daemon=True).start()
        threading.Thread(target=self._preload_yolo,  daemon=True).start()

    # ---------------- capture thread API ----------------

    def submit_frame(self, frame: np.ndarray):
        copy = frame.copy()
        now = time.monotonic()
        with self._lock:
            self._frame_seq += 1
            seq = self._frame_seq
            self._frame_ring[seq] = copy
            self._frame_ts[seq] = now
            while len(self._frame_ring) > self.FRAME_RING_SIZE:
                old_seq, _ = self._frame_ring.popitem(last=False)
                self._frame_ts.pop(old_seq, None)
            self._tracker_frame = copy
            self._tracker_frame_seq = seq
            if self._detector_enabled and not self._paused:
                self._detector_frame = copy
                self._detector_frame_seq = seq

    def draw(self, frame: np.ndarray, paused_view: bool = False) -> np.ndarray:
        with self._lock:
            if paused_view and self._paused:
                bbox       = self._paused_bbox
                detections = self._paused_detections
                ai_active  = self._paused_ai_active
            else:
                bbox       = self._bbox
                detections = self._detections
                ai_active  = self._track_count < self._ai_assist_until_count
        for det in detections:
            frame = self._draw_detection(frame, det)
        if bbox is not None:
            frame = self._draw_bbox(frame, bbox, ai_active=ai_active)
        return frame

    # ---------------- setters (WS thread) ----------------

    def set_click(self, ox: float, oy: float):
        with self._lock:
            self._pending_click = (ox, oy)
            if self._paused and self._paused_seq > 0:
                self._pending_click_seq = self._paused_seq
            else:
                self._pending_click_seq = self._frame_seq
        print(f"[PROC]  set_click({ox:.3f}, {oy:.3f}) "
              f"click_seq={self._pending_click_seq} "
              f"{'(paused)' if self._paused else '(live)'}")

    def set_drag(self, nx: float, ny: float, nw: float, nh: float):
        ocx = nx + nw / 2.0
        ocy = ny + nh / 2.0
        with self._lock:
            self._pending_click = (ocx, ocy)
            self._pending_drag_rect = (nx, ny, nw, nh)
            if self._paused and self._paused_seq > 0:
                self._pending_click_seq = self._paused_seq
            else:
                self._pending_click_seq = self._frame_seq
        print(f"[PROC]  set_drag({nx:.3f},{ny:.3f},{nw:.3f},{nh:.3f}) "
              f"click_seq={self._pending_click_seq}")

    def set_box_size(self, w: int, h: int = None):
        if h is None:
            h = w
        with self._lock:
            mn = int(self.cfg["tracker"]["box_min"])
            mx = int(self.cfg["tracker"]["box_max"])
            new_w = max(mn, min(mx, int(w)))
            new_h = max(mn, min(mx, int(h)))
            changed = (self._box_w != new_w or self._box_h != new_h)
            self._box_w = new_w
            self._box_h = new_h
            if changed and self._bbox is not None:
                self._pending_resize = True
        print(f"[PROC]  set_box_size(w={self._box_w}, h={self._box_h})"
              + ("  [resize pending]" if changed else ""))

    def clear_track(self):
        with self._lock:
            self._pending_click = "clear"
            self._bbox = None
            self._bbox_seq = 0
            self._track_ms = 0.0
        print("[PROC]  clear_track()")

    def pause(self):
        with self._lock:
            self._paused = True
            self._paused_seq = self._frame_seq
            self._paused_frame = self._frame_ring.get(self._frame_seq)
            self._paused_bbox = self._bbox
            self._paused_detections = list(self._detections)
            self._paused_ai_active = self._track_count < self._ai_assist_until_count
        print(f"[PROC]  PAUSED at seq={self._paused_seq}")

    def resume(self):
        with self._lock:
            self._paused = False
            self._paused_seq = 0
            self._paused_frame = None
            self._catching_up = False
        print("[PROC]  RESUMED")

    def is_paused(self):
        with self._lock:
            return self._paused, self._paused_seq, self._catching_up

    def get_display_state(self):
        with self._lock:
            return (self._paused, self._catching_up, self._paused_frame, self._catchup_frame)

    def get_paused_frame(self):
        with self._lock:
            return self._paused_frame

    def get_catchup_frame(self):
        with self._lock:
            return self._catchup_frame

    def enable_detector(self, on: bool):
        with self._lock:
            self._detector_enabled = bool(on)
            if not on:
                self._detections = []
                self._detections_assist = []
                self._det_seq = 0
                self._det_ms = 0.0
                self._detector_frame = None
        print(f"[PROC]  detector {'ENABLED' if on else 'disabled'}")

    # ---------------- config API ----------------

    def get_config(self):
        with self._lock:
            cfg = _deep_copy(self.cfg)
            cfg["_runtime"] = {
                "detection_on": self._detector_enabled,
            }
            return cfg

    def set_param(self, dotted_path: str, value):
        with self._lock:
            _set_nested(self.cfg, dotted_path, value)
            # Resolve the new value
            cur = self.cfg
            for k in dotted_path.split("."):
                cur = cur[k]

            # --- Validation / cross-effect updates ---
            mn = self.cfg["tracker"]["box_min"]
            mx = self.cfg["tracker"]["box_max"]

            # 1. Clamp tracker box defaults to [box_min, box_max] so an
            #    out-of-range default can never be persisted.
            if dotted_path in ("tracker.box_w_default", "tracker.box_h_default"):
                clamped = max(mn, min(mx, int(cur)))
                if clamped != cur:
                    print(f"[CONFIG] clamping {dotted_path} {cur} -> {clamped}")
                    self.cfg["tracker"][dotted_path.split(".")[-1]] = clamped
                    cur = clamped

            # 2. Updating a default ALSO updates the live runtime value so
            #    the very next click uses the new size.  If a tracker is
            #    currently active, request a live resize so the box on screen
            #    shrinks/grows to the new dimensions immediately.
            if dotted_path == "tracker.box_w_default":
                self._box_w = int(cur)
                if self._bbox is not None:
                    self._pending_resize = True
            elif dotted_path == "tracker.box_h_default":
                self._box_h = int(cur)
                if self._bbox is not None:
                    self._pending_resize = True

            # 3. If box_min/box_max change, re-clamp current _box_w/_box_h and
            #    propagate any clamp to the active tracker too.
            if dotted_path == "tracker.type":
                val = str(cur).lower()
                if val not in TRACKER_TYPES:
                    raise ValueError(f"Unknown tracker type {val!r}. Choose from: {TRACKER_TYPES}")
                self.cfg["tracker"]["type"] = val
                cur = val
                if self._bbox is not None:
                    self._pending_resize = True
                print(f"[CONFIG] tracker type -> {val}" + (" [reinit pending]" if self._bbox else ""))

            if dotted_path in ("tracker.box_min", "tracker.box_max"):
                new_w = max(mn, min(mx, self._box_w))
                new_h = max(mn, min(mx, self._box_h))
                if (new_w, new_h) != (self._box_w, self._box_h) and self._bbox is not None:
                    self._pending_resize = True
                self._box_w, self._box_h = new_w, new_h

            # 4. Live model reload — drop the cached YOLO so the detector
            #    thread lazy-loads the new path on its next iteration.
            if dotted_path == "model.yolo_path":
                if self._yolo_model is not None:
                    print(f"[CONFIG] model path changed — dropping cached YOLO for reload")
                self._yolo_model = None
                self._det_names  = {}

        print(f"[CONFIG] {dotted_path} = {cur!r}")
        return cur

    def save_default(self):
        with self._lock:
            cfg_copy = _deep_copy(self.cfg)
        save_config(cfg_copy)

    # ---------------- telemetry ----------------

    def get_track_info(self):
        with self._lock:
            return self._bbox is not None, self._track_ms

    def get_det_info(self):
        with self._lock:
            return (self._detector_enabled, self._det_ms, self._det_count,
                    self._det_error)

    def get_last_lost_reason(self):
        with self._lock:
            r = self._last_lost_reason
            self._last_lost_reason = ""
            return r

    # ---------------- catch-up replay ----------------

    def _catch_up_to_live(self, tracker, from_seq):
        with self._lock:
            self._catching_up = True
            live_seq = self._frame_seq
            replay_frames = [(s, self._frame_ring[s])
                             for s in sorted(self._frame_ring)
                             if from_seq < s <= live_seq]

        total = len(replay_frames)
        print(f"[CATCHUP] start: {total} frames  from_seq={from_seq} live_seq={live_seq}")

        processed = 0
        last_seq = from_seq

        for seq, frame in replay_frames:
            if processed % 8 == 0:
                with self._lock:
                    if self._pending_click is not None:
                        print(f"[CATCHUP] ABORTED at seq={seq}")
                        self._catching_up = False
                        self._catchup_frame = None
                        return tracker, True

            success, bbox = tracker.update(frame)

            if not success:
                print(f"[CATCHUP] LOST at seq={seq} ({processed}/{total})")
                self._jump.reset()
                with self._lock:
                    self._bbox = None
                    self._bbox_seq = 0
                    self._track_ms = 0.0
                    self._last_lost_reason = "lost during catch-up"
                    self._catching_up = False
                    self._catchup_frame = None
                return None, False

            frames_skipped = seq - last_seq
            is_jump, metrics = self._jump.check(bbox, frames_skipped)
            if is_jump:
                reason = f"jump during catch-up seq={seq} dist={metrics['dist_ratio']:.2f}"
                print(f"[CATCHUP] JUMP — {reason}")
                self._jump.reset()
                with self._lock:
                    self._bbox = None
                    self._bbox_seq = 0
                    self._track_ms = 0.0
                    self._last_lost_reason = reason
                    self._catching_up = False
                    self._catchup_frame = None
                return None, False

            processed += 1
            last_seq = seq

            if processed % self._CATCHUP_DISPLAY_INTERVAL == 0 or processed == total:
                with self._lock:
                    self._bbox = tuple(int(v) for v in bbox)
                    self._bbox_seq = seq
                    self._catchup_frame = frame
            else:
                with self._lock:
                    self._bbox = tuple(int(v) for v in bbox)
                    self._bbox_seq = seq

        with self._lock:
            self._catching_up = False
            self._catchup_frame = None
            cur_live = self._frame_seq
        print(f"[CATCHUP] DONE: {processed}/{total} frames  "
              f"seq {from_seq}->{live_seq}  gap_to_live={cur_live - last_seq}")
        return tracker, True

    # ---------------- tracker thread ----------------

    def _tracker_loop(self):
        tracker = None
        last_track_time = None
        last_track_seq = 0

        while True:
            with self._lock:
                frame = self._tracker_frame
                frame_seq = self._tracker_frame_seq
                self._tracker_frame = None

            if frame is None:
                time.sleep(0.005)
                continue

            now = time.monotonic()

            with self._lock:
                pending = self._pending_click
                click_seq = self._pending_click_seq
                drag_rect = self._pending_drag_rect
                self._pending_click = None
                self._pending_drag_rect = None
                bw = self._box_w
                bh = self._box_h
                resize_now = self._pending_resize
                self._pending_resize = False

            h, w = frame.shape[:2]

            if pending == "clear":
                tracker = None
                self._jump.reset()
                last_track_time = None
                last_track_seq = 0
                continue

            if resize_now and tracker is not None and self._bbox is not None:
                bx, by, bw_old, bh_old = self._bbox
                cx_old = bx + bw_old / 2.0
                cy_old = by + bh_old / 2.0
                nx = max(0, min(int(cx_old - bw / 2), w - bw))
                ny = max(0, min(int(cy_old - bh / 2), h - bh))
                new_bbox = (nx, ny, bw, bh)
                ttype = self.cfg["tracker"]["type"]
                tracker = _create_tracker(ttype)
                tracker.init(frame, new_bbox)
                self._jump.init_tracker(new_bbox)
                with self._lock:
                    self._bbox = new_bbox
                    self._bbox_seq = frame_seq
                last_track_time = now
                last_track_seq = frame_seq
                print(f"[PROC]  live resize -> {new_bbox}")

            if pending is not None and pending != "clear":
                ox, oy = pending

                # Click-to-frame lookup
                init_frame = frame
                init_seq = frame_seq
                with self._lock:
                    if click_seq in self._frame_ring:
                        init_frame = self._frame_ring[click_seq]
                        init_seq = click_seq
                    elif self._frame_ring:
                        oldest_seq = next(iter(self._frame_ring))
                        if oldest_seq > click_seq:
                            init_frame = self._frame_ring[oldest_seq]
                            init_seq = oldest_seq

                ih, iw = init_frame.shape[:2]

                if drag_rect is not None:
                    dnx, dny, dnw, dnh = drag_rect
                    dx = max(0, int(dnx * iw))
                    dy = max(0, int(dny * ih))
                    dw = max(1, min(int(dnw * iw), iw - dx))
                    dh = max(1, min(int(dnh * ih), ih - dy))
                    bbox = (dx, dy, dw, dh)
                    init_path = "drag"
                    cx = dx + dw // 2
                    cy = dy + dh // 2
                else:
                    cx = int(ox * iw)
                    cy = int(oy * ih)
                    bbox = None
                    init_path = "raw"

                    ai_bs = self.cfg["tracker"].get("ai_box_size", {})
                    if ai_bs.get("enabled", False):
                        cls = str(ai_bs.get("class", "vehicle")).lower()
                        auto = self._get_auto_box_size(cls)
                        if auto is not None:
                            bw, bh = auto
                            with self._lock:
                                self._box_w = bw
                                self._box_h = bh
                                self.cfg["tracker"]["box_w_default"] = bw
                                self.cfg["tracker"]["box_h_default"] = bh
                                self._auto_box_notify = (bw, bh)

                    ai_acq = self.cfg["tracker"].get("ai_acquisition", {})
                    if ai_acq.get("enabled", False):
                        snapped = self._ai_acquisition_snap(
                            cx, cy, float(ai_acq.get("near_val", 150)), init_seq)
                        if snapped is not None:
                            bbox = snapped
                            init_path = "ai_acquisition"

                    if bbox is None:
                        x = max(0, min(cx - bw // 2, iw - bw))
                        y = max(0, min(cy - bh // 2, ih - bh))
                        raw_bbox = (x, y, bw, bh)
                        acq = self.cfg["tracker"].get("acq_assist", {})
                        if acq.get("enabled", False):
                            margin = float(acq.get("margin", 0.30))
                            refined = self._acq_assist_refine(init_frame, raw_bbox, margin)
                            if refined is not None:
                                bbox = refined
                                init_path = "acq_assist"
                            else:
                                bbox = raw_bbox
                        else:
                            bbox = raw_bbox

                ttype = self.cfg["tracker"]["type"]
                tracker = _create_tracker(ttype)
                tracker.init(init_frame, bbox)
                self._jump.init_tracker(bbox)
                last_track_time = now
                last_track_seq = init_seq
                with self._lock:
                    self._bbox = bbox
                    self._bbox_seq = init_seq
                print(f"[PROC]  tracker init [{init_path}]  click=({cx},{cy})  "
                      f"bbox={bbox}  click_seq={click_seq} init_seq={init_seq}")

                # Iterative catch-up
                catchup_from = init_seq
                for _pass in range(5):
                    with self._lock:
                        gap = self._frame_seq - catchup_from
                    if gap <= 2:
                        break
                    tracker, ok = self._catch_up_to_live(tracker, catchup_from)
                    if not ok or tracker is None:
                        tracker = None
                        last_track_time = None
                        last_track_seq = 0
                        break
                    else:
                        last_track_time = time.monotonic()
                        with self._lock:
                            last_track_seq = self._bbox_seq
                            catchup_from = self._bbox_seq

                with self._lock:
                    if self._paused:
                        self._paused = False
                        self._paused_seq = 0
                        self._paused_frame = None
                        print("[PROC]  auto-RESUMED after catch-up")
                continue

            if tracker is not None:
                t0 = time.monotonic()
                success, bbox = tracker.update(frame)
                dt_ms = (time.monotonic() - t0) * 1000

                if last_track_time is not None:
                    frames_skipped = frame_seq - last_track_seq
                else:
                    frames_skipped = 1

                if not success:
                    print(f"[PROC]  tracker LOST ({dt_ms:.1f}ms) seq={frame_seq}")
                    tracker = None
                    self._jump.reset()
                    last_track_time = None
                    last_track_seq = 0
                    with self._lock:
                        self._bbox = None
                        self._bbox_seq = 0
                        self._track_ms = 0.0
                        self._last_lost_reason = f"csrt update failed (skipped={frames_skipped})"
                    continue

                is_jump, metrics = self._jump.check(bbox, frames_skipped)
                if is_jump:
                    reason = (f"jump dist={metrics['dist_ratio']:.2f} "
                              f"size={metrics['size_ratio']:.2f} "
                              f"iou={metrics['iou']:.2f} "
                              f"skipped={frames_skipped}")
                    print(f"[PROC]  tracker JUMP — dropping ({reason})")
                    tracker = None
                    self._jump.reset()
                    last_track_time = None
                    last_track_seq = 0
                    with self._lock:
                        self._bbox = None
                        self._bbox_seq = 0
                        self._track_ms = 0.0
                        self._last_lost_reason = reason
                    continue

                last_track_time = now
                last_track_seq = frame_seq

                self._track_count += 1
                with self._lock:
                    if self._pending_click == "clear":
                        continue
                    self._bbox = tuple(int(v) for v in bbox)
                    self._bbox_seq = frame_seq
                    self._track_ms = dt_ms

                ai_cfg = self.cfg["tracker"].get("ai_assist", {})
                if (ai_cfg.get("enabled", False)
                        and self._track_count %
                            max(1, int(ai_cfg.get("interval", 30))) == 0):
                    snapped = self._ai_assist_snap(
                        frame, self._bbox,
                        float(ai_cfg.get("iou_min", 0.10)),
                        frame_seq)
                    if snapped is not None:
                        new_bbox, iou = snapped
                        ttype = self.cfg["tracker"]["type"]
                        tracker = _create_tracker(ttype)
                        tracker.init(frame, new_bbox)
                        self._jump.init_tracker(new_bbox)
                        with self._lock:
                            self._bbox = new_bbox
                            self._bbox_seq = frame_seq
                            self._ai_assist_until_count = self._track_count + 15

                if self._track_count == 1 or self._track_count % 30 == 0:
                    print(f"[PROC]  track #{self._track_count}  {dt_ms:.1f}ms  "
                          f"bbox={self._bbox}  seq={frame_seq}")

    # ---------------- detector thread ----------------

    def _preload_yolo(self):
        try:
            self._load_yolo()
        except Exception as e:
            print(f"[PROC]  YOLO preload FAILED: {e}")

    def _load_yolo(self):
        if self._yolo_model is not None:
            return
        from ultralytics import YOLO
        path = self.cfg.get("model", {}).get(
            "yolo_path", "models/yolov26nobbnew_merged_1024.engine")
        # Fallback: if the configured path doesn't exist, try models/<basename>.
        # This handles the case where the YAML still has the pre-reorg path
        # (before the model files were moved into models/).
        if not os.path.exists(path):
            alt = os.path.join(MODELS_DIR, os.path.basename(path))
            if os.path.exists(alt):
                print(f"[PROC]  model not found at {path!r}, using {alt!r}")
                path = alt
        print(f"[PROC]  loading YOLO model: {path}")
        t0 = time.monotonic()
        model = YOLO(path)
        dt = time.monotonic() - t0
        self._yolo_model = model
        self._det_names  = model.names if hasattr(model, "names") else {}
        print(f"[PROC]  YOLO loaded in {dt:.1f}s  classes={self._det_names}")

    def _detector_loop(self):
        det_count = 0
        last_load_fail_ms = 0.0   # monotonic; throttle retries
        while True:
            with self._lock:
                frame   = self._detector_frame
                det_frame_seq = self._detector_frame_seq
                enabled = self._detector_enabled
                self._detector_frame = None

            if not enabled or frame is None:
                time.sleep(0.01)
                continue

            if self._yolo_model is None:
                # Throttle load retries (2s) so a broken path doesn't spin.
                if time.monotonic() - last_load_fail_ms < 2.0:
                    time.sleep(0.05)
                    continue
                try:
                    self._load_yolo()
                    with self._lock:
                        self._det_error = ""   # success — clear any stale error
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    print(f"[PROC]  YOLO load FAILED: {msg}  (will retry; "
                          f"select a valid model in the GUI)")
                    last_load_fail_ms = time.monotonic()
                    with self._lock:
                        self._det_error = msg
                    # Do NOT disable detection — user may be in the middle of
                    # switching models; let them pick another path.
                    continue

            # Capture model locally so a concurrent reload (set_param sets
            # _yolo_model = None) can't turn it into None mid-predict.
            model = self._yolo_model
            if model is None:
                continue

            display_conf = float(self.cfg["detection"]["conf_thresh"])
            ai_assist_cfg = self.cfg["tracker"].get("ai_assist", {})
            ai_acq_cfg    = self.cfg["tracker"].get("ai_acquisition", {})
            any_ai_on = (bool(ai_assist_cfg.get("enabled", False))
                         or bool(ai_acq_cfg.get("enabled", False)))
            # Both AI features share the same low-conf threshold.
            assist_conf = float(ai_assist_cfg.get("conf_min", 0.25))
            # Run YOLO at the LOWER threshold whenever ANY AI feature is on
            # so the assist list contains the low-confidence detections they need.
            effective_conf = min(display_conf, assist_conf) if any_ai_on else display_conf

            t0 = time.monotonic()
            try:
                results = model.predict(frame, conf=effective_conf, verbose=False)
            except Exception as e:
                print(f"[PROC]  YOLO predict FAILED: {e}")
                time.sleep(0.1)
                continue
            dt_ms = (time.monotonic() - t0) * 1000

            display_dets, assist_dets = self._parse_detections(
                results, display_conf, assist_conf if any_ai_on else display_conf)

            # Collect AABB sizes per class for AI box-size estimation
            veh_set = set(n.lower() for n in self.cfg["detection"]["vehicle_names"])
            per_set = set(n.lower() for n in self.cfg["detection"]["person_names"])
            frame_veh, frame_per = [], []
            for det in assist_dets:
                lbl = det["label"].lower()
                poly = det["poly"].astype(np.int32)
                _, _, bw_det, bh_det = cv2.boundingRect(poly)
                if any(v in lbl for v in veh_set):
                    frame_veh.append((bw_det, bh_det))
                elif any(p in lbl for p in per_set):
                    frame_per.append((bw_det, bh_det))

            det_count += 1
            with self._lock:
                if not self._detector_enabled:
                    continue
                self._detections        = display_dets
                self._detections_assist = assist_dets
                self._det_seq           = det_frame_seq
                self._det_ms            = dt_ms
                self._det_count         = len(display_dets)
                if frame_veh:
                    self._det_veh_sizes.append(frame_veh)
                if frame_per:
                    self._det_per_sizes.append(frame_per)

            if det_count == 1 or det_count % 15 == 0:
                print(f"[PROC]  det #{det_count}  {dt_ms:.1f}ms  "
                      f"display={len(display_dets)} assist={len(assist_dets)}")

    def _parse_detections(self, results, display_conf, assist_conf):
        """
        Classify YOLO OBB detections into vehicle/person groups, then emit two
        filtered lists:
          - display: top-N per group with conf >= display_conf (drawn on screen)
          - assist:  top-N per group with conf >= assist_conf  (used by AI-assist only)
        """
        vehicles = []
        persons  = []
        other    = []
        veh_set = set(n.lower() for n in self.cfg["detection"]["vehicle_names"])
        per_set = set(n.lower() for n in self.cfg["detection"]["person_names"])
        top_n   = int(self.cfg["detection"]["top_n"])
        for result in results:
            if result.obb is None or len(result.obb) == 0:
                continue
            polys   = result.obb.xyxyxyxy.cpu().numpy()
            confs   = result.obb.conf.cpu().numpy()
            classes = result.obb.cls.cpu().numpy().astype(int)
            names   = result.names
            for poly, conf, cls in zip(polys, confs, classes):
                label = str(names[cls]).lower()
                det = {"poly": poly, "conf": float(conf), "label": names[cls]}
                if any(v in label for v in veh_set):
                    vehicles.append(det)
                elif any(p in label for p in per_set):
                    persons.append(det)
                else:
                    other.append(det)
        vehicles.sort(key=lambda d: d["conf"], reverse=True)
        persons.sort(key=lambda d: d["conf"], reverse=True)
        if vehicles == [] and persons == [] and other:
            seen = sorted({d["label"] for d in other})
            print(f"[PROC]  WARN: {len(other)} detections present but no class "
                  f"matched vehicle/person lists. Seen labels: {seen}  "
                  f"veh_set={veh_set}  per_set={per_set}")

        # Display list: top-N per class above display_conf (what gets drawn).
        # Assist list:  ALL detections above assist_conf (no cap) — AI features
        # see the full low-confidence pool, independent of display top_n.
        def top(src, thresh):
            return [d for d in src if d["conf"] >= thresh][:top_n]

        def all_above(src, thresh):
            return [d for d in src if d["conf"] >= thresh]

        display = top(vehicles, display_conf) + top(persons, display_conf)
        assist  = all_above(vehicles, assist_conf) + all_above(persons, assist_conf)
        return display, assist

    # ---------------- acquisition assist ----------------

    def _acq_assist_refine(self, frame, raw_bbox, margin):
        """
        Expand `raw_bbox` by `margin` (fraction of bbox), find Shi-Tomasi
        corners in the expanded region, drop outliers, and fit a tight
        bbox around the remaining corner cluster.  Returns refined
        (x, y, w, h) or None if not enough features were found (caller
        falls back to raw_bbox).
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

        # Enforce min size and stay inside the frame
        bmin = int(self.cfg["tracker"]["box_min"])
        bmax = int(self.cfg["tracker"]["box_max"])
        nw = max(bmin, min(bmax, nw))
        nh = max(bmin, min(bmax, nh))
        nx = max(0, min(nx, w_frame - nw))
        ny = max(0, min(ny, h_frame - nh))

        print(f"[PROC]  acq_assist: {len(kept)}/{len(corners)} corners  "
              f"raw=({x},{y},{bw},{bh}) -> refined=({nx},{ny},{nw},{nh})")
        return (nx, ny, nw, nh)

    # ---------------- AI box-size estimation ----------------

    def _get_auto_box_size(self, cls="vehicle"):
        """Compute median W and H from the last 5 detection frames' AABBs
        for the given class ('vehicle' or 'person').
        Returns (w, h) or None if no data."""
        with self._lock:
            history = self._det_veh_sizes if cls == "vehicle" else self._det_per_sizes
            all_sizes = [s for frame in history for s in frame]
        if len(all_sizes) < 2:
            return None
        ws = sorted(s[0] for s in all_sizes)
        hs = sorted(s[1] for s in all_sizes)
        return ws[len(ws) // 2], hs[len(hs) // 2]

    # ---------------- AI acquisition (click-time snap) ----------------

    def _ai_acquisition_snap(self, click_x, click_y, near_val, current_seq=0):
        """
        On a fresh click, return the AABB of the YOLO detection whose centre
        is closest to (click_x, click_y) IF within `near_val` pixels.
        Uses the wider _detections_assist list so low-confidence candidates
        can still snap.  Returns None when nothing qualifies.
        """
        with self._lock:
            det_age = current_seq - self._det_seq if current_seq else 0
            if det_age > self.MAX_DET_AGE:
                return None
            dets = list(self._detections_assist)
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

    # ---------------- AI track assist ----------------

    def _ai_assist_snap(self, frame, tracker_bbox, iou_min, current_seq=0):
        """
        If any YOLO detection overlaps the tracker bbox by IoU >= iou_min,
        return (detection_aabb, iou) — caller will reinit the tracker on it.
        Uses the wider `_detections_assist` list (lower conf threshold) so
        the assist can match against boxes that aren't confident enough to
        display on screen.  Returns None when no detection qualifies.
        """
        with self._lock:
            det_age = current_seq - self._det_seq if current_seq else 0
            if det_age > self.MAX_DET_AGE:
                return None
            dets = list(self._detections_assist)
        if not dets:
            return None

        h_frame, w_frame = frame.shape[:2]
        best_iou = 0.0
        best_aabb = None
        for det in dets:
            poly = det["poly"].astype(np.int32)
            ax, ay, aw, ah = cv2.boundingRect(poly)
            # Clamp to frame just in case
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

    # ---------------- drawing ----------------

    def _draw_bbox(self, frame, bbox, ai_active=False):
        x, y, bw, bh = bbox
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        cx = x + bw // 2
        cy = y + bh // 2
        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)
        # AI-assist indicator: downward triangle above the bbox top edge
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

    def _draw_detection(self, frame, det):
        label_lc = str(det["label"]).lower()
        veh_set = set(n.lower() for n in self.cfg["detection"]["vehicle_names"])
        if any(v in label_lc for v in veh_set):
            color = (0, 0, 255)     # red  — vehicles
        else:
            color = (255, 0, 0)     # blue — persons
        poly = det["poly"].astype(np.int32)
        cv2.polylines(frame, [poly], isClosed=True, color=color, thickness=2)
        return frame

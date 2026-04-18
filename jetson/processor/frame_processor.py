"""FrameProcessor: central tracker + detector orchestrator.

Owns the shared state (config, frame ring buffer, pause/catch-up state,
tracker + detector result slots).  The actual thread bodies live in
tracker_worker.py and detector_worker.py — this class is the state
container + public API.

Threading model:
  * capture thread: calls submit_frame() at ~30fps
  * tracker thread: consumes _tracker_frame, writes _bbox
  * detector thread: consumes _detector_frame, writes _detections
  * WS thread: calls setters + getters
All shared state is protected by `_lock`.
"""
import threading
import time
from collections import OrderedDict, deque

import numpy as np

from jetson.config import load_config, save_config, set_nested, deep_copy
from jetson.tracking import JumpDetector
from jetson.detection import load_yolo
from jetson.processor.drawing import draw_bbox, draw_detection
from jetson.processor.config_mutations import apply_side_effects
from jetson.processor.ai_snap import MAX_DET_AGE
from jetson.processor import detector_worker, tracker_worker


class FrameProcessor:
    """Tracker + YOLO OBB detector, each in its own thread."""

    FRAME_RING_SIZE = 300
    MAX_DET_AGE = MAX_DET_AGE
    MAX_BBOX_SIDE = 400   # hard ceiling — no bbox side can exceed this

    # ---- bbox property: every write is clamped to MAX_BBOX_SIDE ----

    @property
    def _bbox(self):
        return self.__bbox

    @_bbox.setter
    def _bbox(self, value):
        if value is None:
            self.__bbox = None
            return
        bx, by, bw, bh = [int(v) for v in value]
        mx = self.MAX_BBOX_SIDE
        if bw > mx:
            bx += (bw - mx) // 2
            bw = mx
        if bh > mx:
            by += (bh - mx) // 2
            bh = mx
        self.__bbox = (bx, by, bw, bh)

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
        self.__bbox         = None
        self._bbox_seq      = 0
        self._track_ms      = 0.0
        self._track_count   = 0
        self._jump          = JumpDetector(self.cfg)
        self._last_lost_reason = ""
        self._ai_assist_until_count = 0
        self._pending_resize = False

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
        self._detector_frame     = None
        self._detector_frame_seq = 0
        self._detector_enabled   = bool(self.cfg.get("detection", {}).get(
            "enabled_on_start", False))
        self._detections         = []   # drawn on screen
        self._detections_assist  = []   # wider set for AI assist
        self._det_seq            = 0
        self._det_ms             = 0.0
        self._det_count          = 0
        self._yolo_model         = None
        self._det_names          = {}
        self._det_error          = ""

        # AI box-size estimation — rolling window of AABB sizes per class
        self._det_veh_sizes = deque(maxlen=5)
        self._det_per_sizes = deque(maxlen=5)
        self._auto_box_notify = None

        # Worker threads (bodies in tracker_worker.py / detector_worker.py)
        threading.Thread(
            target=tracker_worker.tracker_loop,   args=(self,), daemon=True).start()
        threading.Thread(
            target=detector_worker.detector_loop, args=(self,), daemon=True).start()
        threading.Thread(
            target=self._preload_yolo, daemon=True).start()

    # ================================================================
    #  Capture thread API
    # ================================================================

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
            vehicle_names = list(self.cfg["detection"]["vehicle_names"])
        for det in detections:
            frame = draw_detection(frame, det, vehicle_names)
        if bbox is not None:
            frame = draw_bbox(frame, bbox, ai_active=ai_active)
        return frame

    # ================================================================
    #  WS setters (tracker control)
    # ================================================================

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
            mx = min(int(self.cfg["tracker"]["box_max"]), self.MAX_BBOX_SIDE)
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

    # ================================================================
    #  Pause / resume
    # ================================================================

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
            return (self._paused, self._catching_up,
                    self._paused_frame, self._catchup_frame)

    # ================================================================
    #  Detector control
    # ================================================================

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

    # ================================================================
    #  Config API
    # ================================================================

    def get_config(self):
        with self._lock:
            cfg = deep_copy(self.cfg)
            cfg["_runtime"] = {"detection_on": self._detector_enabled}
            return cfg

    def set_param(self, dotted_path: str, value):
        with self._lock:
            set_nested(self.cfg, dotted_path, value)
            cur = self.cfg
            for k in dotted_path.split("."):
                cur = cur[k]
            cur = apply_side_effects(self, dotted_path, cur)
        print(f"[CONFIG] {dotted_path} = {cur!r}")
        return cur

    def save_default(self):
        with self._lock:
            cfg_copy = deep_copy(self.cfg)
        save_config(cfg_copy)

    # ================================================================
    #  Telemetry
    # ================================================================

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

    # ================================================================
    #  YOLO lazy loader (used by detector_worker)
    # ================================================================

    def _preload_yolo(self):
        try:
            self._load_yolo()
        except Exception as e:
            print(f"[PROC]  YOLO preload FAILED: {e}")

    def _load_yolo(self):
        if self._yolo_model is not None:
            return
        path = self.cfg.get("model", {}).get(
            "yolo_path", "models/yolov26nobbnew_merged_1024.engine")
        model, names = load_yolo(path)
        self._yolo_model = model
        self._det_names  = names

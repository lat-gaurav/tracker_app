"""Detector thread body: runs YOLO on incoming frames, stores results
with the frame_seq they came from, feeds AI-box-size statistics.
"""
import time

import cv2
import numpy as np

from jetson.detection import parse_detections


def detector_loop(processor):
    """Main body of FrameProcessor._detector_loop.  Takes the processor
    instance and runs forever."""
    det_count = 0
    last_load_fail_ms = 0.0      # monotonic; throttle model-load retries

    while True:
        with processor._lock:
            frame         = processor._detector_frame
            det_frame_seq = processor._detector_frame_seq
            enabled       = processor._detector_enabled
            processor._detector_frame = None

        if not enabled or frame is None:
            time.sleep(0.01)
            continue

        # Lazy model load with error throttling
        if processor._yolo_model is None:
            if time.monotonic() - last_load_fail_ms < 2.0:
                time.sleep(0.05)
                continue
            try:
                processor._load_yolo()
                with processor._lock:
                    processor._det_error = ""
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"[PROC]  YOLO load FAILED: {msg} (will retry)")
                last_load_fail_ms = time.monotonic()
                with processor._lock:
                    processor._det_error = msg
                continue

        model = processor._yolo_model
        if model is None:
            continue

        # Effective confidence: use the LOWER of display/assist when any AI
        # feature is on, so the assist list includes low-conf candidates
        display_conf  = float(processor.cfg["detection"]["conf_thresh"])
        ai_assist_cfg = processor.cfg["tracker"].get("ai_assist", {})
        ai_acq_cfg    = processor.cfg["tracker"].get("ai_acquisition", {})
        any_ai_on = (bool(ai_assist_cfg.get("enabled", False))
                     or bool(ai_acq_cfg.get("enabled", False)))
        assist_conf = float(ai_assist_cfg.get("conf_min", 0.25))
        effective_conf = min(display_conf, assist_conf) if any_ai_on else display_conf

        t0 = time.monotonic()
        try:
            results = model.predict(frame, conf=effective_conf, verbose=False)
        except Exception as e:
            print(f"[PROC]  YOLO predict FAILED: {e}")
            time.sleep(0.1)
            continue
        dt_ms = (time.monotonic() - t0) * 1000

        display_dets, assist_dets = parse_detections(
            results, display_conf,
            assist_conf if any_ai_on else display_conf,
            processor.cfg)

        # Collect per-class AABB sizes for AI box-size estimation
        veh_set = set(n.lower() for n in processor.cfg["detection"]["vehicle_names"])
        per_set = set(n.lower() for n in processor.cfg["detection"]["person_names"])
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
        with processor._lock:
            if not processor._detector_enabled:
                continue
            processor._detections        = display_dets
            processor._detections_assist = assist_dets
            processor._det_seq           = det_frame_seq
            processor._det_ms            = dt_ms
            processor._det_count         = len(display_dets)
            if frame_veh:
                processor._det_veh_sizes.append(frame_veh)
            if frame_per:
                processor._det_per_sizes.append(frame_per)

        if det_count == 1 or det_count % 15 == 0:
            print(f"[PROC]  det #{det_count}  {dt_ms:.1f}ms  "
                  f"display={len(display_dets)} assist={len(assist_dets)}")

"""Tracker thread body.  Consumes the latest submitted frame, handles
resize / click / drag commands, initialises the tracker, runs updates
through the jump detector, and invokes periodic AI Assist snaps.
"""
import time

from jetson.tracking import create_tracker, acq_assist_refine
from jetson.processor.catchup import catch_up_to_live
from jetson.processor.ai_snap import (
    ai_acquisition_snap, ai_assist_snap, get_auto_box_size,
)


def tracker_loop(processor):
    """Main body of FrameProcessor._tracker_loop."""
    tracker = None
    last_track_time = None
    last_track_seq  = 0

    while True:
        with processor._lock:
            frame     = processor._tracker_frame
            frame_seq = processor._tracker_frame_seq
            processor._tracker_frame = None

        if frame is None:
            time.sleep(0.005)
            continue

        now = time.monotonic()

        with processor._lock:
            pending    = processor._pending_click
            click_seq  = processor._pending_click_seq
            drag_rect  = processor._pending_drag_rect
            processor._pending_click = None
            processor._pending_drag_rect = None
            bw = processor._box_w
            bh = processor._box_h
            resize_now = processor._pending_resize
            processor._pending_resize = False

        h, w = frame.shape[:2]

        if pending == "clear":
            tracker = None
            processor._jump.reset()
            last_track_time = None
            last_track_seq  = 0
            continue

        # Live resize: swap engine / resize box on active tracker
        if resize_now and tracker is not None and processor._bbox is not None:
            tracker, last_track_time, last_track_seq = _handle_resize(
                processor, frame, frame_seq, bw, bh, w, h, now)

        # Click / drag: init a new tracker, optionally catch up
        if pending is not None and pending != "clear":
            tracker, last_track_time, last_track_seq = _handle_click(
                processor, frame, frame_seq, pending, click_seq,
                drag_rect, bw, bh, now)
            continue

        # Normal tracker update
        if tracker is not None:
            tracker, last_track_time, last_track_seq = _handle_update(
                processor, tracker, frame, frame_seq, now,
                last_track_time, last_track_seq)


# ---- sub-handlers ----

def _handle_resize(processor, frame, frame_seq, bw, bh, w, h, now):
    bx, by, bw_old, bh_old = processor._bbox
    cx_old = bx + bw_old / 2.0
    cy_old = by + bh_old / 2.0
    nx = max(0, min(int(cx_old - bw / 2), w - bw))
    ny = max(0, min(int(cy_old - bh / 2), h - bh))
    new_bbox = (nx, ny, bw, bh)
    ttype = processor.cfg["tracker"]["type"]
    tracker = create_tracker(ttype)
    tracker.init(frame, new_bbox)
    processor._jump.init_tracker(new_bbox)
    with processor._lock:
        processor._bbox = new_bbox
        processor._bbox_seq = frame_seq
    print(f"[PROC]  live resize -> {new_bbox}")
    return tracker, now, frame_seq


def _handle_click(processor, frame, frame_seq, pending, click_seq, drag_rect,
                  bw, bh, now):
    ox, oy = pending

    # Click-to-frame lookup
    init_frame = frame
    init_seq = frame_seq
    with processor._lock:
        if click_seq in processor._frame_ring:
            init_frame = processor._frame_ring[click_seq]
            init_seq = click_seq
        elif processor._frame_ring:
            oldest_seq = next(iter(processor._frame_ring))
            if oldest_seq > click_seq:
                init_frame = processor._frame_ring[oldest_seq]
                init_seq = oldest_seq

    ih, iw = init_frame.shape[:2]
    bbox, init_path, bw, bh = _resolve_init_bbox(
        processor, init_frame, init_seq, ox, oy, drag_rect, bw, bh, ih, iw)

    ttype = processor.cfg["tracker"]["type"]
    tracker = create_tracker(ttype)
    tracker.init(init_frame, bbox)
    processor._jump.init_tracker(bbox)
    last_track_time = now
    last_track_seq = init_seq
    with processor._lock:
        processor._bbox = bbox
        processor._bbox_seq = init_seq
    print(f"[PROC]  tracker init [{init_path}]  bbox={bbox}  "
          f"click_seq={click_seq} init_seq={init_seq}")

    # Iterative catch-up
    catchup_from = init_seq
    for _pass in range(5):
        with processor._lock:
            gap = processor._frame_seq - catchup_from
        if gap <= 2:
            break
        tracker, ok = catch_up_to_live(processor, tracker, catchup_from)
        if not ok or tracker is None:
            tracker = None
            last_track_time = None
            last_track_seq = 0
            break
        last_track_time = time.monotonic()
        with processor._lock:
            last_track_seq = processor._bbox_seq
            catchup_from   = processor._bbox_seq

    # Auto-resume after catch-up
    with processor._lock:
        if processor._paused:
            processor._paused = False
            processor._paused_seq = 0
            processor._paused_frame = None
            print("[PROC]  auto-RESUMED after catch-up")

    return tracker, last_track_time, last_track_seq


def _resolve_init_bbox(processor, init_frame, init_seq, ox, oy, drag_rect,
                       bw, bh, ih, iw):
    """Pick the init bbox from click/drag + optional AI assists."""
    if drag_rect is not None:
        dnx, dny, dnw, dnh = drag_rect
        dx = max(0, int(dnx * iw))
        dy = max(0, int(dny * ih))
        dw = max(1, min(int(dnw * iw), iw - dx))
        dh = max(1, min(int(dnh * ih), ih - dy))
        return (dx, dy, dw, dh), "drag", bw, bh

    cx = int(ox * iw)
    cy = int(oy * ih)

    # AI box-size estimation
    ai_bs = processor.cfg["tracker"].get("ai_box_size", {})
    if ai_bs.get("enabled", False):
        cls = str(ai_bs.get("class", "vehicle")).lower()
        auto = get_auto_box_size(processor, cls, processor.MAX_BBOX_SIDE)
        if auto is not None:
            bw, bh = auto
            with processor._lock:
                processor._box_w = bw
                processor._box_h = bh
                processor.cfg["tracker"]["box_w_default"] = bw
                processor.cfg["tracker"]["box_h_default"] = bh
                processor._auto_box_notify = (bw, bh)

    # AI Acquisition — snap to nearest YOLO detection
    ai_acq = processor.cfg["tracker"].get("ai_acquisition", {})
    if ai_acq.get("enabled", False):
        snapped = ai_acquisition_snap(
            processor, cx, cy, float(ai_acq.get("near_val", 150)), init_seq)
        if snapped is not None:
            return snapped, "ai_acquisition", bw, bh

    # Raw click bbox, optionally refined by acquisition assist
    x = max(0, min(cx - bw // 2, iw - bw))
    y = max(0, min(cy - bh // 2, ih - bh))
    raw_bbox = (x, y, bw, bh)
    acq = processor.cfg["tracker"].get("acq_assist", {})
    if acq.get("enabled", False):
        margin = float(acq.get("margin", 0.30))
        box_min = int(processor.cfg["tracker"]["box_min"])
        box_max = int(processor.cfg["tracker"]["box_max"])
        refined = acq_assist_refine(init_frame, raw_bbox, margin, box_min, box_max)
        if refined is not None:
            return refined, "acq_assist", bw, bh
    return raw_bbox, "raw", bw, bh


def _handle_update(processor, tracker, frame, frame_seq, now,
                   last_track_time, last_track_seq):
    t0 = time.monotonic()
    success, bbox = tracker.update(frame)
    dt_ms = (time.monotonic() - t0) * 1000

    frames_skipped = (frame_seq - last_track_seq
                      if last_track_time is not None else 1)

    if not success:
        print(f"[PROC]  tracker LOST ({dt_ms:.1f}ms) seq={frame_seq}")
        processor._jump.reset()
        with processor._lock:
            processor._bbox = None
            processor._bbox_seq = 0
            processor._track_ms = 0.0
            processor._last_lost_reason = f"csrt update failed (skipped={frames_skipped})"
        return None, None, 0

    is_jump, metrics = processor._jump.check(bbox, frames_skipped)
    if is_jump:
        reason = (f"jump dist={metrics['dist_ratio']:.2f} "
                  f"size={metrics['size_ratio']:.2f} "
                  f"iou={metrics['iou']:.2f} skipped={frames_skipped}")
        print(f"[PROC]  tracker JUMP — dropping ({reason})")
        processor._jump.reset()
        with processor._lock:
            processor._bbox = None
            processor._bbox_seq = 0
            processor._track_ms = 0.0
            processor._last_lost_reason = reason
        return None, None, 0

    processor._track_count += 1
    with processor._lock:
        if processor._pending_click == "clear":
            return tracker, now, frame_seq
        processor._bbox = tuple(int(v) for v in bbox)
        processor._bbox_seq = frame_seq
        processor._track_ms = dt_ms

    # Periodic AI Assist snap
    ai_cfg = processor.cfg["tracker"].get("ai_assist", {})
    if (ai_cfg.get("enabled", False)
            and processor._track_count %
                max(1, int(ai_cfg.get("interval", 30))) == 0):
        snapped = ai_assist_snap(processor, frame, processor._bbox,
                                 float(ai_cfg.get("iou_min", 0.10)),
                                 frame_seq)
        if snapped is not None:
            new_bbox, iou = snapped
            ttype = processor.cfg["tracker"]["type"]
            tracker = create_tracker(ttype)
            tracker.init(frame, new_bbox)
            processor._jump.init_tracker(new_bbox)
            with processor._lock:
                processor._bbox = new_bbox
                processor._bbox_seq = frame_seq
                processor._ai_assist_until_count = processor._track_count + 15

    if processor._track_count == 1 or processor._track_count % 30 == 0:
        print(f"[PROC]  track #{processor._track_count}  {dt_ms:.1f}ms  "
              f"bbox={processor._bbox}  seq={frame_seq}")

    return tracker, now, frame_seq

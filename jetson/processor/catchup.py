"""Catch-up replay: after a click during pause, replay buffered frames through
the tracker as fast as possible so the bbox is up-to-date by the time we
return to live.  Every frame is processed (no skipping) — with csrt-fast
(~5ms) this runs at ~6.6× real-time."""

# Update shared display frame every Nth processed frame to reduce lock churn
CATCHUP_DISPLAY_INTERVAL = 5


def catch_up_to_live(processor, tracker, from_seq):
    """Replay every frame in the ring buffer from `from_seq` to live.
    Returns (tracker, success) — tracker may be None if lost during replay.

    `processor` is the FrameProcessor instance (we access shared state via
    its lock).  Kept as a function rather than a method so the replay logic
    is isolated and testable.
    """
    with processor._lock:
        processor._catching_up = True
        live_seq = processor._frame_seq
        replay_frames = [(s, processor._frame_ring[s])
                         for s in sorted(processor._frame_ring)
                         if from_seq < s <= live_seq]

    total = len(replay_frames)
    print(f"[CATCHUP] start: {total} frames  from_seq={from_seq} live_seq={live_seq}")

    processed = 0
    last_seq = from_seq

    for seq, frame in replay_frames:
        # Abort check — every 8 frames is enough, no need to lock every step
        if processed % 8 == 0:
            with processor._lock:
                if processor._pending_click is not None:
                    print(f"[CATCHUP] ABORTED at seq={seq}")
                    processor._catching_up = False
                    processor._catchup_frame = None
                    return tracker, True

        success, bbox = tracker.update(frame)

        if not success:
            print(f"[CATCHUP] LOST at seq={seq} ({processed}/{total})")
            processor._jump.reset()
            with processor._lock:
                processor._bbox = None
                processor._bbox_seq = 0
                processor._track_ms = 0.0
                processor._last_lost_reason = "lost during catch-up"
                processor._catching_up = False
                processor._catchup_frame = None
            return None, False

        frames_skipped = seq - last_seq
        is_jump, metrics = processor._jump.check(bbox, frames_skipped)
        if is_jump:
            reason = f"jump during catch-up seq={seq} dist={metrics['dist_ratio']:.2f}"
            print(f"[CATCHUP] JUMP — {reason}")
            processor._jump.reset()
            with processor._lock:
                processor._bbox = None
                processor._bbox_seq = 0
                processor._track_ms = 0.0
                processor._last_lost_reason = reason
                processor._catching_up = False
                processor._catchup_frame = None
            return None, False

        processed += 1
        last_seq = seq

        # Update display state — refresh the display frame only every Nth
        # iteration to save lock acquisitions + frame copies
        if processed % CATCHUP_DISPLAY_INTERVAL == 0 or processed == total:
            with processor._lock:
                processor._bbox = tuple(int(v) for v in bbox)
                processor._bbox_seq = seq
                processor._catchup_frame = frame
        else:
            with processor._lock:
                processor._bbox = tuple(int(v) for v in bbox)
                processor._bbox_seq = seq

    with processor._lock:
        processor._catching_up = False
        processor._catchup_frame = None
        cur_live = processor._frame_seq
    print(f"[CATCHUP] DONE: {processed}/{total} frames  "
          f"seq {from_seq}->{live_seq}  gap_to_live={cur_live - last_seq}")
    return tracker, True

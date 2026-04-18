"""Capture thread: reads frames from camera or video file, feeds the
FrameProcessor, draws overlays, and pushes to the RTSP appsrc.

Runs as a daemon thread started by the main entry point.
"""
import os
import time
import json

import cv2
import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from jetson.constants import STREAM_W, STREAM_H, FPS
from jetson.streaming.canvas import fit_to_canvas
from jetson.streaming.pipelines import build_cam_pipeline


def _overlay_caption(frame, text, color):
    cv2.putText(frame, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                color, 2, cv2.LINE_AA)


def _prepare_display(processor, frame):
    """Decide what to display: live tracker, paused frozen, or catch-up.
    Returns a new BGR frame ready for RTSP push."""
    paused, catching_up, frozen_frame, cu_frame = processor.get_display_state()

    if catching_up:
        if cu_frame is not None:
            display = cu_frame.copy()
            display = processor.draw(display)
        elif frozen_frame is not None:
            display = frozen_frame.copy()
            display = processor.draw(display, paused_view=True)
        else:
            display = processor.draw(frame)
        _overlay_caption(display, "CATCHING UP", (0, 165, 255))
        return display

    if paused:
        display = frozen_frame.copy() if frozen_frame is not None else frame.copy()
        display = processor.draw(display, paused_view=True)
        _overlay_caption(display, "PAUSED", (0, 0, 255))
        return display

    return processor.draw(frame)


def _record_frame(processor, recorder, frame, frame_counter):
    """Enqueue a clean frame + overlay snapshot for the recording writer."""
    if not recorder.is_active():
        return
    try:
        with processor._lock:
            ov = {
                "ts": time.time(),
                "frame": frame_counter + 1,
                "frame_seq": processor._frame_seq,
                "tracker_bbox": list(processor._bbox) if processor._bbox else None,
                "tracker_bbox_seq": processor._bbox_seq,
                "track_ms": round(processor._track_ms, 1),
                "ai_active": processor._track_count < processor._ai_assist_until_count,
                "det_seq": processor._det_seq,
                "detections": [
                    {"label": d["label"],
                     "conf": round(d["conf"], 3),
                     "aabb": list(cv2.boundingRect(d["poly"].astype(np.int32)))}
                    for d in processor._detections
                ],
            }
        recorder.enqueue_frame(frame.tobytes(), json.dumps(ov))
    except Exception as e:
        print(f"[REC]   enqueue error: {e}")


class _Source:
    """Wraps either a GStreamer appsink (for cameras) or a cv2.VideoCapture
    (for video files).  Provides a uniform read_frame() interface."""

    def __init__(self):
        self.cap_cv   = None
        self.gst_pipe = None
        self.gst_sink = None

    def stop(self):
        if self.gst_pipe is not None:
            self.gst_pipe.set_state(Gst.State.NULL)
            self.gst_pipe = None
        if self.cap_cv is not None:
            self.cap_cv.release()
            self.cap_cv = None
        self.gst_sink = None

    def start(self, source, processor):
        self.stop()
        if source.startswith("/dev/video"):
            if not os.path.exists(source):
                print(f"[CAP]   Device {source} not found — waiting for source change")
                return
            try:
                self.gst_pipe = Gst.parse_launch(
                    build_cam_pipeline(source, processor.cfg.get("camera", {})))
                self.gst_sink = self.gst_pipe.get_by_name('sink')
                self.gst_pipe.set_state(Gst.State.PLAYING)
            except Exception as e:
                print(f"[CAP]   Camera pipeline failed: {e}")
                self.gst_pipe = None
                self.gst_sink = None
                return
            print(f"[CAP]   Camera started: {source}")
        else:
            self.cap_cv = cv2.VideoCapture(source)
            if not self.cap_cv.isOpened():
                print(f"[CAP]   ERROR: cannot open {source}")
                self.cap_cv = None
                return
            src_w = int(self.cap_cv.get(cv2.CAP_PROP_FRAME_WIDTH))
            src_h = int(self.cap_cv.get(cv2.CAP_PROP_FRAME_HEIGHT))
            src_fps = self.cap_cv.get(cv2.CAP_PROP_FPS) or 30
            print(f"[CAP]   Video file opened: {source} "
                  f"({src_w}x{src_h} @ {src_fps:.0f}fps)")

    def read_frame(self):
        if self.gst_sink is not None:
            sample = self.gst_sink.emit('try-pull-sample', 200 * 1000000)
            if sample is None:
                return None
            buf = sample.get_buffer()
            caps = sample.get_caps()
            s = caps.get_structure(0)
            w = s.get_value('width')
            h = s.get_value('height')
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3)).copy()
            buf.unmap(mapinfo)
            return frame
        if self.cap_cv is not None:
            ok, frame = self.cap_cv.read()
            if not ok:
                self.cap_cv.set(cv2.CAP_PROP_POS_FRAMES, 0)
                print("[CAP]   Video looped")
                return None
            return frame
        return None

    @property
    def is_video_file(self):
        return self.cap_cv is not None


def capture_and_push(processor, rtsp_state, recorder, initial_source):
    """Main capture loop.  Runs in its own daemon thread.

    Args:
        processor:      FrameProcessor instance
        rtsp_state:     RTSPState (has .appsrc, .source_request)
        recorder:       Recorder instance (non-blocking enqueue API)
        initial_source: '/dev/videoN' or path to video file
    """
    source = _Source()
    current_source = initial_source
    source.start(current_source, processor)

    frame_duration_ns = int(1e9 / FPS)
    frame_interval    = 1.0 / FPS
    pts       = 0
    frame_n   = 0
    first     = True

    while True:
        t_start = time.monotonic()

        # Hot-switch source if requested
        if rtsp_state.source_request is not None:
            new_source = rtsp_state.source_request
            rtsp_state.source_request = None
            print(f"[CAP]   Switching source: {current_source} -> {new_source}")
            processor.clear_track()
            current_source = new_source
            source.start(current_source, processor)
            pts = 0
            frame_n = 0
            first = True
            continue

        frame = source.read_frame()
        if frame is None:
            continue

        if first:
            h, w = frame.shape[:2]
            print(f"[CAP]   First frame: {w}x{h} -> canvas {STREAM_W}x{STREAM_H}")
            first = False

        frame = fit_to_canvas(frame)
        processor.submit_frame(frame)

        display_frame = _prepare_display(processor, frame)
        _record_frame(processor, recorder, frame, recorder.frame_count)

        # Push to RTSP appsrc
        if rtsp_state.appsrc is not None:
            out_buf = Gst.Buffer.new_wrapped(display_frame.tobytes())
            out_buf.pts      = pts
            out_buf.duration = frame_duration_ns
            pts += frame_duration_ns
            ret = rtsp_state.appsrc.emit('push-buffer', out_buf)
            if ret == Gst.FlowReturn.FLUSHING or ret == Gst.FlowReturn.ERROR:
                print(f"[CAP]   push-buffer returned {ret} — clearing appsrc")
                rtsp_state.appsrc = None
                pts = 0
            elif frame_n % 150 == 0:
                print(f"[CAP]   frame={frame_n} push-buffer ret={ret}")
        else:
            if frame_n % 150 == 0:
                print(f"[CAP]   frame={frame_n} appsrc=None (waiting for client)")

        frame_n += 1

        # Pace video files to ~30fps (cameras are paced by the sensor)
        if source.is_video_file:
            elapsed = time.monotonic() - t_start
            sleep_s = frame_interval - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)

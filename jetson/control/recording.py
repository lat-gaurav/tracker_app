"""Recording: ffmpeg H.264 writer thread + overlay JSONL + input log.

The capture thread calls enqueue_frame() to push (bytes, json_str) pairs.
A background writer thread drains the queue into ffmpeg stdin and the
overlay file so disk I/O never blocks capture.  Frames dropped when the
queue is full (ffmpeg too slow) are silently skipped.
"""
import os
import json
import queue as _queue_mod
import subprocess
import threading
import time

import yaml

from jetson.constants import STREAM_W, STREAM_H, FPS, RECORDINGS_DIR


class Recorder:
    """Non-blocking H.264 recorder with synchronized overlay metadata."""

    def __init__(self, processor):
        self._processor   = processor
        self._active      = False
        self._dir         = None
        self._proc        = None        # ffmpeg subprocess
        self._overlay_f   = None        # overlay.jsonl file handle
        self._inputs_f    = None        # inputs.jsonl file handle
        self._start_ts    = 0.0
        self._frame_n     = 0
        self._queue       = None        # queue.Queue, holds (bytes, json)
        self._writer      = None        # writer thread

    # ---- public API ----

    def start(self):
        if self._active:
            return
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        self._dir = os.path.join(RECORDINGS_DIR, ts)
        os.makedirs(self._dir, exist_ok=True)

        # Config snapshot
        cfg = self._processor.get_config()
        cfg.pop("_runtime", None)
        with open(os.path.join(self._dir, "config.yaml"), "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

        # ffmpeg: raw BGR stdin → H.264 elementary stream
        video_path = os.path.join(self._dir, "video.h264")
        self._proc = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{STREAM_W}x{STREAM_H}", "-r", str(FPS),
             "-i", "-",
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
             "-crf", "23", "-f", "h264", video_path],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        self._overlay_f = open(os.path.join(self._dir, "overlay.jsonl"), "a")
        self._inputs_f  = open(os.path.join(self._dir, "inputs.jsonl"), "a")
        self._start_ts  = time.time()
        self._frame_n   = 0

        self._queue = _queue_mod.Queue(maxsize=60)   # ~2s buffer at 30fps
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

        self._active = True
        print(f"[REC]   Started → {self._dir}")

    def stop(self):
        if not self._active:
            return
        self._active = False
        if self._queue:
            try:
                self._queue.put(None, timeout=1)
            except Exception:
                pass
            time.sleep(0.5)   # drain
        self._queue = None
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None
        if self._overlay_f:
            self._overlay_f.close()
            self._overlay_f = None
        if self._inputs_f:
            self._inputs_f.close()
            self._inputs_f = None
        print(f"[REC]   Stopped → {self._dir}")

    def enqueue_frame(self, frame_bytes, overlay_json):
        """Non-blocking: drop the frame if ffmpeg can't keep up."""
        if not self._active or self._queue is None:
            return
        try:
            self._queue.put_nowait((frame_bytes, overlay_json))
        except _queue_mod.Full:
            pass

    def log_input(self, cmd_line):
        """Append a WS command to inputs.jsonl for replay / debugging."""
        if not self._active or self._inputs_f is None:
            return
        try:
            self._inputs_f.write(
                json.dumps({"ts": time.time(), "cmd": cmd_line}) + "\n")
            self._inputs_f.flush()
        except Exception:
            pass

    # ---- status queries ----

    def is_active(self):
        return self._active

    @property
    def frame_count(self):
        return self._frame_n

    @property
    def directory(self):
        return self._dir

    @property
    def start_ts(self):
        return self._start_ts

    # ---- writer thread ----

    def _writer_loop(self):
        while True:
            q = self._queue
            if q is None:
                break
            try:
                item = q.get(timeout=0.5)
            except Exception:
                continue
            if item is None:
                break
            frame_bytes, overlay_json = item
            try:
                if self._proc and self._proc.stdin:
                    self._proc.stdin.write(frame_bytes)
                self._frame_n += 1
                if self._overlay_f:
                    self._overlay_f.write(overlay_json + "\n")
                    self._overlay_f.flush()
            except Exception as e:
                print(f"[REC]   writer error: {e}")

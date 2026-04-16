"""
Reconstruct an MP4 video from a recording session.

Reads the raw H.264 video (and optionally overlay.jsonl) from a recording
directory and produces a new MP4 alongside the source files.

By default the output has tracker bboxes and detection polygons drawn on each
frame — identical to what the ground station saw during the live session.
With --no-overlay, a clean MP4 (no annotations) is produced instead.

Usage:
    python3 reconstruct.py recordings/2026-04-14_15-30-00
    python3 reconstruct.py recordings/2026-04-14_15-30-00 --no-overlay
    python3 reconstruct.py recordings/2026-04-14_15-30-00 -o custom_name.mp4
"""

import argparse
import os
import json
import sys
import cv2
import numpy as np


def load_overlays(path):
    overlays = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                overlays.append(json.loads(line))
    return overlays


def draw_overlay(frame, ov):
    # Detections
    for det in ov.get("detections", []):
        aabb = det.get("aabb")
        if aabb:
            x, y, bw, bh = aabb
            cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 0, 255), 2)

    # Tracker bbox
    bbox = ov.get("tracker_bbox")
    if bbox:
        x, y, bw, bh = [int(v) for v in bbox]
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        cx = x + bw // 2
        cy = y + bh // 2
        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)

        # AI assist triangle
        if ov.get("ai_active"):
            size = 9
            tri = np.array([
                [cx - size, max(0, y - size - 4)],
                [cx + size, max(0, y - size - 4)],
                [cx,        max(0, y - 2)],
            ], dtype=np.int32)
            cv2.drawContours(frame, [tri], 0, (0, 255, 255), -1)


def main():
    parser = argparse.ArgumentParser(
        description="Reconstruct an MP4 from a recording session.")
    parser.add_argument("rec_dir",
                        help="Recording directory (containing video.h264).")
    parser.add_argument("--no-overlay", action="store_true",
                        help="Produce a clean MP4 with no annotations drawn.")
    parser.add_argument("-o", "--output", default=None,
                        help="Output filename (saved inside rec_dir). "
                             "Defaults to overlay.mp4 or raw.mp4.")
    args = parser.parse_args()

    rec_dir = args.rec_dir.rstrip("/")
    if not os.path.isdir(rec_dir):
        print(f"[ERROR] {rec_dir} is not a directory")
        sys.exit(1)

    video_path = os.path.join(rec_dir, "video.h264")
    if not os.path.exists(video_path):
        print(f"[ERROR] {video_path} not found")
        sys.exit(1)

    # Always write inside the recording directory — strip any path the user
    # passes via -o so we can never accidentally write elsewhere.
    default_name = "raw.mp4" if args.no_overlay else "overlay.mp4"
    out_name = os.path.basename(args.output) if args.output else default_name
    out_path = os.path.join(rec_dir, out_name)

    overlays = []
    if not args.no_overlay:
        overlay_path = os.path.join(rec_dir, "overlay.jsonl")
        if not os.path.exists(overlay_path):
            print(f"[ERROR] {overlay_path} not found "
                  f"(use --no-overlay to skip annotations)")
            sys.exit(1)
        overlays = load_overlays(overlay_path)
        print(f"[INFO] Loaded {len(overlays)} overlay entries")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[INFO] Video: {w}x{h} @ {fps:.0f}fps  ({total} frames)")
    print(f"[INFO] Mode: {'raw (no overlay)' if args.no_overlay else 'overlay'}")
    print(f"[INFO] Output: {out_path}")

    writer = cv2.VideoWriter(out_path,
                             cv2.VideoWriter_fourcc(*"mp4v"),
                             fps, (w, h))

    frame_n = 0
    ov_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_n += 1

        if not args.no_overlay:
            # Find the overlay entry whose frame number matches this frame.
            ov = None
            while (ov_idx < len(overlays)
                   and overlays[ov_idx].get("frame", 0) <= frame_n):
                if overlays[ov_idx].get("frame", 0) == frame_n:
                    ov = overlays[ov_idx]
                ov_idx += 1
            if ov:
                draw_overlay(frame, ov)

        writer.write(frame)

        if frame_n % 100 == 0:
            print(f"[INFO] Frame {frame_n}/{total}")

    writer.release()
    cap.release()
    print(f"[DONE] Wrote {frame_n} frames to {out_path}")


if __name__ == "__main__":
    main()

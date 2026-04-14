"""
Reconstruct an overlaid video from a recording session.

Reads the raw H.264 video + overlay.jsonl from a recording directory and
produces a new MP4 with tracker bboxes and detection polygons drawn on each
frame — identical to what the ground station saw during the live session.

Usage:
    python3 reconstruct.py recordings/2026-04-14_15-30-00
    python3 reconstruct.py recordings/2026-04-14_15-30-00 -o output.mp4
"""

import sys
import os
import json
import cv2
import numpy as np

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 reconstruct.py <recording_dir> [-o output.mp4]")
        sys.exit(1)

    rec_dir = sys.argv[1]
    out_path = "output_overlay.mp4"
    if "-o" in sys.argv:
        out_path = sys.argv[sys.argv.index("-o") + 1]

    video_path   = os.path.join(rec_dir, "video.h264")
    overlay_path = os.path.join(rec_dir, "overlay.jsonl")

    if not os.path.exists(video_path):
        print(f"[ERROR] {video_path} not found")
        sys.exit(1)
    if not os.path.exists(overlay_path):
        print(f"[ERROR] {overlay_path} not found")
        sys.exit(1)

    # Load all overlay entries
    overlays = []
    with open(overlay_path) as f:
        for line in f:
            line = line.strip()
            if line:
                overlays.append(json.loads(line))
    print(f"[INFO] Loaded {len(overlays)} overlay entries")

    # Open video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[INFO] Video: {w}x{h} @ {fps:.0f}fps  ({total} frames)")
    print(f"[INFO] Output: {out_path}")

    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

    frame_n = 0
    ov_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_n += 1

        # Find matching overlay entry (by frame number)
        ov = None
        while ov_idx < len(overlays) and overlays[ov_idx].get("frame", 0) <= frame_n:
            if overlays[ov_idx].get("frame", 0) == frame_n:
                ov = overlays[ov_idx]
            ov_idx += 1

        if ov:
            # Draw detections
            for det in ov.get("detections", []):
                aabb = det.get("aabb")
                if aabb:
                    x, y, bw, bh = aabb
                    color = (0, 0, 255)   # red for vehicle (default)
                    cv2.rectangle(frame, (x, y), (x + bw, y + bh), color, 2)

            # Draw tracker bbox
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

        writer.write(frame)

        if frame_n % 100 == 0:
            print(f"[INFO] Frame {frame_n}/{total}")

    writer.release()
    cap.release()
    print(f"[DONE] Wrote {frame_n} frames to {out_path}")


if __name__ == "__main__":
    main()

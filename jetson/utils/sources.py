"""Camera / video file source helpers."""
import os
import glob
import subprocess

from jetson.constants import VIDEO_TEST_DIR


def get_available_sources(current_device):
    """List available /dev/video* cameras and .mp4 files in video_test/.
    `current_device` is echoed back as the current source."""
    cameras = sorted([f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("video")])
    videos  = sorted(glob.glob(f"{VIDEO_TEST_DIR}/*.mp4"))
    return {"cameras": cameras, "videos": videos, "current": current_device}


def check_device(device):
    """Returns the device to use: the requested one if it exists, otherwise
    the first available /dev/video* device, or the original string if none
    exist (allows start-up without a camera plugged in)."""
    if os.path.exists(device):
        print(f"[OK]    Camera device {device} found.")
        return device
    available = sorted([f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("video")])
    print(f"[WARN]  Default camera {device} not found.")
    print(f"[INFO]  Available video devices: {available}")
    if available:
        print(f"[INFO]  Falling back to {available[0]}")
        return available[0]
    print(f"[WARN]  No cameras found — start anyway; select a source from ground.")
    return device


def probe_cam_formats(device):
    """Probe a V4L2 camera for supported (resolution, framerate) combinations.
    Returns a list of 'WxH@FPS' strings, or an empty list on failure."""
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--list-formats-ext", "-d", device],
            stderr=subprocess.STDOUT, timeout=5,
        ).decode()
    except Exception as e:
        return [], str(e)

    formats = []
    cur_w = cur_h = 0
    for line in out.splitlines():
        line = line.strip()
        if "Size:" in line and "Discrete" in line:
            parts = line.split()
            res = parts[-1]   # e.g. "1280x800"
            w, h = res.split("x")
            cur_w, cur_h = int(w), int(h)
        elif "Interval:" in line and "(" in line:
            fps_str = line.split("(")[1].split("fps")[0].strip()
            fps = float(fps_str)
            entry = f"{cur_w}x{cur_h}@{fps:.0f}"
            if entry not in formats:
                formats.append(entry)
    return formats, ""

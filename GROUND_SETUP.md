# Ground Laptop Setup

## Date: 2026-04-08

---

## Python Environment

- The Ground Laptop runs **miniconda3**
- Active environment: `base`
- `python3` resolves to `/home/gaurav/miniconda3/bin/python3` (Python 3.13.5)
- System Python (`/usr/bin/python3`) is shadowed by conda — `sudo apt install python3-*` packages are NOT visible in the conda env

---

## PyQt

- **PyQt6** is installed via conda (`pyqt 6.9.1`)
- PyQt5 is NOT available in the conda env (only in system Python)

### PyQt6 vs PyQt5 differences

| PyQt5 | PyQt6 |
|-------|-------|
| `from PyQt5.QtWidgets import ...` | `from PyQt6.QtWidgets import ...` |
| `Qt.AlignCenter` | `Qt.AlignmentFlag.AlignCenter` |
| `app.exec_()` | `app.exec()` |

---

## GStreamer

- Conda installs GStreamer **1.26.3** which does NOT include all plugins
- System GStreamer plugins (libav, h264parse, etc.) live at `/usr/lib/x86_64-linux-gnu/gstreamer-1.0/`
- Must set `GST_PLUGIN_PATH` so conda GStreamer finds system plugins:

```bash
export GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0
```

Add to `~/.bashrc` to make permanent. `ground_station.py` sets this automatically via `os.environ.setdefault(...)`.

### Running the raw pipeline (for testing)

```bash
GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0 \
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.101:8554/stream \
  latency=0 drop-on-latency=true protocols=tcp ! \
  rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink sync=false
```

---

## Ground Station App (`ground_station.py`)

Run with:
```bash
QT_QPA_PLATFORM=xcb python3 ground_station.py
```

`QT_QPA_PLATFORM=xcb` forces X11 mode (required for `xvimagesink` window embedding on Wayland/mixed setups).

### Architecture

- **`xvimagesink`** renders directly into a Qt `QWidget` via `GstVideo.VideoOverlay.set_window_handle()`
  - This is the same sink `autovideosink` selects on X11 — identical to the raw gst-launch pipeline
  - Do NOT use `appsink` + `QLabel` for live video: copying frames to Python memory on every frame (30fps) causes GStreamer pipeline stalls

- **Watchdog** — a `QTimer` running every 500ms checks if a frame has arrived in the last 3s
  - Probe is placed on `videoconvert` src pad (before the sink), so it only writes a timestamp — zero memory allocation, zero Qt interaction
  - If no frame arrives for 3s, the pipeline is torn down and restarted after 500ms

- **Why NOT `appsink`** — pulling frames into Python, converting to `QImage`, and emitting Qt signals from the GStreamer thread adds per-frame overhead that eventually stalls the pipeline. `xvimagesink` has no such overhead.

### Key GStreamer pipeline

```
rtspsrc → rtph264depay → h264parse → avdec_h264 → videoconvert[probe] → xvimagesink
```

`rtspsrc` properties used:
- `protocols=tcp` — required over SIYI radio (UDP drops too many packets)
- `tcp-timeout=3000000` — give up on stalled TCP after 3s
- `do-rtsp-keep-alive=true` — sends RTSP keep-alive to prevent server timeout

`xvimagesink` properties used:
- `handle-events=false` — **critical**: prevents GStreamer from listening to X11 window events (close, hide, etc.). Without this, any window obscure/close event kills the pipeline with "Output window was closed" error

---

## Installing packages

Always use conda (not pip or apt) for the base environment:

```bash
conda install <package>
```

Or pip inside conda if not available:

```bash
pip install <package>
```

---

## Standard Operating Procedure (SOP) — Before Running Ground Station App

Follow this checklist every time before launching `ground_station.py`:

1. **Close the SIYI remote controller's video display**
   The SIYI remote controller screen and the ground station app share the same
   radio link bandwidth. Running both simultaneously saturates the link and causes
   the RTSP stream to freeze and disconnect at ~29 seconds.
   → Turn off / close the video display on the SIYI handheld before connecting.

2. **Verify the route to the Jetson is correct**
   ```bash
   ip route get 192.168.144.101
   # Must show: via 192.168.43.1 dev wlp4s0
   # If not: sudo ip route add 192.168.144.0/24 via 192.168.43.1 dev wlp4s0
   ```

3. **Verify stream.py is running on the Jetson**
   ```bash
   # From ground laptop
   nc -zv 192.168.144.101 8554
   # Should say: Connection to 192.168.144.101 8554 port [tcp/*] succeeded
   ```

4. **Launch the app**
   ```bash
   QT_QPA_PLATFORM=xcb python3 ground_station.py
   ```

---

## Test

A working PyQt6 test app is at `test_pyqt.py`:

```bash
python3 test_pyqt.py
```

# Stable2 — CSRT Tracker + Multi-threaded Pipeline

## Date: 2026-04-11

---

## What changed since Stable1

### New: CSRT Object Tracking

Click anywhere on the live video in the ground station to start tracking an object using the CSRT (Channel and Spatial Reliability Tracker) algorithm.

- **Click to track** — left-click on the video places a bounding box (default 20x20 px) and starts tracking
- **Click again** — re-initializes the tracker on the new target
- **Box size** — configurable from the ground station UI (Box field, 10-200 px, press Enter)
- **Tracking lost** — tracker automatically clears when the object is lost

### New: Multi-threaded Processing Architecture

Tracking runs in a **separate thread** from the capture/encode pipeline to prevent FPS drops.

```
Capture thread (30fps):   pull frame → submit_frame() → draw(bbox) → push to appsrc → encode
                                  │                          ▲
                                  │ frame copy               │ last known bbox
                                  ▼                          │
Tracker thread (variable):  CSRT update(frame) ─────────► updates _bbox
```

- Capture thread always runs at full 30fps — never waits for CSRT
- Tracker thread runs CSRT at whatever rate the CPU allows
- Bounding box is drawn at 30fps using the last known position

### New: Tracking Telemetry

- Jetson sends `status:tracking Xms` over WebSocket once per second while tracking is active
- Ground station displays tracking time in the health bar ("Track: tracking 45.2ms")
- Jetson terminal logs every 30th update: `[PROC] update #30 45.2ms bbox=(x,y,w,h)`

### Changed: RTSP Reconnect Fix

- **stream.py**: `on_client_closed` no longer nulls `_appsrc` — capture thread keeps pushing frames so the hardware encoder stays warm. Reconnecting clients get instant video without the 5-second nvv4l2h264enc re-initialization.
- **stream.py**: Capture thread detects `FLOW_FLUSHING` from `push-buffer` to handle actual pipeline teardown gracefully.
- **ground_station.py**: `GLib.MainLoop` created once at startup, no longer recreated on every reconnect (eliminates context race condition).

### Fixed: Click Registration Race Condition

- Tracker thread previously consumed `_pending_click` in the same lock acquisition as `_latest_frame`. If no frame was available at that moment (between captures or during CSRT update), the click was cleared and discarded — causing random missed clicks.
- Fix: `_pending_click` is only consumed when a frame is available to process it on. Clicks now survive until the next frame arrives.

### Changed: Clean Shutdown

- `os._exit(0)` after GLib loop quits — prevents GStreamer/GLib lingering callbacks from blocking Ctrl+C.

---

## Architecture

### Files

| File | Runs on | Purpose |
|------|---------|---------|
| `stream.py` | Jetson | RTSP server + WebSocket server + capture thread |
| `processor.py` | Jetson | CSRT tracker (own thread) + bbox drawing |
| `ground_station.py` | Ground Laptop | PyQt6 GUI + GStreamer RTSP client + WebSocket client |
| `csrt_demo.py` | Jetson (testing) | Standalone CSRT demo on local video files |

### Threads on Jetson

| Thread | Purpose | Rate |
|--------|---------|------|
| Main (GLib) | RTSP server event loop | event-driven |
| Capture | Camera → process → encode → RTSP | 30fps |
| Tracker | CSRT update on latest frame | CPU-dependent |
| WebSocket | Commands from ground + status push | event-driven |

### Data Flow

```
Camera (/dev/video4)
    │
    ▼  v4l2src → videoconvert → appsink (max-buffers=1, drop=true)
    │
Capture Thread
    │
    ├──► processor.submit_frame(frame)  ──► Tracker Thread: CSRT update
    │                                              │
    ├──► processor.draw(frame)  ◄──────────── _bbox (last known position)
    │
    ▼  appsrc (max-buffers=1, leaky-type=2)
    │
    ▼  videoconvert → videoflip → nvvidconv → nvv4l2h264enc (800kbps)
    │
    ▼  h264parse → rtph264pay → RTSP (TCP)
    │
    ▼  ──── SIYI Radio Link ────
    │
Ground Station
    │
    ▼  rtspsrc (latency=200, tcp, tcp-timeout=15s)
    │
    ▼  rtph264depay → h264parse → avdec_h264 → queue (leaky) → videoconvert → xvimagesink
```

### WebSocket Protocol

| Command | Direction | Description |
|---------|-----------|-------------|
| `click:nx,ny` | Ground → Jetson | Start tracking at normalized coords (0-1) |
| `boxsize:N` | Ground → Jetson | Set tracking box size (pixels) |
| `rotate:M` | Ground → Jetson | Set video rotation (0-3) |
| `status:tracking Xms` | Jetson → Ground | Tracking telemetry (1/sec) |

---

## Configuration

### Jetson (stream.py)

| Setting | Value | Notes |
|---------|-------|-------|
| IP | 192.168.144.102 | Static, set via nmcli |
| Camera | /dev/video4 | Intel RealSense D400 color |
| Resolution | 1280x800 @ 30fps | |
| Encoder | nvv4l2h264enc | Jetson hardware H.264 |
| Bitrate | 800 kbps | |
| Keyframe interval | 30 frames (1s) | |
| RTSP port | 8554 | |
| WebSocket port | 5001 | |
| Default box size | 20x20 px | |

### Ground Laptop (ground_station.py)

| Setting | Value | Notes |
|---------|-------|-------|
| RTSP URL | rtsp://192.168.144.102:8554/stream | TCP transport |
| WS URL | ws://192.168.144.102:5001 | Auto-reconnect |
| Jitter buffer | 200ms | rtspsrc latency |
| TCP timeout | 15s | Covers nvv4l2h264enc init |
| Watchdog | 8s | Reconnects if no frames |
| Display | xvimagesink | sync=false, handle-events=false |

---

## Dependencies

### Jetson

```bash
sudo apt install gir1.2-gst-rtsp-server-1.0
pip3 install websockets opencv-contrib-python
```

### Ground Laptop

```bash
conda install pyqt
pip3 install websocket-client
# GST_PLUGIN_PATH set automatically by ground_station.py
```

---

## Running

### Jetson

```bash
cd ~/tracker_app
python3 stream.py
```

### Ground Laptop

```bash
cd ~/tracker_app
QT_QPA_PLATFORM=xcb python3 ground_station.py
```

See `GROUND_SETUP.md` for the full pre-flight checklist (SIYI remote display, routing, etc.).

---

## Known Limitations

1. **CSRT speed** — not yet benchmarked on Jetson at 1280x800. If >33ms per update, tracker FPS will be lower than video FPS (video stream unaffected).
2. **PTS drift** — capture thread uses fixed 33ms PTS increments, not wall-clock time. Under normal operation this is fine, but under heavy CPU load it could cause minor timestamp drift.
3. **SIYI bandwidth** — close the SIYI remote controller's video display before running the ground station (documented in GROUND_SETUP.md and NETWORK_SETUP.md).
4. **Suspend-mode** — `suspend-mode=NONE` is set but the hardware encoder may still re-initialize on some reconnects. The `_appsrc` persistence fix minimizes this.

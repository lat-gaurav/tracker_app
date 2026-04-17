# Tracker App — Documentation

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Project Structure](#3-project-structure)
4. [Configuration](#4-configuration)
5. [Tracker Engine](#5-tracker-engine)
6. [Frame Synchronisation](#6-frame-synchronisation)
7. [Pause-and-Select with Catch-Up](#7-pause-and-select-with-catch-up)
8. [Detection Pipeline](#8-detection-pipeline)
9. [AI Assist Mechanisms](#9-ai-assist-mechanisms)
10. [Ground Station GUI](#10-ground-station-gui)
11. [WebSocket Protocol](#11-websocket-protocol)
12. [Recording](#12-recording)
13. [VISCA Camera Control](#13-visca-camera-control)

---

## 1. Overview

A real-time target tracking system running on a Jetson Orin platform. A camera
feed is captured, processed (tracker + YOLO detector), and streamed via RTSP to
a remote PyQt6 ground station. The operator selects targets to track by
clicking or dragging on the video. All control flows over a WebSocket channel.

**Key components:**

| Component | File | Runs on |
|-----------|------|---------|
| Stream server | `stream.py` | Jetson |
| Frame processor | `processor.py` | Jetson |
| Ground station GUI | `ground_station.py` | Laptop/desktop |
| Camera control | `visca.py` | Jetson (serial to camera) |
| Config | `config/default.yaml` | Jetson |

---

## 2. Architecture

### 2.1 Threading Model

```
Jetson (stream.py + processor.py)
 ┌────────────────────────────────────────────────────┐
 │                                                    │
 │  Capture Thread          Tracker Thread            │
 │  (capture_and_push)      (_tracker_loop)           │
 │  ┌──────────┐            ┌──────────┐              │
 │  │ Read cam │─submit──>  │ CSRT/KCF │              │
 │  │ Fit 1080p│  frame     │ update() │              │
 │  │ Draw ovly│            │ Kalman   │              │
 │  │ Push RTSP│            │ Jump det │              │
 │  │ Record   │            └──────────┘              │
 │  └──────────┘                                      │
 │                          Detector Thread           │
 │                          (_detector_loop)          │
 │                          ┌──────────┐              │
 │                          │ YOLO OBB │              │
 │                          │ predict  │              │
 │                          └──────────┘              │
 │                                                    │
 │  WebSocket Server (async, port 5001)               │
 │  ┌──────────────────────────────┐                  │
 │  │ Commands ←→ Status updates   │                  │
 │  └──────────────────────────────┘                  │
 │                                                    │
 │  GStreamer RTSP Server (port 8554)                 │
 │  ┌──────────────────────────────┐                  │
 │  │ H.264 stream at 1920×1080   │                  │
 │  └──────────────────────────────┘                  │
 └────────────────────────────────────────────────────┘
            │ RTSP              │ WebSocket
            ▼                   ▼
 ┌────────────────────────────────────────────────────┐
 │  Ground Station (ground_station.py)                │
 │  ┌──────────────┐  ┌──────────────┐               │
 │  │ GStreamer     │  │ WSClient     │               │
 │  │ RTSP player  │  │ send/recv    │               │
 │  │ → xvimagesink│  │ commands     │               │
 │  └──────────────┘  └──────────────┘               │
 │  ┌──────────────────────────────────┐              │
 │  │ PyQt6 UI: video + config panel   │              │
 │  │ Click/drag → target selection    │              │
 │  │ Pause → catch-up → resume        │              │
 │  └──────────────────────────────────┘              │
 └────────────────────────────────────────────────────┘
```

### 2.2 Data Flow Per Frame

```
Camera → GStreamer appsink → _fit_to_canvas(1920×1080)
  → submit_frame(frame)
     ├─ _frame_ring[seq] = frame       (ring buffer, 300 frames / 10s)
     ├─ _tracker_frame = frame          (for tracker thread)
     └─ _detector_frame = frame         (for detector thread, if enabled)
  → get_display_state()                 (pause / catch-up / live)
  → processor.draw(frame)              (overlay bbox + detections)
  → GStreamer appsrc → RTSP H.264 → ground station
```

### 2.3 Shared State Protection

All shared state in `FrameProcessor` is protected by a single
`threading.Lock`. Each thread acquires the lock briefly to read/write its
slot, then processes outside the lock. No thread holds the lock during
expensive operations (CSRT update, YOLO inference).

---

## 3. Project Structure

```
tracker_app/
├── stream.py              # Jetson server: capture, RTSP, WebSocket, recording
├── processor.py           # FrameProcessor: tracker + detector threads, config
├── ground_station.py      # PyQt6 ground station GUI
├── visca.py               # Sony VISCA serial camera control
├── config/
│   └── default.yaml       # Persisted configuration
├── models/                # YOLO .pt / .engine model files
├── recordings/            # Recording output (video + overlay JSONL)
└── video_test/            # Sample video files for testing
```

---

## 4. Configuration

### 4.1 Config System

Configuration is managed via `config/default.yaml` with a Python dict
(`DEFAULT_CONFIG`) as fallback. The `load_config()` function deep-merges the
YAML over the defaults.

### 4.2 Config Keys

```yaml
tracker:
  type: csrt-fast           # tracker engine (see §5)
  box_w_default: 20         # default init box width (pixels)
  box_h_default: 20         # default init box height
  box_min: 10               # minimum box size
  box_max: 400              # maximum box size
  acq_assist:
    enabled: false           # corner-cluster refinement on init
    margin: 0.30
  ai_assist:
    enabled: false           # periodic YOLO snap during tracking
    interval: 30             # frames between snaps
    iou_min: 0.10            # minimum IoU to accept snap
    conf_min: 0.25           # low-conf threshold for assist detections
  ai_acquisition:
    enabled: false           # snap to nearest YOLO detection on click
    near_val: 150            # max distance in pixels
  ai_box_size:
    enabled: false           # auto-size box from median detection
    class: vehicle           # "vehicle" or "person"

camera:
  width: 0                  # 0 = auto-negotiate
  height: 0
  fps: 0

detection:
  enabled_on_start: false
  conf_thresh: 0.45
  top_n: 5
  vehicle_names: [car, truck, bus, ...]
  person_names: [person, people, pedestrian]

jump_detector:
  enabled: true
  dist_thresh: 0.35         # centre movement / predicted diagonal
  size_thresh: 1.5          # area ratio threshold
  iou_thresh: 0.35          # overlap threshold

kalman:
  enabled: true
  process_noise: 0.01
  measure_noise: 0.1
```

### 4.3 Live Parameter Updates

Parameters can be changed at runtime via WebSocket (`set_param:path=value`).
Changes to `tracker.type` trigger a live reinit of the active tracker.
Changes to box defaults trigger a live resize. All changes can be persisted
with `save_default`.

---

## 5. Tracker Engine

### 5.1 Available Types

| Type | Algorithm | Update Time | Notes |
|------|-----------|-------------|-------|
| `csrt` | Full CSRT | ~23ms | Best accuracy, all features |
| `csrt-fast` | CSRT, reduced features | ~5ms | **Recommended**. No color names, no segmentation, template=100 |
| `csrt-faster` | CSRT, HOG only | ~3ms | Good for catch-up |
| `csrt-ultra` | CSRT, minimal | ~1ms | Basic accuracy |
| `kcf` | Kernelized Correlation Filter | ~7ms | No tuning knobs |
| `mosse` | Min Output Sum of Sq. Error | ~0.1ms | Fastest, drifts easily |

### 5.2 CSRT Parameter Presets

The `_csrt_params(preset)` function tunes `cv2.TrackerCSRT_Params`:

| Parameter | default | fast | faster | ultra |
|-----------|---------|------|--------|-------|
| template_size | 200 | 100 | 80 | 50 |
| use_color_names | True | False | False | False |
| use_segmentation | True | False | False | False |
| use_gray | True | True | False | False |
| number_of_scales | 33 | 17 | 9 | 5 |
| admm_iterations | 4 | 2 | 2 | 1 |
| num_hog_channels_used | 18 | 18 | 9 | 4 |

### 5.3 Tracker Lifecycle

```
User clicks on video
  → set_click(ox, oy) or set_drag(nx, ny, nw, nh)
  → _tracker_loop picks up _pending_click
  → Click-to-frame lookup (ring buffer)
  → AI acquisition snap / Acq assist refine / Raw bbox
  → _create_tracker(type).init(frame, bbox)
  → _jump.init_tracker(bbox)
  → [If paused: iterative catch-up to live]
  → Normal tracking: tracker.update(frame) each frame
  → Jump detector validates each update
  → AI assist periodically snaps to YOLO detection
```

---

## 6. Frame Synchronisation

### 6.1 Frame Sequencing

Every frame gets a monotonic `frame_seq` in `submit_frame()`. A 300-frame
`OrderedDict` ring buffer (`_frame_ring`) stores recent frames keyed by seq.
Timestamps are stored in `_frame_ts`.

### 6.2 What Gets Tagged

| Data | Seq field | Meaning |
|------|-----------|---------|
| Tracker bbox | `_bbox_seq` | Which frame produced this bbox |
| Detections | `_det_seq` | Which frame YOLO ran on |
| Pending click | `_pending_click_seq` | Frame the user was looking at |
| Tracker frame | `_tracker_frame_seq` | Seq of frame given to tracker thread |
| Detector frame | `_detector_frame_seq` | Seq of frame given to detector thread |

### 6.3 Age-Gating

AI Assist and AI Acquisition check `current_seq - det_seq`. If detections
are older than `MAX_DET_AGE` (4 frames), they are rejected to prevent
snapping to stale positions on moving targets.

### 6.4 Kalman Multi-Step Prediction

When frames are skipped (tracker slower than camera), `SORTKalman.predict(steps=N)`
applies the transition matrix N times and accumulates process noise correctly:

```
Q_total = Σ_{k=0}^{N-1} F^k Q (F^k)^T
```

This prevents the jump detector from false-triggering on fast-moving targets
when the tracker occasionally skips a frame.

---

## 7. Pause-and-Select with Catch-Up

### 7.1 User Flow

1. Press **Pause** → stream freezes, "PAUSED" overlay, camera keeps buffering
2. Click or drag on the frozen frame to select a target
3. Tracker initialises on the **exact frozen frame** (from ring buffer)
4. **Catch-up** replays every buffered frame to reach live
5. Display shows "CATCHING UP" with the tracker bbox moving at high speed
6. Once caught up → auto-resume → live tracking continues

### 7.2 Implementation

**Pause state:**
- `_paused_frame`: held copy of the frozen frame (survives ring buffer eviction)
- `_paused_bbox`, `_paused_detections`: frozen overlay snapshot
- Detector stops receiving new frames while paused

**Catch-up (`_catch_up_to_live`):**
- Snapshots all frames from `init_seq` to `live_seq` into a local list
  (prevents eviction during replay)
- Processes every frame (no skipping — csrt-fast at 5ms is 6.6× real-time)
- Updates `_catchup_frame` every 5th frame for display
- Runs jump detection on every frame
- Aborts if user sends a new click or clear
- Iterative passes (up to 5) to catch frames that arrived during replay

**Display during catch-up:**
- `get_display_state()` returns `(paused, catching_up, frozen_frame, catchup_frame)`
  atomically in one lock acquisition
- Capture loop shows catch-up frames with live bbox overlay

### 7.3 Performance with csrt-fast

```
10-second pause = 300 frames
Pass 1: 300 × 5ms = 1.5s  (45 new frames arrive)
Pass 2:  45 × 5ms = 0.2s  (7 new frames)
Pass 3:   7 × 5ms = 0.04s → gap ≤ 2 → done
Total: ~1.8 seconds
```

---

## 8. Detection Pipeline

### 8.1 YOLO OBB Detector

Runs in `_detector_loop` on its own thread. Uses Ultralytics YOLO with
oriented bounding box (OBB) support. Models are stored in `models/`.

### 8.2 Detection Flow

```
_detector_frame → YOLO predict → _parse_detections
  → display_dets (≥ conf_thresh, top-N per class, shown on screen)
  → assist_dets  (≥ lower conf_min, used by AI assist/acquisition)
```

### 8.3 Detection Stamping

Each detection result is stamped with `_det_seq` — the frame_seq of the
frame that was actually fed to YOLO. This enables age-gating in AI features.

---

## 9. AI Assist Mechanisms

### 9.1 AI Acquisition (Click-Time Snap)

On a fresh click, if enabled, snaps the init bbox to the nearest YOLO
detection within `near_val` pixels. Uses the wider `_detections_assist`
list. Age-gated by `MAX_DET_AGE`.

### 9.2 AI Track Assist (Periodic Snap)

Every `interval` tracker updates, checks if any YOLO detection overlaps
the tracker bbox by IoU ≥ `iou_min`. If so, reinitialises the tracker on
the detection bbox. Age-gated.

### 9.3 Acquisition Assist (Corner Refinement)

On init, expands the raw click bbox by `margin`, finds Shi-Tomasi corners
in the ROI, clusters them, and refines the bbox to the dominant corner
cluster. No YOLO dependency.

### 9.4 AI Box Size Estimation

Uses the median AABB size of recent YOLO detections (vehicle or person
class) to automatically set `box_w` and `box_h` for the next click.

---

## 10. Ground Station GUI

### 10.1 Layout

```
+------------------------------------------+------------------+
|                                          |                  |
|          Video (RTSP via GStreamer)       |   Config Tabs    |
|          Click to track                  |   ┌────────────┐ |
|          Drag to select bbox             |   │ Tracker    │ |
|                                          |   │ Detection  │ |
|                                          |   │ Camera     │ |
|                                          |   │ Advanced   │ |
|                                          |   └────────────┘ |
|                                          |   [Save Default] |
+------------------------------------------+------------------+
| ● 30fps  Health:100%  Uptime:05:23  Track:4.2ms  Det:35ms  |
+-------------------------------------------------------------+
| Source:[▼] Rotate  Box:[40] W×H  Stop  Pause  Detect  REC  |
+-------------------------------------------------------------+
```

### 10.2 Video Widget (ClickableVideoWidget)

Supports both point clicks and drag-to-select:
- **Click** (< 8px movement): sends `click:nx,ny` with normalised coords
- **Drag** (≥ 8px): draws green rubber-band rectangle, sends `drag:nx,ny,nw,nh`
- Handles rotation-aware coordinate mapping via `_video_rect()` / `_widget_to_norm()`

### 10.3 Pause Button

- Sends `pause` / `resume` WebSocket commands
- Button turns orange "Resume" when paused
- Auto-resets when Jetson sends `status:resumed` after catch-up

### 10.4 Tracker Type Dropdown

Top of Tracker config tab. Shows all 6 types with timing info.
Changes send `set_param:tracker.type=<value>` which triggers live reinit.

### 10.5 Config Sync

On WebSocket connect, ground station sends `list_sources`, `list_models`,
`get_config`. The Jetson responds with JSON payloads. All config widgets
are populated from the Jetson's actual state via `_apply_config_to_ui()`.

---

## 11. WebSocket Protocol

### 11.1 Commands (Ground Station → Jetson)

| Command | Description |
|---------|-------------|
| `click:nx,ny` | Target selection (normalised 0-1) |
| `drag:nx,ny,nw,nh` | Drag-to-select rectangle (normalised) |
| `clear_track` | Stop tracking |
| `pause` | Freeze display, start buffering |
| `resume` | Resume live view |
| `boxsize:w[,h]` | Set init box size |
| `detect:on\|off` | Toggle YOLO detector |
| `rotate:0-3` | Set video rotation |
| `source:path` | Switch camera/video source |
| `set_param:path=value` | Change any config parameter |
| `get_config` | Request full config JSON |
| `save_default` | Persist config to YAML |
| `list_sources` | List cameras + video files |
| `list_models` | List YOLO model files |
| `record:start\|stop` | Start/stop recording |
| `visca:command` | VISCA camera control |

### 11.2 Status Updates (Jetson → Ground Station, 1Hz)

| Message | Description |
|---------|-------------|
| `status:tracking Xms` | Tracker active, update time |
| `status:detection Xms objects=N` | Detector stats |
| `status:det_error msg` | Detector error |
| `status:lost reason` | Track lost with reason |
| `status:recording MM:SS frames=N` | Recording progress |
| `status:boxsize_auto W,H` | AI box size notification |
| `status:resumed` | Auto-resumed after catch-up |

### 11.3 One-Time Responses

| Message | Description |
|---------|-------------|
| `config:{json}` | Full config JSON |
| `models:{json}` | Available models + current |
| `sources:{json}` | Cameras + videos + current |
| `cam_formats:{json}` | Supported camera resolutions |

---

## 12. Recording

### 12.1 Format

Recordings are saved to `recordings/<timestamp>/`:
- `video.h264` — raw H.264 via ffmpeg (libx264 ultrafast)
- `overlay.jsonl` — per-frame metadata (one JSON line per frame)
- `config.yaml` — config snapshot at recording start
- `inputs.jsonl` — input command log

### 12.2 Overlay JSONL Fields

```json
{
  "ts": 1713000000.123,
  "frame": 1,
  "frame_seq": 4523,
  "tracker_bbox": [100, 200, 40, 40],
  "tracker_bbox_seq": 4521,
  "track_ms": 4.2,
  "ai_active": false,
  "det_seq": 4519,
  "detections": [{"label": "car", "conf": 0.87, "aabb": [100, 200, 60, 50]}]
}
```

The `frame_seq` / `tracker_bbox_seq` / `det_seq` fields allow post-processing
to determine exactly which data was computed from which frame.

### 12.3 Writer Thread

Recording uses a dedicated writer thread with a 60-frame queue to avoid
blocking the capture loop. Frames that can't be enqueued (queue full) are
dropped silently.

---

## 13. VISCA Camera Control

Sony FCB-EV9520L control via serial (RS-232). The `visca.py` module provides
methods for zoom, focus, exposure, white balance, image settings, and presets.
Commands are forwarded via WebSocket (`visca:command`) from the ground station
Camera tab.

Supported controls: zoom (direct/tele/wide), focus (auto/manual/one-push),
exposure (auto/shutter/iris/manual), white balance, stabiliser, WDR, defog,
noise reduction, day/night mode, flip/mirror, and 16 presets.

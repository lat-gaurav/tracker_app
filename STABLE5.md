# Stable5 — Tracker App Documentation

## Date: 2026-04-14

---

## 1. Overview

Real-time drone target tracking system running on a **Jetson Orin NX** (stream.py) with a **ground station GUI** (ground_station.py) on a laptop. The Jetson captures video from a camera or video file, runs CSRT tracking + YOLO OBB detection + AI assist features, encodes the annotated stream via hardware H.264, and serves it over RTSP. The ground station receives the stream, displays it, and sends commands over WebSocket.

---

## 2. System Architecture

```
Jetson Orin NX (192.168.144.102)              Ground Laptop
─────────────────────────────────             ─────────────

  Camera (/dev/video*)                        ground_station.py (PyQt6)
      │                                            │
      ▼                                            │
  Capture Thread (30fps)                           │
      │                                            │
      ├─► processor.submit_frame()                 │
      │       ├─► Tracker Thread (CSRT)            │
      │       └─► Detector Thread (YOLO OBB, GPU)  │
      │                                            │
      ├─► processor.draw() ◄── _bbox + _detections │
      │                                            │
      ▼                                            │
  _fit_to_canvas (1920x1080)                       │
      │                                            │
      ▼                                            │
  appsrc → nvv4l2h264enc (1.5Mbps)                 │
      │                                            │
      ▼                                            │
  RTSP Server (:8554)  ─── TCP ──► rtspsrc ──► xvimagesink
                                                   │
  WebSocket (:5001)    ◄───────── commands ────────┘
                       ────────── telemetry ───────►│
```

### Threads on Jetson

| Thread | Purpose | Rate |
|--------|---------|------|
| Main (GLib) | RTSP server event loop | event-driven |
| Capture | Camera/file → process → encode → RTSP | 30fps |
| Tracker | CSRT update on latest frame | CPU-dependent |
| Detector | YOLO OBB inference on GPU | GPU-dependent |
| WebSocket | Commands from ground + status push | event-driven |

---

## 3. Files

| File | Runs on | Purpose |
|------|---------|---------|
| `stream.py` | Jetson | RTSP server + WebSocket server + capture thread |
| `processor.py` | Jetson | CSRT tracker, YOLO detector, Kalman, jump detector, AI features, drawing |
| `ground_station.py` | Ground Laptop | PyQt6 GUI + GStreamer RTSP client + WebSocket client |
| `config/default.yaml` | Jetson | All tunable parameters (loaded at startup, saved via GUI) |
| `models/` | Jetson | YOLO weight files (.pt, .engine, .onnx) |
| `video_test/` | Jetson | Test video files (H.264 MP4) |
| `csrt_demo.py` | Jetson (testing) | Standalone CSRT demo on local video |
| `yolo_demo.py` | Jetson (testing) | Standalone YOLO OBB demo → output video |

---

## 4. Features

### 4.1 Video Streaming

- **Hardware H.264 encoding** via `nvv4l2h264enc` (Jetson NVENC)
- **1920x1080 canvas** — content letterboxed/pillarboxed to preserve aspect ratio
- **1.5 Mbps** bitrate, keyframe every 30 frames
- **RTSP over TCP** (required for SIYI radio link)
- **Auto-reconnect** with exponential backoff on ground station
- **Watchdog** — if no frames for 8s, auto-reconnect

### 4.2 Source Selection

- **Camera**: any V4L2 device (`/dev/video*`), auto-negotiates or user-selected format
- **Video files**: any MP4 in `video_test/` folder, played via `cv2.VideoCapture`, loops automatically
- **Hot-switching**: change source from ground station dropdown, capture pipeline restarts seamlessly
- **Camera format probe**: ground station can query supported resolutions/framerates via `v4l2-ctl`, select from dropdown

### 4.3 CSRT Object Tracking

- **Click to track** — left-click on video, bounding box placed at click position
- **Configurable box W/H** — square or independent W x H, adjustable from bottom bar or config tab
- **Multi-threaded** — tracker runs in its own thread, never blocks 30fps capture/encode pipeline
- **Live resize** — changing box W/H while tracking immediately reinitializes the tracker at the new size
- **Stop Track** button — clears the active tracker

### 4.4 YOLO OBB Detection

- **TensorRT FP16 engine** for best Jetson performance (falls back to .pt)
- **Oriented Bounding Boxes** — rotated polygons drawn on frame
- **Vehicle class** (red) and **Person class** (blue) — configurable class name lists
- **Two confidence thresholds**:
  - `detection.conf_thresh` — what gets drawn on screen (display list, capped by `top_n`)
  - `tracker.ai_assist.conf_min` — wider low-confidence set for AI features (uncapped)
- **Live model switch** — select a different model from dropdown, reloads without restart
- **Detection ON by default** — `detection.enabled_on_start` config option

### 4.5 Jump Detection

- **SORT-style 7-state Kalman filter** predicts bbox position + size
- State: `[cx, cy, scale, aspect_ratio, vcx, vcy, v_scale]`
- Every CSRT update is compared against prediction: distance ratio, size ratio, IoU
- Jump declared if: `dist > dist_thresh AND (size > size_thresh OR iou < iou_thresh)`
- If jump detected: tracker dropped, reason logged and sent to ground
- Configurable thresholds — live from GUI

### 4.6 Acquisition Assist

- On click, expands the ROI by `margin`, runs **Shi-Tomasi corner detection** (`cv2.goodFeaturesToTrack`)
- Filters outliers (> 2x median distance from click center)
- Fits a tight bbox around the dominant corner cluster
- Tracker initializes on the refined (more trackable) bbox instead of raw click
- Falls back to raw click if too few corners found

### 4.7 AI Box Size Estimation

- Uses **median** vehicle or person detection AABB sizes from the last **5 detection frames**
- On click, automatically sets box W/H to match typical detection size in the scene
- Updates the Default box W/H fields in the GUI via `status:boxsize_auto`
- Class-selectable: Vehicle or Person dropdown

### 4.8 AI Acquisition (Click-time Snap)

- On click, if a YOLO detection is within `near_val` pixels of the click center
- Tracker initializes on the **detection's AABB** instead of raw click bbox
- Uses the assist list (low-confidence detections), so even uncertain detections help
- Requires Detection: ON (ground GUI blocks enable if not)

### 4.9 AI Track Assist (Periodic Snap)

- Every N frames, computes IoU between tracker bbox and all YOLO detections (assist list)
- If max IoU >= threshold: reinitializes tracker on the detection's AABB
- Refreshes the CSRT appearance model, prevents drift
- **Cyan triangle indicator** drawn above bbox for ~0.5s after each snap
- Requires Detection: ON

### 4.10 Init Priority Order

When the user clicks on the video:

1. **AI Box Size** (if enabled) — adjusts W/H to median detection size
2. **AI Acquisition** (if enabled + detection in range) — replaces bbox with detection AABB
3. **Acquisition Assist** (if enabled) — refines bbox to corner cluster
4. **Raw click** — uses the configured W x H at the click position

---

## 5. Configuration (config/default.yaml)

All tunable parameters in one YAML file, organized by section:

| Section | Key params |
|---------|------------|
| `model` | `yolo_path` (path in `models/`) |
| `camera` | `width`, `height`, `fps` (0 = auto-negotiate) |
| `tracker` | `box_w_default`, `box_h_default`, `box_min`, `box_max` |
| `tracker.acq_assist` | `enabled`, `margin` |
| `tracker.ai_box_size` | `enabled`, `class` (vehicle/person) |
| `tracker.ai_acquisition` | `enabled`, `near_val` (pixels) |
| `tracker.ai_assist` | `enabled`, `interval`, `iou_min`, `conf_min` |
| `detection` | `enabled_on_start`, `conf_thresh`, `top_n`, class name lists |
| `jump_detector` | `enabled`, `dist_thresh`, `size_thresh`, `iou_thresh` |
| `kalman` | `enabled`, `process_noise`, `measure_noise` |

### Parameter effect timing

| Category | Takes effect |
|----------|-------------|
| Detection (conf, top_n, names) | Immediate (next inference) |
| Jump detector thresholds | Immediate (next CSRT update) |
| Tracker box defaults | Immediate (next click, + live resize if active) |
| Kalman noise params | Next tracker init (Stop Track + click) |
| Model path | Live reload (~3-5s engine load) |

### Save as Default

Click "Save as Default (Jetson)" in the GUI. Any focused text field auto-commits before the save command is sent. Writes to `config/default.yaml` on the Jetson.

---

## 6. Ground Station GUI

### Layout

- **Top left**: Live video (expanding, click to track)
- **Top right**: Config panel (tabbed: Tracker / Detection / Advanced)
- **Health bar**: FPS, Health %, Uptime, Reconnects, Track ms, Det ms
- **Bottom bar**: Source dropdown, Rotate, Box W/H, Stop Track, Detect ON/OFF, Connect

### Detection-required guard

AI Acquisition and AI Track Assist checkboxes are blocked with a warning dialog if Detection is OFF. Turning Detection OFF auto-disables both AI features.

---

## 7. WebSocket Protocol

### Commands (Ground to Jetson)

| Command | Description |
|---------|-------------|
| `click:nx,ny` | Start tracking at normalized coords (0-1) |
| `clear_track` | Stop the active tracker |
| `boxsize:N` or `boxsize:W,H` | Set tracker box size |
| `detect:on/off` | Toggle YOLO detection |
| `rotate:M` | Set video rotation (0-3) |
| `source:path` | Switch video source |
| `set_param:path=value` | Update any config parameter |
| `get_config` | Request full config JSON |
| `save_default` | Persist config to YAML |
| `list_sources` | List cameras + video files |
| `list_models` | List model files in `models/` |
| `list_cam_formats` | Probe camera supported formats |
| `cam_format:WxH@FPS` or `auto` | Set camera sensor mode |

### Telemetry (Jetson to Ground, 1/sec)

| Message | Description |
|---------|-------------|
| `status:tracking Xms` | Tracker update time |
| `status:detection Xms objects=N` | Detection inference time + count |
| `status:det_error msg` | YOLO load/predict error |
| `status:lost reason` | Tracker dropped (jump/CSRT fail) |
| `status:boxsize_auto W,H` | AI box size estimation result |

---

## 8. Dependencies

### Jetson

```bash
sudo apt install gir1.2-gst-rtsp-server-1.0 ffmpeg v4l-utils
pip3 install websockets opencv-contrib-python==4.8.1.78 numpy==1.26.4 pyyaml ultralytics
```

### Ground Laptop

```bash
conda install pyqt
pip3 install websocket-client
export GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0
```

---

## 9. Running

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

See `GROUND_SETUP.md` for SIYI pre-flight checklist and `NETWORK_SETUP.md` for routing.

---

## 10. Known Limitations

1. **Single tracker** — one target at a time (multi-track planned as Tier 4)
2. **CSRT removed in OpenCV 4.9+** — pinned to `opencv-contrib-python==4.8.1.78`
3. **SIYI bandwidth** — close the SIYI remote controller's video display before streaming
4. **AV1 videos** — re-encode to H.264 first (`ffmpeg -i in.mp4 -c:v libx264 -crf 18 out.mp4`)
5. **Fixed encoder resolution** — nvv4l2h264enc canvas locked at 1920x1080 (no mid-stream resize)
6. **Kalman params** — noise values only apply on next tracker init

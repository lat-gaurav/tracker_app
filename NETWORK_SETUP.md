# Network Setup & RTSP Streaming Documentation

## Date: 2026-04-08

---

## Network Diagram

```
                        (Office WiFi: 172.16.0.x)
                               │
                               │ SSH
                               │
┌──────────┐              ┌────────────┐
│  Camera  │              │  MacBook   │
│(RealSense│              │(for coding)│
│  D400)   │              └─────┬──────┘
└────┬─────┘                    │
     │ USB                      │ Ethernet cable
     │                          │ (SSH + Internet Sharing)
┌────┴─────┐                    │ (192.168.2.x subnet)
│  Jetson  │                    │
│  Orin    │                    │
│          │                    │
│ WiFi:  172.16.0.176           │
│ Eth:   192.168.144.101        │
└────┬─────┘                    │
     │ Ethernet                 │
     │ (192.168.144.x)          │
┌────┴──────────┐               │
│ SIYI Air      │               │
│ Module        │               │
│ 192.168.144.11│               │
└────┬──────────┘               │
     │                          │
     │ Radio Telemetry          │
     │                          │
┌────┴──────────┐               │
│ SIYI Ground   │               │
│ Remote        │               │
│ 192.168.144.12│               │
└────┬──────────┘               │
     │                          │
     │ WiFi Hotspot             │
     │ (192.168.43.x)           │
     │                          │
┌────┴──────────────────────────┴───┐
│         Ground Laptop             │
│                                   │
│  WiFi (wlp4s0): 192.168.43.194   │
│  Ethernet (eno1): 192.168.2.6    │
└───────────────────────────────────┘
```

---

## IP Address Summary

| Device              | Interface  | IP Address       | Subnet           |
|---------------------|------------|------------------|------------------|
| Jetson              | wlP1p1s0   | 172.16.0.176     | 172.16.0.0/23    |
| Jetson              | enP8p1s0   | 192.168.144.101  | 192.168.144.0/24 |
| SIYI Air Module     | -          | 192.168.144.11   | 192.168.144.0/24 |
| SIYI Ground Remote  | -          | 192.168.144.12   | 192.168.144.0/24 |
| SIYI Ground Hotspot | -          | 192.168.43.1     | 192.168.43.0/24  |
| Ground Laptop       | wlp4s0     | 192.168.43.194   | 192.168.43.0/24  |
| Ground Laptop       | eno1       | 192.168.2.6      | 192.168.2.0/24   |
| MacBook             | -          | 192.168.2.1      | 192.168.2.0/24   |
| Jetson (public)     | -          | 125.16.160.66    | -                |

---

## Camera Details

- **Model:** Intel RealSense D400 series (USB)
- **Video Devices:** /dev/video0 - /dev/video5
- **Color Camera:** /dev/video4 (YUYV 4:2:2)
- **Supported Resolutions:**
  - 424x240 (up to 90fps)
  - 480x270 (up to 90fps)
  - 640x360 (up to 90fps)
  - 640x480 (up to 60fps)
  - 848x480 (up to 60fps)
  - 1280x720 (up to 30fps)
  - 1280x800 (up to 30fps)

---

## RTSP Streaming

### Server (Jetson)

Run on the Jetson:

```bash
cd ~/tracker_app
python stream.py
```

Stream URL: `rtsp://192.168.144.101:8554/stream`

The server listens on all interfaces (0.0.0.0:8554).

### Client (Ground Laptop)

```bash
ffplay rtsp://192.168.144.101:8554/stream
```

or

```bash
vlc rtsp://192.168.144.101:8554/stream
```

---

## Problem & Fix: RTSP Stream Not Reachable from Ground Laptop

### Problem

The Ground Laptop has two network interfaces:
1. **WiFi (wlp4s0)** - connected to SIYI Ground Remote hotspot (192.168.43.x)
2. **Ethernet (eno1)** - connected to MacBook for SSH and internet sharing (192.168.2.x)

The MacBook internet sharing set the default gateway on `eno1`, causing ALL traffic
(including traffic to 192.168.144.101) to route through the MacBook. The MacBook has
no path to the Jetson's 192.168.144.x subnet, so the stream failed.

### Diagnosis

```bash
# This showed traffic going to wrong interface
ip route get 192.168.144.101
# Output: 192.168.144.101 via 192.168.2.1 dev eno1 src 192.168.2.6
#         (wrong — going to MacBook instead of WiFi/SIYI)
```

### Fix (Temporary - lost on reboot)

On the Ground Laptop:

```bash
sudo ip route add 192.168.144.0/24 via 192.168.43.1 dev wlp4s0
```

This tells the laptop: route all 192.168.144.x traffic through WiFi via the
SIYI Ground Remote gateway (192.168.43.1), which forwards it over the radio
link to the Jetson.

### Fix (Permanent - survives reboot)

On the Ground Laptop:

```bash
sudo tee /etc/NetworkManager/dispatcher.d/99-siyi-route << 'EOF'
#!/bin/bash
if [ "$1" = "wlp4s0" ] && [ "$2" = "up" ]; then
    ip route add 192.168.144.0/24 via 192.168.43.1 dev wlp4s0
fi
EOF
sudo chmod +x /etc/NetworkManager/dispatcher.d/99-siyi-route
```

This automatically adds the route whenever the WiFi interface connects.

### Verification

```bash
ip route get 192.168.144.101
# Should show: 192.168.144.101 via 192.168.43.1 dev wlp4s0 src 192.168.43.194
```

---

## Utility Scripts

### check_camera.py

Tests camera availability and captures test frames.

```bash
# Check all cameras
python check_camera.py

# Check specific device
python check_camera.py /dev/video4
```

### stream.py

RTSP server with error logging. Reports:
- Camera device existence at startup
- Client connect/disconnect events
- GStreamer pipeline errors, warnings, and state changes

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| stream.py says device not found | Camera unplugged or wrong /dev/videoX | Run `check_camera.py` to find correct device |
| Server starts but client can't connect | Routing issue on Ground Laptop | Check `ip route get 192.168.144.101` |
| "No route to host" | Missing or wrong route | Add route: `sudo ip route add 192.168.144.0/24 via 192.168.43.1 dev wlp4s0` |
| Stream plays but freezes | Bandwidth over SIYI link | Lower bitrate in stream.py (currently 2000kbps) |
| Port 8554 refused | stream.py not running | Start stream.py on Jetson |

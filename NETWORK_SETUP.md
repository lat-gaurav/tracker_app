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

## Problem & Fix: High Latency and Corrupted Stream on Ground Laptop

### Problem

When playing the RTSP stream on the Ground Laptop via `ffplay`, the video had
massive packet loss, H264 decoding errors ("concealing X DC, X AC, X MV errors"),
and high latency (2-5 seconds). The issues:

1. **Resolution too high**: 1280x800 at 2000kbps exceeded the SIYI radio link bandwidth.
2. **ffplay buffering**: ffplay always adds buffering even with `-fflags nobuffer`.
3. **UDP packet loss**: Default RTSP uses UDP for media transport, but the SIYI link drops UDP packets.
4. **UDP push doesn't work**: The SIYI link is one-directional (Ground Laptop → Jetson works, Jetson → Ground Laptop does NOT). So direct UDP streaming from Jetson to Ground Laptop is not possible.

### Diagnosis

```bash
# On Jetson — tried to ping Ground Laptop through SIYI
ping -c 3 192.168.43.194
# Result: Destination Net Unreachable (via both 192.168.144.11 and .12)
# Conclusion: SIYI only routes inbound (Ground Laptop → Jetson), not outbound
```

ffplay output showed constant errors:
```
[rtsp] RTP: missed 1 packets
[h264] concealing 640 DC, 640 AC, 640 MV errors in P frame
```

### Fix (Jetson - stream.py)

Lowered resolution and bitrate to fit SIYI bandwidth:

| Setting | Before | After |
|---------|--------|-------|
| Resolution | 1280x800 | 640x360 |
| Bitrate | 2000 kbps | 500 kbps |
| Keyframe interval | default | 30 frames (1s) |
| config-interval | -1 | 1 (SPS/PPS every keyframe) |

### Fix (Ground Laptop - use GStreamer client with TCP)

**Do NOT use ffplay** — use GStreamer with `protocols=tcp`:

```bash
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.101:8554/stream latency=0 drop-on-latency=true protocols=tcp ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink sync=false
```

Key flags:
- `protocols=tcp` — forces TCP transport, avoids UDP packet loss over SIYI link
- `latency=0` — no jitter buffer
- `drop-on-latency=true` — drops late packets instead of buffering
- `sync=false` — renders frames immediately

### What did NOT work

| Approach | Why it failed |
|----------|--------------|
| `ffplay -fflags nobuffer -flags low_delay` | Still buffers, massive H264 errors |
| `ffplay -rtsp_transport tcp` | Better but still high latency |
| UDP push from Jetson (`stream_udp.py`) | SIYI link doesn't route Jetson → Ground Laptop |
| RTSP over UDP (default) | Packet loss over SIYI radio link |

---

## Problem & Fix: RTSP Stream Freezes / EOS after ~29 Seconds

### Problem

The RTSP stream on the Ground Laptop froze and received EOS (End of Stream) after
consistently ~29 seconds. The same raw `gst-launch` pipeline also froze. Meanwhile,
the SIYI remote controller's video display worked flawlessly.

### Root Cause: SIYI Radio Bandwidth Saturation

The SIYI radio link is shared between two video streams when both devices are active:

```
Jetson
  ├── RTSP stream (stream.py) → 2000 kbps ──→ over SIYI radio → Ground Laptop
  └── SIYI air unit video     → ~2-4 Mbps ──→ over SIYI radio → SIYI Remote Controller
```

When both the SIYI remote controller's video display AND the ground laptop's RTSP
stream were running simultaneously, the combined bandwidth exceeded the SIYI radio
link's capacity. This caused TCP congestion, packet loss, and the SIYI NAT router
dropping the TCP session — appearing as a ~29 second freeze/EOS on the ground laptop.

The SIYI remote controller appeared "flawless" because its video is the SIYI system's
own native protocol which takes priority, while the RTSP TCP stream suffered.

### Fix

**Close the SIYI remote controller's video display before running the ground station app.**
Only one video consumer over the SIYI radio link at a time.

### Why TCP keepalive didn't help

We tried `sudo sysctl -w net.ipv4.tcp_keepalive_time=10` — this had no effect because
`rtspsrc` does not enable `SO_KEEPALIVE` on its socket, so OS-level keepalive settings
are ignored by GStreamer.

### Troubleshooting checklist for future freezes

1. Is the SIYI remote controller's video display open? → Close it
2. Is anything else streaming from the Jetson simultaneously? → Check `ss -tn | grep 8554` on Jetson
3. Is the bitrate too high? → Lower `bitrate=` in stream.py (currently at stream.py settings)

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
| Stream plays but freezes/artifacts | Bandwidth over SIYI link | Lower bitrate in stream.py (currently 500kbps) |
| Stream freezes/EOS at ~29s | SIYI radio bandwidth saturated by two video streams | Close SIYI remote controller's video display — only one video stream over the radio at a time |
| Port 8554 refused | stream.py not running | Start stream.py on Jetson |
| ffplay shows H264 concealing errors | UDP packet loss over SIYI | Use GStreamer client with `protocols=tcp` |
| gst-launch stuck at "SETUP stream 0" | UDP blocked by SIYI link | Add `protocols=tcp` to rtspsrc |
| High latency (2-5s) | Client buffering | Use GStreamer with `latency=0 drop-on-latency=true sync=false` |
| Jetson can't reach Ground Laptop | SIYI link is one-way only | Use pull model (RTSP), not push (UDP) |

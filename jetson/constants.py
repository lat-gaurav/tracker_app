"""Runtime constants for the Jetson side.

These are values that rarely change and are shared across modules.
User-editable runtime parameters live in config/default.yaml instead.
"""

# ---- Networking ----
WS_PORT  = 5001
RTSP_PORT = "8554"
RTSP_MOUNT = "/stream"
RTSP_IP   = "192.168.144.102"

# ---- Camera / streaming ----
DEFAULT_DEVICE = "/dev/video4"
STREAM_W       = 1920       # fixed streaming canvas (content centred + padded)
STREAM_H       = 1080
FPS            = 30

# ---- VISCA camera control ----
VISCA_PORT = "/dev/ttyACM0"
VISCA_BAUD = 9600

# ---- Paths ----
CONFIG_PATH = "config/default.yaml"
MODELS_DIR  = "models"
RECORDINGS_DIR = "recordings"
VIDEO_TEST_DIR = "video_test"

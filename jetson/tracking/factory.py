"""Tracker factory: CSRT presets + KCF + MOSSE.

CSRT preset benchmarks (Jetson Orin, 1920×1080):
  csrt         ~23ms   full accuracy (all features, template=200, 33 scales)
  csrt-fast    ~5ms    good accuracy (no color names / segmentation, template=100)
  csrt-faster  ~3ms    decent        (HOG only, template=80, 9 scales)
  csrt-ultra   ~1.3ms  basic         (4-ch HOG, template=50, 5 scales)
  kcf          ~7ms    good (different algorithm, no tuning)
  mosse        ~0.1ms  basic (simplest DCF, drifts easily)
"""
import cv2


def _csrt_params(preset="default"):
    p = cv2.TrackerCSRT_Params()
    if preset == "fast":
        p.template_size = 100
        p.use_color_names = False
        p.use_segmentation = False
        p.number_of_scales = 17
        p.admm_iterations = 2
    elif preset == "faster":
        p.template_size = 80
        p.use_color_names = False
        p.use_gray = False
        p.use_segmentation = False
        p.number_of_scales = 9
        p.admm_iterations = 2
        p.num_hog_channels_used = 9
    elif preset == "ultra":
        p.template_size = 50
        p.use_color_names = False
        p.use_gray = False
        p.use_segmentation = False
        p.use_channel_weights = False
        p.number_of_scales = 5
        p.admm_iterations = 1
        p.num_hog_channels_used = 4
    return p


TRACKER_TYPES = [
    "csrt", "csrt-fast", "csrt-faster", "csrt-ultra", "kcf", "mosse",
]


def create_tracker(tracker_type="csrt"):
    """Create an OpenCV tracker of the given type."""
    tracker_type = tracker_type.lower()
    if tracker_type.startswith("csrt"):
        preset = tracker_type.split("-", 1)[1] if "-" in tracker_type else "default"
        params = _csrt_params(preset)
        if hasattr(cv2, "TrackerCSRT_create"):
            return cv2.TrackerCSRT_create(params)
        if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create"):
            return cv2.legacy.TrackerCSRT_create(params)
        raise RuntimeError("CSRT not available")
    if tracker_type == "kcf":
        if hasattr(cv2, "TrackerKCF_create"):
            return cv2.TrackerKCF_create()
        if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerKCF_create"):
            return cv2.legacy.TrackerKCF_create()
        raise RuntimeError("KCF not available")
    if tracker_type == "mosse":
        if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerMOSSE_create"):
            return cv2.legacy.TrackerMOSSE_create()
        raise RuntimeError("MOSSE not available")
    raise ValueError(f"Unknown tracker type {tracker_type!r}. Choose from: {TRACKER_TYPES}")

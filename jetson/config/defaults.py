"""Built-in configuration defaults. YAML overrides these at runtime."""

DEFAULT_CONFIG = {
    "model": {
        "yolo_path": "models/yolov26nobbnew_merged_1024.engine",
    },
    "tracker": {
        "type": "csrt-fast",    # csrt, csrt-fast, csrt-faster, csrt-ultra, kcf, mosse
        "box_w_default": 20,
        "box_h_default": 20,
        "box_min": 10,
        "box_max": 400,
        "acq_assist": {
            "enabled": False,
            "margin": 0.30,
        },
        "ai_assist": {
            "enabled": False,
            "interval": 30,
            "iou_min": 0.10,
            "conf_min": 0.25,
        },
        "ai_acquisition": {
            "enabled": False,
            "near_val": 150,
        },
        "ai_box_size": {
            "enabled": False,
            "class": "vehicle",   # "vehicle" or "person"
        },
    },
    "camera": {
        "width": 0,     # 0 = auto-negotiate
        "height": 0,
        "fps": 0,
    },
    "detection": {
        "enabled_on_start": False,
        "conf_thresh": 0.45,
        "top_n": 5,
        "vehicle_names": ["car", "truck", "bus", "van", "motor", "motorcycle", "bicycle", "vehicle"],
        "person_names":  ["person", "people", "pedestrian"],
    },
    "jump_detector": {
        "enabled": True,
        "dist_thresh": 0.35,
        "size_thresh": 1.5,
        "iou_thresh":  0.35,
    },
    "kalman": {
        "enabled": True,
        "process_noise": 0.01,
        "measure_noise": 0.1,
    },
}

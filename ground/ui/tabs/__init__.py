"""Config tabs: tracker, detection, camera (VISCA), advanced."""
from ground.ui.tabs.tracker_tab import build_tracker_tab
from ground.ui.tabs.detection_tab import build_detection_tab, DetectionTab
from ground.ui.tabs.camera_tab import build_camera_tab
from ground.ui.tabs.advanced_tab import build_advanced_tab, AdvancedTab

__all__ = [
    "build_tracker_tab",
    "build_detection_tab", "DetectionTab",
    "build_camera_tab",
    "build_advanced_tab", "AdvancedTab",
]

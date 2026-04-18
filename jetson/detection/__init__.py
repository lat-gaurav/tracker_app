"""YOLO OBB detector: loader + result parser."""
from jetson.detection.yolo import load_yolo
from jetson.detection.parser import parse_detections

__all__ = ["load_yolo", "parse_detections"]

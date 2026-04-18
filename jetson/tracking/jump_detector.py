"""Jump detector: flags implausible tracker updates via Kalman prediction."""
import numpy as np

from jetson.tracking.kalman import SORTKalman


class JumpDetector:
    """Detects implausible bounding-box jumps.  Compares the CSRT update
    against a Kalman prediction; flags the frame if distance/size/IoU
    metrics exceed configured thresholds."""

    def __init__(self, config):
        self.cfg    = config
        self.kalman = None

    def reset(self):
        self.kalman = None

    def init_tracker(self, bbox):
        if self.cfg["kalman"]["enabled"]:
            self.kalman = SORTKalman(
                self.cfg["kalman"]["process_noise"],
                self.cfg["kalman"]["measure_noise"],
            )
            self.kalman.init(bbox)
        else:
            self.kalman = None

    def check(self, bbox, frames_skipped=1):
        """Returns (is_jump, metrics_dict_or_None).  Updates Kalman only if not a jump.
        `frames_skipped` lets the Kalman scale its prediction when the tracker
        ran behind (e.g. skipped camera frames)."""
        if self.kalman is None or not self.cfg["jump_detector"]["enabled"]:
            if self.kalman:
                self.kalman.update(bbox)
            return False, None
        pred = self.kalman.predict(steps=frames_skipped)
        if pred is None:
            self.kalman.update(bbox)
            return False, None
        pcx, pcy = pred[0] + pred[2] / 2.0, pred[1] + pred[3] / 2.0
        ncx, ncy = bbox[0] + bbox[2] / 2.0, bbox[1] + bbox[3] / 2.0
        dist = float(np.hypot(pcx - ncx, pcy - ncy))
        diag = max(float(np.hypot(pred[2], pred[3])), 1.0)
        dist_ratio = dist / diag
        pa = max(pred[2] * pred[3], 1.0)
        na = max(bbox[2] * bbox[3], 1.0)
        size_ratio = max(na / pa, pa / na)
        iou = self._iou(pred, bbox)
        dt = self.cfg["jump_detector"]["dist_thresh"]
        sz = self.cfg["jump_detector"]["size_thresh"]
        io = self.cfg["jump_detector"]["iou_thresh"]
        is_jump = (dist_ratio > dt) and (size_ratio > sz or iou < io)
        if not is_jump:
            self.kalman.update(bbox)
        return is_jump, {
            "dist_ratio": dist_ratio, "size_ratio": size_ratio, "iou": iou,
        }

    @staticmethod
    def _iou(a, b):
        ax1, ay1, aw, ah = a
        ax2, ay2 = ax1 + aw, ay1 + ah
        bx1, by1, bw, bh = b
        bx2, by2 = bx1 + bw, by1 + bh
        iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        ih = max(0.0, min(ay2, by2) - max(ay1, by1))
        inter = iw * ih
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0

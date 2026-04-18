"""SORT-style constant-velocity Kalman filter for 2D bounding boxes."""
import numpy as np


class SORTKalman:
    """
    7-state constant-velocity Kalman filter.
    State: [cx, cy, s, r, vcx, vcy, vs]
        cx, cy = box centre
        s      = area (w*h)
        r      = aspect ratio (w/h)  — assumed constant (no velocity)
        v*     = velocities
    """

    def __init__(self, process_noise=0.01, measure_noise=0.1):
        self.q = process_noise
        self.r = measure_noise
        self.initialized = False
        self.F = np.eye(7)
        self.F[0, 4] = 1.0
        self.F[1, 5] = 1.0
        self.F[2, 6] = 1.0
        self.H = np.zeros((4, 7))
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0
        self.H[2, 2] = 1.0
        self.H[3, 3] = 1.0

    def init(self, bbox):
        x, y, w, h = bbox
        cx = x + w / 2.0
        cy = y + h / 2.0
        s  = max(w * h, 1.0)
        r  = (w / h) if h > 0 else 1.0
        self.x = np.array([cx, cy, s, r, 0, 0, 0], dtype=np.float64)
        self.Q = np.eye(7) * self.q
        self.Q[4:, 4:] *= 10.0            # more noise on velocities
        self.R = np.eye(4) * self.r
        self.P = np.eye(7) * 10.0
        self.initialized = True

    def predict(self, steps=1):
        """Predict forward `steps` frames.  For steps > 1, accumulate process noise
        via the correct discrete-time formula Σ F^k Q (F^k)^T."""
        if not self.initialized:
            return None
        steps = max(1, int(steps))
        if steps == 1:
            self.x = self.F @ self.x
            self.P = self.F @ self.P @ self.F.T + self.Q
        else:
            F_n = np.linalg.matrix_power(self.F, steps)
            self.x = F_n @ self.x
            Q_acc = np.zeros_like(self.Q)
            F_k = np.eye(7)
            for _ in range(steps):
                Q_acc += F_k @ self.Q @ F_k.T
                F_k = F_k @ self.F
            self.P = F_n @ self.P @ F_n.T + Q_acc
        return self._state_to_bbox(self.x)

    def update(self, bbox):
        if not self.initialized:
            self.init(bbox)
            return
        x, y, w, h = bbox
        z = np.array([x + w / 2.0, y + h / 2.0, max(w * h, 1.0),
                      (w / h) if h > 0 else 1.0])
        y_resid = z - (self.H @ self.x)
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y_resid
        self.P = (np.eye(7) - K @ self.H) @ self.P

    @staticmethod
    def _state_to_bbox(state):
        cx, cy, s, r = float(state[0]), float(state[1]), float(state[2]), float(state[3])
        if s <= 0: s = 1.0
        if r <= 0: r = 1.0
        w = float(np.sqrt(max(s * r, 1.0)))
        h = s / w if w > 0 else 1.0
        return (cx - w / 2.0, cy - h / 2.0, w, h)

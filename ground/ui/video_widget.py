"""Clickable video widget with drag-to-select support.

xvimagesink renders the RTSP stream directly into this widget's X11
window.  The widget itself never sees pixel data — it only maps mouse
events to normalised video coordinates (accounting for the aspect-ratio-
preserved render rect and the current video rotation)."""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QPen, QColor
from PyQt6.QtWidgets import QWidget


class ClickableVideoWidget(QWidget):
    """Captures mouse clicks + drag rectangles, emits normalised coordinates."""

    DRAG_THRESHOLD = 8   # pixels; a move smaller than this is a click

    # Native video dimensions by rotation method (dims swap for 90°/270°)
    VIDEO_DIMS = {
        0: (1920, 1080),
        1: (1080, 1920),
        2: (1920, 1080),
        3: (1080, 1920),
    }

    def __init__(self, on_click, on_drag=None, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._on_drag  = on_drag
        self._rotate_method = 0
        self._drag_start = None
        self._drag_end   = None
        self._dragging   = False
        # xvimagesink needs its own native X11 window to receive events.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_rotation(self, method):
        self._rotate_method = method

    # ---- coordinate helpers ----

    def _video_rect(self):
        """(offset_x, offset_y, render_w, render_h) of the video inside
        this widget (aspect-ratio preserved, centred)."""
        vw, vh = self.VIDEO_DIMS[self._rotate_method]
        ww, wh = self.width(), self.height()
        scale = min(ww / vw, wh / vh)
        rw = vw * scale
        rh = vh * scale
        return (ww - rw) / 2, (wh - rh) / 2, rw, rh

    def _widget_to_norm(self, px, py):
        """Widget pixel → normalised video coords (0-1), or None if on
        the black bars."""
        ox, oy, rw, rh = self._video_rect()
        if px < ox or px > ox + rw or py < oy or py > oy + rh:
            return None
        return ((px - ox) / rw, (py - oy) / rh)

    # ---- mouse events ----

    def mousePressEvent(self, event):
        if event.button().name != 'LeftButton':
            return
        self._drag_start = event.position()
        self._drag_end = None
        self._dragging = False

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        self._drag_end = event.position()
        dx = abs(self._drag_end.x() - self._drag_start.x())
        dy = abs(self._drag_end.y() - self._drag_start.y())
        if dx > self.DRAG_THRESHOLD or dy > self.DRAG_THRESHOLD:
            self._dragging = True
        if self._dragging:
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button().name != 'LeftButton' or self._drag_start is None:
            return
        if self._dragging and self._drag_end is not None and self._on_drag:
            p1 = self._widget_to_norm(self._drag_start.x(), self._drag_start.y())
            p2 = self._widget_to_norm(self._drag_end.x(), self._drag_end.y())
            if p1 and p2:
                nx = min(p1[0], p2[0])
                ny = min(p1[1], p2[1])
                nw = abs(p2[0] - p1[0])
                nh = abs(p2[1] - p1[1])
                if nw > 0.005 and nh > 0.005:
                    self._on_drag(nx, ny, nw, nh)
        elif not self._dragging:
            pt = self._widget_to_norm(
                self._drag_start.x(), self._drag_start.y())
            if pt:
                self._on_click(pt[0], pt[1])
        self._drag_start = None
        self._drag_end = None
        self._dragging = False
        self.update()

    def paintEvent(self, event):
        """Rubber-band rectangle during a drag."""
        if (not self._dragging or self._drag_start is None
                or self._drag_end is None):
            return
        painter = QPainter(self)
        pen = QPen(QColor(0, 255, 0, 200), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(0, 255, 0, 40))
        x1, y1 = self._drag_start.x(), self._drag_start.y()
        x2, y2 = self._drag_end.x(), self._drag_end.y()
        painter.drawRect(int(min(x1, x2)), int(min(y1, y2)),
                         int(abs(x2 - x1)), int(abs(y2 - y1)))
        painter.end()

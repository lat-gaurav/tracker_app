"""NO SIGNAL page shown when the Jetson stream is offline."""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel


class OfflineScreen(QWidget):
    """Black page with blinking 'NO SIGNAL' title + attempt counter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: black;")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title = QLabel("NO SIGNAL")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setFont(QFont("Monospace", 28, QFont.Weight.Bold))
        self.title.setStyleSheet("color: #ff3333;")

        self.subtitle = QLabel("Jetson stream offline — retrying...")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setStyleSheet("color: #888888; font-size: 13px;")

        self.attempt_label = QLabel("")
        self.attempt_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attempt_label.setStyleSheet("color: #555555; font-size: 11px;")

        layout.addWidget(self.title)
        layout.addSpacing(8)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.attempt_label)

        self._blink_state = True

    def set_attempt(self, text):
        self.attempt_label.setText(text)

    def blink(self):
        """Toggle title colour between bright and dim red."""
        self._blink_state = not self._blink_state
        color = "#ff3333" if self._blink_state else "#661111"
        self.title.setStyleSheet(f"color: {color};")

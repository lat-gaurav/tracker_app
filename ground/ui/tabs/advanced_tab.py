"""Advanced tab: raw WebSocket command entry for debugging."""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QLabel


class AdvancedTab:
    def __init__(self, send_fn):
        """send_fn(command_str) — sends a raw WebSocket message."""
        self._send = send_fn
        self.widget = self._build()

    def _build(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        row = QHBoxLayout()
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText(
            "Raw WebSocket command (e.g. set_param:jump_detector.enabled=false)")
        self.cmd_input.returnPressed.connect(self._send_command)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(70)
        send_btn.clicked.connect(self._send_command)

        row.addWidget(self.cmd_input)
        row.addWidget(send_btn)
        v.addLayout(row)

        hint = QLabel("Commands: set_param:PATH=VALUE | get_config | save_default | "
                      "list_sources | source:PATH | rotate:N | click:NX,NY | "
                      "boxsize:N[,M] | detect:on|off | clear_track | pause | resume | "
                      "drag:NX,NY,NW,NH | visca:CMD")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888; font-size: 10px;")
        v.addWidget(hint)
        return w

    def _send_command(self):
        text = self.cmd_input.text().strip()
        if not text:
            return
        self._send(text)
        self.cmd_input.clear()


def build_advanced_tab(send_fn):
    """Returns (AdvancedTab, QWidget)."""
    tab = AdvancedTab(send_fn)
    return tab, tab.widget

"""Factory helpers that build parameter widgets wired to set_param callbacks.

Each builder registers the widget in a `param_widgets` dict keyed by the
dotted config path, so the main window can later populate them when
`config:` JSON arrives from the Jetson.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QLineEdit, QCheckBox, QMessageBox


def param_line_edit(param_widgets, set_param_fn, path, placeholder="", width=80):
    """QLineEdit whose editingFinished sends set_param:PATH=VALUE."""
    w = QLineEdit()
    w.setFixedWidth(width)
    w.setPlaceholderText(placeholder)
    w.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def commit(p=path, widget=w):
        t = widget.text().strip()
        if t:
            set_param_fn(p, t)
    w.editingFinished.connect(commit)
    param_widgets[path] = w
    return w


def param_check(param_widgets, set_param_fn, path, get_detect_on=None,
                text=""):
    """QCheckBox that sends true/false.  If the path is an AI feature
    (tracker.ai_assist.* / tracker.ai_acquisition.*) and detection is
    OFF, the toggle is blocked with a warning message box."""
    c = QCheckBox(text)
    needs_detect = (path.startswith("tracker.ai_assist.")
                    or path.startswith("tracker.ai_acquisition."))

    def on_toggle(on, p=path, widget=c):
        if on and needs_detect and get_detect_on is not None:
            if not get_detect_on():
                QMessageBox.warning(
                    widget, "Detection is OFF",
                    "AI features need YOLO detection running.\n\n"
                    "Turn ON the Detect button (bottom bar) first, then "
                    "enable this option.")
                widget.blockSignals(True)
                widget.setChecked(False)
                widget.blockSignals(False)
                return
        set_param_fn(p, "true" if on else "false")

    c.toggled.connect(on_toggle)
    param_widgets[path] = c
    return c


def section_header(text):
    """A small section header label with top border and bold text."""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "color: #cccccc; font-size: 11px; font-weight: bold; "
        "border-top: 1px solid #333; padding-top: 6px; margin-top: 4px;"
    )
    return lbl

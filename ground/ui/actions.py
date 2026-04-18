"""User-action handlers for the MainWindow.

Each function takes the MainWindow (referred to as `win`) and implements
one button/widget handler.  Keeping these out of main_window.py lets that
file focus on UI construction and signal wiring."""
import os

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QLineEdit


# ---- tracker controls ----

def stop_track(win):
    win.ws_client.send("clear_track")
    _status(win, "Sent: clear_track")
    win.health.track_label.setText("Track: --")
    win.health.track_label.setStyleSheet("color: gray; font-size: 11px;")


def toggle_detect(win):
    win.detect_on = not win.detect_on
    win.bottom.detect_btn.setText(f"Detect: {'ON' if win.detect_on else 'OFF'}")
    win.ws_client.send(f"detect:{'on' if win.detect_on else 'off'}")
    _status(win, f"Sent: detect:{'on' if win.detect_on else 'off'}")
    if not win.detect_on:
        win.health.det_label.setText("Det: --")
        win.health.det_label.setStyleSheet("color: gray; font-size: 11px;")
        for ai_path in ("tracker.ai_assist.enabled",
                        "tracker.ai_acquisition.enabled"):
            w = win.param_widgets.get(ai_path)
            if w is not None and w.isChecked():
                w.blockSignals(True); w.setChecked(False); w.blockSignals(False)
                set_param(win, ai_path, "false")


def toggle_pause(win):
    win.paused = not win.paused
    if win.paused:
        win.ws_client.send("pause")
        win.bottom.pause_btn.setText("Resume")
        win.bottom.pause_btn.setStyleSheet(
            "background-color: #cc6600; color: white; font-weight: bold;")
        win.ws_response_label.setText("PAUSED — click or drag on target")
        win.ws_response_label.setStyleSheet("color: #ff8800; font-size: 11px;")
    else:
        win.ws_client.send("resume")
        win.bottom.pause_btn.setText("Pause")
        win.bottom.pause_btn.setStyleSheet("")
        _status(win, "Resumed")


def toggle_record(win):
    win.recording = not win.recording
    if win.recording:
        win.ws_client.send("record:start")
        win.bottom.rec_btn.setStyleSheet(
            "background-color: #cc0000; color: white; font-weight: bold;")
        win.health.rec_label.setText("REC")
        win.health.rec_label.setStyleSheet(
            "color: #ff0000; font-weight: bold; font-size: 11px;")
    else:
        win.ws_client.send("record:stop")
        win.bottom.rec_btn.setStyleSheet("font-weight: bold;")
        win.health.rec_label.setText("")


# ---- box size ----

def on_box_wh_toggled(win, checked):
    win.bottom.box_input.setEnabled(not checked)
    win.bottom.box_w_input.setEnabled(checked)
    win.bottom.box_h_input.setEnabled(checked)
    if checked:
        try:
            s = int(win.bottom.box_input.text())
            win.bottom.box_w_input.setText(str(s))
            win.bottom.box_h_input.setText(str(s))
        except ValueError:
            pass
    send_box_size(win)


def send_box_size(win):
    tcfg = win.config.get("tracker", {})
    mn = int(tcfg.get("box_min", 10))
    mx = int(tcfg.get("box_max", 400))
    try:
        if win.bottom.box_wh_check.isChecked():
            w = max(mn, min(mx, int(win.bottom.box_w_input.text())))
            h = max(mn, min(mx, int(win.bottom.box_h_input.text())))
            win.bottom.box_w_input.setText(str(w))
            win.bottom.box_h_input.setText(str(h))
            win.ws_client.send(f"boxsize:{w},{h}")
            _status(win, f"Sent: boxsize:{w},{h}")
        else:
            size = max(mn, min(mx, int(win.bottom.box_input.text())))
            win.bottom.box_input.setText(str(size))
            win.ws_client.send(f"boxsize:{size}")
            _status(win, f"Sent: boxsize:{size}")
    except ValueError:
        bw = tcfg.get("box_w_default", 20)
        bh = tcfg.get("box_h_default", 20)
        win.bottom.box_input.setText(str(bw))
        win.bottom.box_w_input.setText(str(bw))
        win.bottom.box_h_input.setText(str(bh))


# ---- source / rotate / cam format ----

def send_rotate(win):
    win._rotate_index = (win._rotate_index + 1) % 4
    method = win._rotate_index
    win.ws_client.send(f"rotate:{method}")
    win.bottom.rotate_btn.setText(win._rotate_labels[method])
    win.video_widget.set_rotation(method)
    _status(win, f"Sent: rotate:{method}")


def on_source_changed(win, index):
    if index < 0:
        return
    path = win.bottom.source_combo.itemData(index)
    if path:
        win.ws_client.send(f"source:{path}")
        _status(win, f"Sent: source:{os.path.basename(path)}")


def on_cam_fmt_changed(win, index):
    if index < 0:
        return
    val = win.cam_fmt_combo.itemData(index)
    if val:
        win.ws_client.send(f"cam_format:{val}")
        _status(win, f"Sent: cam_format:{val}")


# ---- video click / drag ----

def on_video_click(win, nx, ny):
    win.ws_client.send(f"click:{nx:.4f},{ny:.4f}")
    tag = " (paused)" if win.paused else ""
    _status(win, f"Sent: click ({nx:.3f}, {ny:.3f}){tag}")


def on_video_drag(win, nx, ny, nw, nh):
    win.ws_client.send(f"drag:{nx:.4f},{ny:.4f},{nw:.4f},{nh:.4f}")
    tag = " (paused)" if win.paused else ""
    _status(win, f"Sent: drag ({nx:.3f},{ny:.3f} {nw:.3f}x{nh:.3f}){tag}")


# ---- config / misc ----

def set_param(win, path, value):
    win.ws_client.send(f"set_param:{path}={value}")
    _status(win, f"Sent: set_param:{path}={value}")


def save_default(win):
    fw = QApplication.focusWidget()
    if isinstance(fw, QLineEdit):
        fw.clearFocus()   # commit pending edit via editingFinished
    QTimer.singleShot(50, lambda: win.ws_client.send("save_default"))
    _status(win, "Sent: save_default")


def visca(win, cmd):
    win.ws_client.send(f"visca:{cmd}")
    _status(win, f"Sent: visca:{cmd}")


def send_raw(win, text):
    win.ws_client.send(text)
    _status(win, f"Sent: {text}")


# ---- helpers ----

def _status(win, text, color="#aaaaaa"):
    win.ws_response_label.setText(text)
    win.ws_response_label.setStyleSheet(f"color: {color}; font-size: 11px;")

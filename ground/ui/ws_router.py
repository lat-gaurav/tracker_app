"""Route incoming WebSocket messages to the right UI updater.

Each message family (`status:*`, `config:`, `models:`, etc.) is handled
by a small function that takes the MainWindow as a context.  Keeping
routing here keeps main_window.py focused on layout and high-level
behaviour.
"""
import json
import os


def route_message(win, msg):
    """Dispatch a single raw message string.  `win` is the MainWindow."""
    # VISCA replies
    if (msg.startswith("visca_reply:") or msg.startswith("VISCA OK:")
            or msg.startswith("VISCA Error:")):
        win.ws_response_label.setText(f"Camera: {msg}")
        color = "#00cc44" if "OK" in msg or "reply" in msg else "#ff3333"
        win.ws_response_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        return

    # Status messages
    if msg == "status:resumed":
        if win.paused:
            win.paused = False
            win.bottom.pause_btn.setChecked(False)
            win.bottom.pause_btn.setText("Pause")
            win.bottom.pause_btn.setStyleSheet("")
            win.ws_response_label.setText("Catch-up complete — live tracking resumed")
            win.ws_response_label.setStyleSheet("color: #00cc44; font-size: 11px;")
        return

    if msg.startswith("status:tracking "):
        info = msg[len("status:tracking "):]
        win.health.track_label.setText(f"Track: {info}")
        win.health.track_label.setStyleSheet("color: #00cc44; font-size: 11px;")
        return
    if msg.startswith("status:detection "):
        info = msg[len("status:detection "):]
        win.health.det_label.setText(f"Det: {info}")
        win.health.det_label.setStyleSheet("color: #00cc44; font-size: 11px;")
        return
    if msg.startswith("status:det_error "):
        err = msg[len("status:det_error "):]
        win.health.det_label.setText(f"Det ERR: {err[:80]}")
        win.health.det_label.setStyleSheet("color: #ff3333; font-size: 11px;")
        return
    if msg.startswith("status:recording "):
        info = msg[len("status:recording "):]
        win.health.rec_label.setText(f"REC {info}")
        win.health.rec_label.setStyleSheet(
            "color: #ff0000; font-weight: bold; font-size: 11px;")
        return
    if msg.startswith("status:lost "):
        reason = msg[len("status:lost "):]
        win.health.track_label.setText(f"Lost: {reason}")
        win.health.track_label.setStyleSheet("color: #ff8800; font-size: 11px;")
        return
    if msg.startswith("status:boxsize_auto "):
        _handle_boxsize_auto(win, msg)
        return

    # JSON payloads
    if msg.startswith("config:"):
        try:
            cfg = json.loads(msg[7:])
            win.apply_config(cfg)
        except Exception as e:
            print(f"[GS] config parse failed: {e}")
        return
    if msg.startswith("models:"):
        try:
            data = json.loads(msg[7:])
            win.detection_tab.set_models(
                data.get("models", []), data.get("current", ""))
        except Exception as e:
            print(f"[GS] models parse failed: {e}")
        return
    if msg.startswith("cam_formats:"):
        _handle_cam_formats(win, msg)
        return
    if msg.startswith("sources:"):
        _handle_sources(win, msg)
        return

    # Fallback — just show the text
    win.ws_response_label.setText(f"Jetson: {msg}")
    win.ws_response_label.setStyleSheet("color: #00cc44; font-size: 11px;")


# ---- helpers ----

def _handle_boxsize_auto(win, msg):
    try:
        parts = msg[len("status:boxsize_auto "):].split(",")
        aw, ah = parts[0].strip(), parts[1].strip()
        for path, val in [("tracker.box_w_default", aw),
                          ("tracker.box_h_default", ah)]:
            w = win.param_widgets.get(path)
            if w:
                w.blockSignals(True); w.setText(val); w.blockSignals(False)
        for field, val in [(win.bottom.box_input,   aw),
                           (win.bottom.box_w_input, aw),
                           (win.bottom.box_h_input, ah)]:
            field.blockSignals(True); field.setText(val); field.blockSignals(False)
        win.ws_response_label.setText(f"AI box size: {aw}x{ah}")
        win.ws_response_label.setStyleSheet("color: #00cccc; font-size: 11px;")
    except Exception:
        pass


def _handle_cam_formats(win, msg):
    try:
        data = json.loads(msg[12:])
        fmts = data.get("formats", [])
        err  = data.get("error", "")
        dev  = data.get("device", "")
        win.cam_fmt_combo.blockSignals(True)
        win.cam_fmt_combo.clear()
        win.cam_fmt_combo.addItem("Auto", "auto")
        cc = win.config.get("camera", {})
        cur_w = int(cc.get("width", 0))
        cur_h = int(cc.get("height", 0))
        cur_f = int(cc.get("fps", 0))
        cur_str = f"{cur_w}x{cur_h}@{cur_f}" if cur_w else "auto"
        select_idx = 0
        for i, fmt in enumerate(fmts):
            win.cam_fmt_combo.addItem(fmt, fmt)
            if fmt == cur_str:
                select_idx = i + 1   # +1 because Auto is index 0
        win.cam_fmt_combo.setCurrentIndex(select_idx)
        win.cam_fmt_combo.blockSignals(False)
        if err:
            win.ws_response_label.setText(f"Format probe error: {err}")
            win.ws_response_label.setStyleSheet("color: #ff3333; font-size: 11px;")
        else:
            win.ws_response_label.setText(f"Found {len(fmts)} formats for {dev}")
            win.ws_response_label.setStyleSheet("color: #00cc44; font-size: 11px;")
    except Exception as e:
        print(f"[GS] cam_formats parse failed: {e}")


def _handle_sources(win, msg):
    try:
        data = json.loads(msg[8:])
        current = data.get("current", "")
        win.bottom.source_combo.blockSignals(True)
        win.bottom.source_combo.clear()
        select_idx = 0
        for cam in data.get("cameras", []):
            win.bottom.source_combo.addItem(f"Camera: {cam}", cam)
            if cam == current:
                select_idx = win.bottom.source_combo.count() - 1
        for vid in data.get("videos", []):
            name = os.path.basename(vid)
            win.bottom.source_combo.addItem(f"Video: {name}", vid)
            if vid == current:
                select_idx = win.bottom.source_combo.count() - 1
        win.bottom.source_combo.setCurrentIndex(select_idx)
        win.bottom.source_combo.blockSignals(False)
    except Exception as e:
        print(f"[GS] sources parse failed: {e}")

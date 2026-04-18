"""Tracker config tab: engine selector, init box, AI features, jump detector, Kalman."""
from PyQt6.QtWidgets import QWidget, QFormLayout, QLabel, QComboBox

from ground.ui.param_factory import param_line_edit, param_check, section_header


def build_tracker_tab(param_widgets, set_param_fn, get_detect_on):
    """Build the tracker tab and return the top-level QWidget.
    Widgets that bind to config are registered in `param_widgets`."""
    w = QWidget()
    form = QFormLayout(w)
    form.setContentsMargins(10, 8, 10, 8)
    form.setSpacing(6)

    # ---- Tracker engine selector ----
    form.addRow(section_header("Tracker engine"))
    tracker_type_combo = QComboBox()
    tracker_type_combo.addItem("CSRT — best accuracy (~23ms)",     "csrt")
    tracker_type_combo.addItem("CSRT Fast — good accuracy (~5ms)", "csrt-fast")
    tracker_type_combo.addItem("CSRT Faster — decent (~3ms)",      "csrt-faster")
    tracker_type_combo.addItem("CSRT Ultra — basic (~1ms)",        "csrt-ultra")
    tracker_type_combo.addItem("KCF — balanced (~5ms)",            "kcf")
    tracker_type_combo.addItem("MOSSE — fastest (~1ms)",           "mosse")
    tracker_type_combo.currentIndexChanged.connect(
        lambda i: set_param_fn("tracker.type", tracker_type_combo.itemData(i))
        if i >= 0 else None)
    param_widgets["tracker.type"] = tracker_type_combo
    form.addRow("Type:", tracker_type_combo)
    _note(form, "Switches live. CSRT Fast is the best all-round choice.")

    # ---- Init box & assists ----
    form.addRow(section_header("Init box"))
    form.addRow("Default box W:",
                param_line_edit(param_widgets, set_param_fn, "tracker.box_w_default"))
    form.addRow("Default box H:",
                param_line_edit(param_widgets, set_param_fn, "tracker.box_h_default"))
    form.addRow("Min box size:",
                param_line_edit(param_widgets, set_param_fn, "tracker.box_min"))
    form.addRow("Max box size:",
                param_line_edit(param_widgets, set_param_fn, "tracker.box_max"))
    form.addRow("AI box size:",
                param_check(param_widgets, set_param_fn,
                            "tracker.ai_box_size.enabled", get_detect_on))

    ai_box_class = QComboBox()
    ai_box_class.addItem("Vehicle", "vehicle")
    ai_box_class.addItem("Person",  "person")
    ai_box_class.currentIndexChanged.connect(
        lambda i: set_param_fn("tracker.ai_box_size.class", ai_box_class.itemData(i))
        if i >= 0 else None)
    param_widgets["tracker.ai_box_size.class"] = ai_box_class
    form.addRow("Size from class:", ai_box_class)

    form.addRow("Acq. Assist:",
                param_check(param_widgets, set_param_fn,
                            "tracker.acq_assist.enabled", get_detect_on))
    form.addRow("Acq. margin:",
                param_line_edit(param_widgets, set_param_fn, "tracker.acq_assist.margin"))
    form.addRow("AI Acquisition:",
                param_check(param_widgets, set_param_fn,
                            "tracker.ai_acquisition.enabled", get_detect_on))
    form.addRow("Near distance (px):",
                param_line_edit(param_widgets, set_param_fn,
                                "tracker.ai_acquisition.near_val"))
    _note(form,
          "AI box size: auto W/H from median vehicle detection. "
          "Acq. Assist: refine to corner cluster. "
          "AI Acquisition: snap to nearest YOLO detection on click.")

    # ---- AI Track Assist ----
    form.addRow(section_header("AI Track Assist"))
    form.addRow("Enabled:",
                param_check(param_widgets, set_param_fn,
                            "tracker.ai_assist.enabled", get_detect_on))
    form.addRow("Interval (frames):",
                param_line_edit(param_widgets, set_param_fn, "tracker.ai_assist.interval"))
    form.addRow("Min IoU:",
                param_line_edit(param_widgets, set_param_fn, "tracker.ai_assist.iou_min"))
    form.addRow("Min conf (assist):",
                param_line_edit(param_widgets, set_param_fn, "tracker.ai_assist.conf_min"))
    _note(form,
          "Periodically snaps tracker to overlapping YOLO detection. "
          "'Min conf (assist)' is a LOWER threshold than the display "
          "one — low-confidence detections help assist without "
          "cluttering the screen.  ▽ on bbox = active.  Requires Detect: ON.")

    # ---- Jump detector ----
    form.addRow(section_header("Jump detector"))
    form.addRow("Enabled:",
                param_check(param_widgets, set_param_fn, "jump_detector.enabled"))
    form.addRow("Distance threshold:",
                param_line_edit(param_widgets, set_param_fn, "jump_detector.dist_thresh"))
    form.addRow("Size threshold:",
                param_line_edit(param_widgets, set_param_fn, "jump_detector.size_thresh"))
    form.addRow("IoU threshold:",
                param_line_edit(param_widgets, set_param_fn, "jump_detector.iou_thresh"))

    # ---- Kalman filter ----
    form.addRow(section_header("Kalman filter (SORT)"))
    form.addRow("Enabled:",
                param_check(param_widgets, set_param_fn, "kalman.enabled"))
    form.addRow("Process noise:",
                param_line_edit(param_widgets, set_param_fn, "kalman.process_noise"))
    form.addRow("Measure noise:",
                param_line_edit(param_widgets, set_param_fn, "kalman.measure_noise"))
    _note(form, "Applied on next Stop Track → click.")

    return w


def _note(form, text):
    lbl = QLabel(text)
    lbl.setStyleSheet("color: #888888; font-size: 10px;")
    lbl.setWordWrap(True)
    form.addRow("", lbl)

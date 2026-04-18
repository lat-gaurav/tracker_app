"""Populate UI widgets from the Jetson's config JSON."""
from PyQt6.QtWidgets import QCheckBox, QComboBox, QLineEdit


def apply_config_to_widgets(param_widgets, cfg):
    """Update every registered widget in `param_widgets` from the cfg dict.
    Signals are blocked so widget changes don't echo back as set_param calls."""
    for path, widget in param_widgets.items():
        try:
            val = cfg
            for k in path.split("."):
                val = val[k]
        except (KeyError, TypeError):
            continue
        if isinstance(widget, QCheckBox):
            widget.blockSignals(True)
            widget.setChecked(bool(val))
            widget.blockSignals(False)
        elif isinstance(widget, QComboBox):
            widget.blockSignals(True)
            idx = widget.findData(val)
            if idx >= 0:
                widget.setCurrentIndex(idx)
            widget.blockSignals(False)
        elif isinstance(widget, QLineEdit):
            widget.blockSignals(True)
            if isinstance(val, list):
                widget.setText(",".join(str(x) for x in val))
            else:
                widget.setText(str(val))
            widget.blockSignals(False)


def sync_bottom_box_inputs(bw, bh, box_input, box_w_input, box_h_input, box_wh_check):
    """Sync the bottom-bar Box / W / H inputs to `(bw, bh)`.  If equal,
    show 'square' mode; otherwise enable the W×H mode."""
    if bw is None or bh is None:
        return
    box_input.blockSignals(True)
    box_w_input.blockSignals(True)
    box_h_input.blockSignals(True)
    if bw == bh:
        box_input.setText(str(bw))
        box_w_input.setText(str(bw))
        box_h_input.setText(str(bh))
        box_wh_check.blockSignals(True)
        box_wh_check.setChecked(False)
        box_wh_check.blockSignals(False)
        box_input.setEnabled(True)
        box_w_input.setEnabled(False)
        box_h_input.setEnabled(False)
    else:
        box_input.setText(str(bw))
        box_w_input.setText(str(bw))
        box_h_input.setText(str(bh))
        box_wh_check.blockSignals(True)
        box_wh_check.setChecked(True)
        box_wh_check.blockSignals(False)
        box_input.setEnabled(False)
        box_w_input.setEnabled(True)
        box_h_input.setEnabled(True)
    box_input.blockSignals(False)
    box_w_input.blockSignals(False)
    box_h_input.blockSignals(False)

"""Detection config tab: model selector + detection thresholds + class lists."""
import os

from PyQt6.QtWidgets import QWidget, QFormLayout, QLabel, QComboBox

from ground.ui.param_factory import param_line_edit


class DetectionTab:
    """Detection tab wrapper — exposes the model combo for external population."""

    def __init__(self, param_widgets, set_param_fn):
        self.model_combo = QComboBox()
        self.widget = self._build(param_widgets, set_param_fn)

    def _build(self, param_widgets, set_param_fn):
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(10, 8, 10, 8)
        form.setSpacing(6)

        self.model_combo.setMinimumWidth(240)
        self.model_combo.addItem("(waiting for list_models…)", "")

        def on_model_changed(index):
            if index < 0:
                return
            path = self.model_combo.itemData(index)
            if path:
                set_param_fn("model.yolo_path", path)
        self.model_combo.currentIndexChanged.connect(on_model_changed)

        form.addRow("Model:", self.model_combo)

        restart_hint = QLabel("(reloads live; ~3-5s engine load on first inference)")
        restart_hint.setStyleSheet("color: #888888; font-size: 10px;")
        form.addRow("", restart_hint)

        form.addRow("Confidence threshold:",
                    param_line_edit(param_widgets, set_param_fn, "detection.conf_thresh"))
        form.addRow("Top-N per class:",
                    param_line_edit(param_widgets, set_param_fn, "detection.top_n"))
        form.addRow("Vehicle class names:",
                    param_line_edit(param_widgets, set_param_fn,
                                    "detection.vehicle_names", "car,truck,…", width=240))
        form.addRow("Person class names:",
                    param_line_edit(param_widgets, set_param_fn,
                                    "detection.person_names", "person,pedestrian", width=240))
        return w

    # ---- public API for MainWindow to populate the model list ----

    def set_models(self, models, current):
        """Populate the model dropdown from a models:{...} WS reply."""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        select_idx = 0
        for i, m in enumerate(models):
            self.model_combo.addItem(os.path.basename(m), m)
            if m == current:
                select_idx = i
        if not models:
            self.model_combo.addItem("(no models found)", "")
        self.model_combo.setCurrentIndex(select_idx)
        self.model_combo.blockSignals(False)


def build_detection_tab(param_widgets, set_param_fn):
    """Build the detection tab; returns (DetectionTab instance, QWidget)."""
    tab = DetectionTab(param_widgets, set_param_fn)
    return tab, tab.widget

"""Bottom control bar: source, rotate, box size, stop/pause/detect/rec,
plus status labels and the main Connect button.

This builder returns both the layout and a ``Bindings`` object that
exposes the created widgets to the MainWindow so it can hook up
slots and display logic (e.g., set button styles when pause toggles).
"""
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton,
)


@dataclass
class BottomBarWidgets:
    status_label:    QLabel
    ws_status_label: QLabel
    source_combo:    QComboBox
    rotate_btn:      QPushButton
    connect_btn:     QPushButton
    box_input:       QLineEdit
    box_wh_check:    QCheckBox
    box_w_input:     QLineEdit
    box_h_input:     QLineEdit
    stop_track_btn:  QPushButton
    detect_btn:      QPushButton
    pause_btn:       QPushButton
    rec_btn:         QPushButton


def build_bottom_bar():
    """Returns (layout, BottomBarWidgets).  Slots are wired by the caller."""
    status_label = QLabel("Disconnected")
    status_label.setStyleSheet("color: gray; font-size: 12px;")
    ws_status_label = QLabel("WS: --")
    ws_status_label.setStyleSheet("color: gray; font-size: 11px;")

    source_combo = QComboBox()
    source_combo.setMinimumWidth(240)
    source_combo.addItem("Camera: /dev/video4", "/dev/video4")

    rotate_btn = QPushButton("Rotate: 0°")
    rotate_btn.setFixedWidth(110)

    connect_btn = QPushButton("Connect")
    connect_btn.setFixedWidth(110)

    box_input = QLineEdit("20")
    box_input.setFixedWidth(50)
    box_input.setAlignment(Qt.AlignmentFlag.AlignCenter)

    box_wh_check = QCheckBox("W×H")
    box_wh_check.setStyleSheet("font-size: 11px;")

    box_w_input = QLineEdit("20"); box_w_input.setFixedWidth(50)
    box_w_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
    box_w_input.setEnabled(False)
    box_h_input = QLineEdit("20"); box_h_input.setFixedWidth(50)
    box_h_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
    box_h_input.setEnabled(False)

    stop_track_btn = QPushButton("Stop Track")
    stop_track_btn.setFixedWidth(100)

    detect_btn = QPushButton("Detect: OFF")
    detect_btn.setFixedWidth(110)
    detect_btn.setCheckable(True)

    pause_btn = QPushButton("Pause")
    pause_btn.setFixedWidth(80)
    pause_btn.setCheckable(True)

    rec_btn = QPushButton("REC")
    rec_btn.setFixedWidth(60)
    rec_btn.setCheckable(True)
    rec_btn.setStyleSheet("font-weight: bold;")

    # ---- layout ----
    bar = QHBoxLayout()
    bar.addWidget(QLabel("Source:"))
    bar.addWidget(source_combo)
    bar.addSpacing(8)
    bar.addWidget(rotate_btn)
    bar.addSpacing(12)
    bar.addWidget(QLabel("Box:"))
    bar.addWidget(box_input)
    bar.addWidget(box_wh_check)
    bar.addWidget(QLabel("W")); bar.addWidget(box_w_input)
    bar.addWidget(QLabel("H")); bar.addWidget(box_h_input)
    bar.addSpacing(12)
    bar.addWidget(stop_track_btn);      bar.addSpacing(4)
    bar.addWidget(pause_btn);           bar.addSpacing(4)
    bar.addWidget(detect_btn);          bar.addSpacing(4)
    bar.addWidget(rec_btn)
    bar.addStretch()
    bar.addWidget(ws_status_label); bar.addSpacing(8)
    bar.addWidget(status_label);    bar.addSpacing(8)
    bar.addWidget(connect_btn)

    return bar, BottomBarWidgets(
        status_label=status_label,
        ws_status_label=ws_status_label,
        source_combo=source_combo,
        rotate_btn=rotate_btn,
        connect_btn=connect_btn,
        box_input=box_input,
        box_wh_check=box_wh_check,
        box_w_input=box_w_input,
        box_h_input=box_h_input,
        stop_track_btn=stop_track_btn,
        detect_btn=detect_btn,
        pause_btn=pause_btn,
        rec_btn=rec_btn,
    )

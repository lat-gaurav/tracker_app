"""Sony FCB-EV9520L VISCA camera control tab.

Every control sends `visca:<cmd>[:arg]` via the visca_fn callback.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QScrollArea, QFormLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QSlider, QLineEdit,
)

from ground.ui.param_factory import section_header


def build_camera_tab(visca_fn):
    """visca_fn(cmd_str) — sends `visca:<cmd_str>` over WebSocket."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    inner = QWidget()
    form = QFormLayout(inner)
    form.setContentsMargins(8, 6, 8, 6)
    form.setSpacing(4)

    _build_zoom(form, visca_fn)
    _build_focus(form, visca_fn)
    _build_exposure(form, visca_fn)
    _build_white_balance(form, visca_fn)
    _build_image(form, visca_fn)
    _build_day_night(form, visca_fn)
    _build_other(form, visca_fn)
    _build_presets(form, visca_fn)

    scroll.setWidget(inner)
    return scroll


# ---- section builders ----

def _build_zoom(form, visca):
    form.addRow(section_header("Zoom"))

    zs = QHBoxLayout()
    zs.addWidget(QLabel("W"))
    zoom_slider = QSlider(Qt.Orientation.Horizontal)
    zoom_slider.setRange(0, 16384)
    zoom_slider.setValue(0)
    zoom_lbl = QLabel("0"); zoom_lbl.setFixedWidth(40)
    zoom_slider.sliderReleased.connect(
        lambda: visca(f"zoom_direct:{zoom_slider.value()}"))
    zoom_slider.valueChanged.connect(lambda v: zoom_lbl.setText(str(v)))
    zs.addWidget(zoom_slider, 1)
    zs.addWidget(QLabel("T"))
    zs.addWidget(zoom_lbl)
    form.addRow(zs)

    zb = QHBoxLayout()
    b = QPushButton("W"); b.setFixedWidth(30)
    b.pressed.connect(lambda: visca("zoom_wide:5"))
    b.released.connect(lambda: visca("zoom_stop"))
    zb.addWidget(b)
    for label, pos in [("1x",0),("5x",3000),("10x",6000),
                       ("15x",9000),("20x",12000),("30x",16384)]:
        b = QPushButton(label); b.setFixedWidth(32)
        b.clicked.connect(lambda _, p=pos: (
            visca(f"zoom_direct:{p}"), zoom_slider.setValue(p)))
        zb.addWidget(b)
    b = QPushButton("T"); b.setFixedWidth(30)
    b.pressed.connect(lambda: visca("zoom_tele:5"))
    b.released.connect(lambda: visca("zoom_stop"))
    zb.addWidget(b)
    b = QPushButton("D-Zm"); b.setFixedWidth(40); b.setCheckable(True)
    b.toggled.connect(lambda on: visca("dzoom_on" if on else "dzoom_off"))
    zb.addWidget(b)
    form.addRow(zb)


def _build_focus(form, visca):
    form.addRow(section_header("Focus"))
    fr = QHBoxLayout()
    for lbl, cmd in [("AF","focus_auto"),("MF","focus_manual"),("1-Push","focus_one_push")]:
        b = QPushButton(lbl); b.setFixedWidth(50)
        b.clicked.connect(lambda _, c=cmd: visca(c))
        fr.addWidget(b)
    fr.addSpacing(8)
    b = QPushButton("Near"); b.setFixedWidth(42)
    b.pressed.connect(lambda: visca("focus_near:4"))
    b.released.connect(lambda: visca("focus_stop"))
    fr.addWidget(b)
    b = QPushButton("Far"); b.setFixedWidth(42)
    b.pressed.connect(lambda: visca("focus_far:4"))
    b.released.connect(lambda: visca("focus_stop"))
    fr.addWidget(b)
    fr.addStretch()
    form.addRow(fr)


def _build_exposure(form, visca):
    form.addRow(section_header("Exposure"))
    er = QHBoxLayout()
    for lbl, cmd in [("Auto","ae_auto"),("Shtr","ae_shutter"),
                     ("Iris","ae_iris"),("Man","ae_manual")]:
        b = QPushButton(lbl); b.setFixedWidth(42)
        b.clicked.connect(lambda _, c=cmd: visca(c))
        er.addWidget(b)
    er.addSpacing(6)
    b = QPushButton("BL+"); b.setFixedWidth(32)
    b.clicked.connect(lambda: visca("backlight_on"))
    er.addWidget(b)
    b = QPushButton("BL-"); b.setFixedWidth(32)
    b.clicked.connect(lambda: visca("backlight_off"))
    er.addWidget(b)
    er.addStretch()
    form.addRow(er)


def _build_white_balance(form, visca):
    form.addRow(section_header("White Balance"))
    wr = QHBoxLayout()
    for lbl, cmd in [("Auto","wb_auto"),("In","wb_indoor"),("Out","wb_outdoor"),
                     ("ATW","wb_atw"),("1P","wb_one_push")]:
        b = QPushButton(lbl); b.setFixedWidth(38)
        b.clicked.connect(lambda _, c=cmd: visca(c))
        wr.addWidget(b)
    wr.addStretch()
    form.addRow(wr)


def _build_image(form, visca):
    form.addRow(section_header("Image"))
    ig = QGridLayout(); ig.setSpacing(3)
    for lbl, cmd, r, c in [
        ("Stab ON","stabilizer_on",0,0), ("Stab OFF","stabilizer_off",0,1),
        ("WDR ON","wdr_on",0,2), ("WDR OFF","wdr_off",0,3),
        ("VE ON","ve_on",1,0), ("Defog Lo","defog_on:1",1,1),
        ("Defog Hi","defog_on:3",1,2), ("Defog OFF","defog_off",1,3),
        ("HiSens+","high_sens_on",2,0), ("HiSens-","high_sens_off",2,1),
    ]:
        b = QPushButton(lbl); b.setFixedHeight(24)
        b.clicked.connect(lambda _, cm=cmd: visca(cm))
        ig.addWidget(b, r, c)
    form.addRow(ig)

    sl = QHBoxLayout()
    sl.addWidget(QLabel("NR"))
    ns = QSlider(Qt.Orientation.Horizontal); ns.setRange(0, 5); ns.setValue(2)
    nl = QLabel("2"); nl.setFixedWidth(15)
    ns.valueChanged.connect(lambda v: (nl.setText(str(v)), visca(f"nr:{v}")))
    sl.addWidget(ns); sl.addWidget(nl)
    sl.addSpacing(8)
    sl.addWidget(QLabel("Apt"))
    ap = QSlider(Qt.Orientation.Horizontal); ap.setRange(0, 15); ap.setValue(8)
    al = QLabel("8"); al.setFixedWidth(15)
    ap.valueChanged.connect(lambda v: (al.setText(str(v)), visca(f"aperture:{v}")))
    sl.addWidget(ap); sl.addWidget(al)
    form.addRow(sl)


def _build_day_night(form, visca):
    form.addRow(section_header("Day / Night"))
    dr = QHBoxLayout()
    for lbl, cmd in [("Auto ICR","auto_icr_on"),("ICR OFF","auto_icr_off"),
                     ("Night","icr_on"),("Day","icr_off")]:
        b = QPushButton(lbl); b.setFixedWidth(58)
        b.clicked.connect(lambda _, c=cmd: visca(c))
        dr.addWidget(b)
    dr.addStretch()
    form.addRow(dr)


def _build_other(form, visca):
    form.addRow(section_header("Other"))
    og = QGridLayout(); og.setSpacing(3)
    for lbl, cmd, r, c in [
        ("Flip ON","flip_on",0,0), ("Flip OFF","flip_off",0,1),
        ("Mirror+","mirror_on",0,2), ("Mirror-","mirror_off",0,3),
        ("B&W","bw_on",1,0), ("Color","bw_off",1,1),
        ("Freeze","freeze_on",1,2), ("Unfreeze","freeze_off",1,3),
        ("Lens Init","lens_init",2,0), ("Reset","cam_reset",2,1),
    ]:
        b = QPushButton(lbl); b.setFixedHeight(24)
        b.clicked.connect(lambda _, cm=cmd: visca(cm))
        og.addWidget(b, r, c)
    form.addRow(og)


def _build_presets(form, visca):
    form.addRow(section_header("Presets"))
    pr = QHBoxLayout()
    preset_spin = QLineEdit("0")
    preset_spin.setFixedWidth(25)
    preset_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pr.addWidget(preset_spin)
    for lbl, pfx in [("Save","preset_save"),("Recall","preset_recall"),("Rst","preset_reset")]:
        b = QPushButton(lbl); b.setFixedWidth(48)
        b.clicked.connect(lambda _, cp=pfx: visca(
            f"{cp}:{int(preset_spin.text())&0xF}"))
        pr.addWidget(b)
    pr.addStretch()
    form.addRow(pr)

"""Dispatch VISCA sub-commands to the VISCACamera instance.

Parses 'action[:arg]' strings from the WebSocket protocol and invokes the
corresponding camera method.  Returns a reply string (always starts with
'VISCA OK:', 'VISCA Error:', or 'visca_reply:' for query results).
"""


def dispatch_visca(camera, cmd: str) -> str:
    """Execute a VISCA sub-command and return the reply string."""
    parts = cmd.split(":")
    action = parts[0]
    arg = int(parts[1]) if len(parts) > 1 else 0

    try:
        # -------- Zoom --------
        if action == "zoom_tele":       camera.zoom_tele(arg)
        elif action == "zoom_wide":     camera.zoom_wide(arg)
        elif action == "zoom_stop":     camera.zoom_stop()
        elif action == "zoom_direct":   camera.zoom_direct(arg)
        elif action == "zoom_pos":
            return f"visca_reply:zoom_pos={camera.zoom_position_inq()}"
        elif action == "dzoom_on":      camera.dzoom_on()
        elif action == "dzoom_off":     camera.dzoom_off()

        # -------- Focus --------
        elif action == "focus_auto":    camera.focus_auto()
        elif action == "focus_manual":  camera.focus_manual()
        elif action == "focus_far":     camera.focus_far(arg)
        elif action == "focus_near":    camera.focus_near(arg)
        elif action == "focus_stop":    camera.focus_stop()
        elif action == "focus_one_push":camera.focus_one_push()
        elif action == "focus_direct":  camera.focus_direct(arg)
        elif action == "focus_pos":
            return f"visca_reply:focus_pos={camera.focus_position_inq()}"

        # -------- Exposure --------
        elif action == "ae_auto":       camera.ae_full_auto()
        elif action == "ae_manual":     camera.ae_manual()
        elif action == "ae_shutter":    camera.ae_shutter_priority()
        elif action == "ae_iris":       camera.ae_iris_priority()
        elif action == "shutter":       camera.shutter_direct(arg)
        elif action == "iris":          camera.iris_direct(arg)
        elif action == "gain":          camera.gain_direct(arg)
        elif action == "exp_comp_on":   camera.exp_comp_on()
        elif action == "exp_comp_off":  camera.exp_comp_off()
        elif action == "exp_comp":      camera.exp_comp_direct(arg)
        elif action == "backlight_on":  camera.backlight_on()
        elif action == "backlight_off": camera.backlight_off()

        # -------- White balance --------
        elif action == "wb_auto":       camera.wb_auto()
        elif action == "wb_indoor":     camera.wb_indoor()
        elif action == "wb_outdoor":    camera.wb_outdoor()
        elif action == "wb_atw":        camera.wb_atw()
        elif action == "wb_manual":     camera.wb_manual()
        elif action == "wb_one_push":   camera.wb_one_push_trigger()
        elif action == "rgain":         camera.rgain_direct(arg)
        elif action == "bgain":         camera.bgain_direct(arg)

        # -------- Image processing --------
        elif action == "stabilizer_on":   camera.stabilizer_on()
        elif action == "stabilizer_off":  camera.stabilizer_off()
        elif action == "stabilizer_hold": camera.stabilizer_hold()
        elif action == "wdr_on":        camera.wdr_on()
        elif action == "wdr_off":       camera.wdr_off()
        elif action == "ve_on":         camera.ve_on()
        elif action == "defog_on":      camera.defog_on(arg)
        elif action == "defog_off":     camera.defog_off()
        elif action == "nr":            camera.nr_direct(arg)
        elif action == "aperture":      camera.aperture_direct(arg)
        elif action == "high_sens_on":  camera.high_sensitivity_on()
        elif action == "high_sens_off": camera.high_sensitivity_off()

        # -------- ICR (day/night) --------
        elif action == "icr_on":        camera.icr_on()
        elif action == "icr_off":       camera.icr_off()
        elif action == "auto_icr_on":   camera.auto_icr_on()
        elif action == "auto_icr_off":  camera.auto_icr_off()

        # -------- Other --------
        elif action == "flip_on":       camera.picture_flip_on()
        elif action == "flip_off":      camera.picture_flip_off()
        elif action == "mirror_on":     camera.lr_reverse_on()
        elif action == "mirror_off":    camera.lr_reverse_off()
        elif action == "freeze_on":     camera.freeze_on()
        elif action == "freeze_off":    camera.freeze_off()
        elif action == "bw_on":         camera.bw_on()
        elif action == "bw_off":        camera.bw_off()

        # -------- Presets --------
        elif action == "preset_save":   camera.memory_set(arg)
        elif action == "preset_recall": camera.memory_recall(arg)
        elif action == "preset_reset":  camera.memory_reset(arg)

        # -------- System --------
        elif action == "lens_init":     camera.lens_init()
        elif action == "cam_reset":     camera.camera_reset()
        elif action == "power_on":      camera.power_on()
        elif action == "power_off":     camera.power_off()

        else:
            return f"Error: unknown VISCA command: {action}"
        return f"VISCA OK: {action}"
    except Exception as e:
        return f"VISCA Error: {e}"

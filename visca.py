"""
VISCA serial driver for Sony FCB-EV9520L (and compatible cameras).

Thread-safe: all methods acquire a lock before sending on the serial port.
Commands return True on ACK+Completion, False on error.
Inquiries return the parsed value or None on error.
"""

import serial
import threading
import time


class VISCACamera:
    def __init__(self, port="/dev/ttyACM0", baud=9600, addr=1):
        self.addr = addr
        self._header = 0x80 | addr
        self._lock = threading.Lock()
        self._ser = None
        self._port = port
        self._baud = baud

    # ------------------------------------------------------------------ connection

    def open(self):
        self._ser = serial.Serial(self._port, self._baud, timeout=1)
        time.sleep(0.1)
        # Initialize: Address Set + IF_Clear
        self._raw_send(bytes([0x88, 0x30, 0x01, 0xFF]))
        time.sleep(0.3)
        self._ser.read(100)  # drain
        self._raw_send(bytes([0x88, 0x01, 0x00, 0x01, 0xFF]))
        time.sleep(0.3)
        self._ser.read(100)  # drain
        print(f"[VISCA] Opened {self._port} @ {self._baud} baud, camera addr={self.addr}")

    def close(self):
        if self._ser:
            self._ser.close()
            self._ser = None

    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    # ------------------------------------------------------------------ low-level

    def _raw_send(self, data):
        if self._ser:
            self._ser.write(data)

    def _cmd(self, *payload):
        """Send a VISCA command and wait for ACK + Completion.
        Returns True on success, False on error."""
        pkt = bytes([self._header]) + bytes(payload) + bytes([0xFF])
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(pkt)
            # Read ACK
            ack = self._read_packet()
            if ack is None or len(ack) < 2:
                return False
            if (ack[1] & 0x60) == 0x60:  # error
                return False
            # Read Completion
            comp = self._read_packet()
            if comp is None:
                return True  # some commands don't send completion
            if (comp[1] & 0x60) == 0x60:  # error
                return False
            return True

    def _inq(self, *payload):
        """Send a VISCA inquiry and return the payload bytes (without header/terminator).
        Returns bytes or None on error."""
        pkt = bytes([self._header]) + bytes(payload) + bytes([0xFF])
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(pkt)
            resp = self._read_packet()
            if resp is None or len(resp) < 3:
                return None
            if (resp[1] & 0x60) == 0x60:  # error
                return None
            return resp[2:]  # strip header + socket byte

    def _read_packet(self, timeout=1.0):
        """Read bytes until 0xFF terminator or timeout."""
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            b = self._ser.read(1)
            if not b:
                continue
            buf.append(b[0])
            if b[0] == 0xFF:
                return bytes(buf)
        return None if not buf else bytes(buf)

    # ------------------------------------------------------------------ Zoom

    def zoom_stop(self):
        return self._cmd(0x01, 0x04, 0x07, 0x00)

    def zoom_tele(self, speed=3):
        """Zoom in. speed: 0 (slow) to 7 (fast)."""
        return self._cmd(0x01, 0x04, 0x07, 0x20 | (speed & 0x07))

    def zoom_wide(self, speed=3):
        """Zoom out. speed: 0 (slow) to 7 (fast)."""
        return self._cmd(0x01, 0x04, 0x07, 0x30 | (speed & 0x07))

    def zoom_direct(self, position):
        """Set zoom to absolute position (0x0000=wide, 0x4000=tele)."""
        p, q, r, s = (position >> 12) & 0xF, (position >> 8) & 0xF, \
                      (position >> 4) & 0xF, position & 0xF
        return self._cmd(0x01, 0x04, 0x47, p, q, r, s)

    def zoom_position_inq(self):
        """Returns zoom position (0-16384) or None."""
        r = self._inq(0x09, 0x04, 0x47)
        if r and len(r) >= 4:
            return (r[0] << 12) | (r[1] << 8) | (r[2] << 4) | r[3]
        return None

    # ------------------------------------------------------------------ Digital Zoom

    def dzoom_on(self):
        return self._cmd(0x01, 0x04, 0x06, 0x02)

    def dzoom_off(self):
        return self._cmd(0x01, 0x04, 0x06, 0x03)

    # ------------------------------------------------------------------ Focus

    def focus_auto(self):
        return self._cmd(0x01, 0x04, 0x38, 0x02)

    def focus_manual(self):
        return self._cmd(0x01, 0x04, 0x38, 0x03)

    def focus_stop(self):
        return self._cmd(0x01, 0x04, 0x08, 0x00)

    def focus_far(self, speed=3):
        return self._cmd(0x01, 0x04, 0x08, 0x20 | (speed & 0x07))

    def focus_near(self, speed=3):
        return self._cmd(0x01, 0x04, 0x08, 0x30 | (speed & 0x07))

    def focus_one_push(self):
        return self._cmd(0x01, 0x04, 0x18, 0x01)

    def focus_direct(self, position):
        p, q, r, s = (position >> 12) & 0xF, (position >> 8) & 0xF, \
                      (position >> 4) & 0xF, position & 0xF
        return self._cmd(0x01, 0x04, 0x48, p, q, r, s)

    def focus_position_inq(self):
        r = self._inq(0x09, 0x04, 0x48)
        if r and len(r) >= 4:
            return (r[0] << 12) | (r[1] << 8) | (r[2] << 4) | r[3]
        return None

    def focus_af_mode(self, mode):
        """0=normal, 1=interval, 2=zoom_trigger"""
        modes = {0: 0x00, 1: 0x01, 2: 0x02}
        return self._cmd(0x01, 0x04, 0x57, modes.get(mode, 0x00))

    # ------------------------------------------------------------------ Exposure (AE)

    def ae_full_auto(self):
        return self._cmd(0x01, 0x04, 0x39, 0x00)

    def ae_manual(self):
        return self._cmd(0x01, 0x04, 0x39, 0x03)

    def ae_shutter_priority(self):
        return self._cmd(0x01, 0x04, 0x39, 0x0A)

    def ae_iris_priority(self):
        return self._cmd(0x01, 0x04, 0x39, 0x0B)

    def shutter_direct(self, val):
        """Set shutter position (0x00-0x15). See manual for speed mapping."""
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x4A, 0x00, 0x00, p, q)

    def iris_direct(self, val):
        """Set iris position (0x00=close to 0x11=F1.6)."""
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x4B, 0x00, 0x00, p, q)

    def gain_direct(self, val):
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x4C, 0x00, 0x00, p, q)

    def exp_comp_on(self):
        return self._cmd(0x01, 0x04, 0x3E, 0x02)

    def exp_comp_off(self):
        return self._cmd(0x01, 0x04, 0x3E, 0x03)

    def exp_comp_direct(self, val):
        """Exposure compensation (0x00=-10.5dB to 0x0E=+10.5dB, 0x07=0dB)."""
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x4E, 0x00, 0x00, p, q)

    def backlight_on(self):
        return self._cmd(0x01, 0x04, 0x33, 0x02)

    def backlight_off(self):
        return self._cmd(0x01, 0x04, 0x33, 0x03)

    # ------------------------------------------------------------------ White Balance

    def wb_auto(self):
        return self._cmd(0x01, 0x04, 0x35, 0x00)

    def wb_indoor(self):
        return self._cmd(0x01, 0x04, 0x35, 0x01)

    def wb_outdoor(self):
        return self._cmd(0x01, 0x04, 0x35, 0x02)

    def wb_one_push(self):
        return self._cmd(0x01, 0x04, 0x35, 0x03)

    def wb_atw(self):
        return self._cmd(0x01, 0x04, 0x35, 0x04)

    def wb_manual(self):
        return self._cmd(0x01, 0x04, 0x35, 0x05)

    def wb_one_push_trigger(self):
        return self._cmd(0x01, 0x04, 0x10, 0x05)

    def rgain_direct(self, val):
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x43, 0x00, 0x00, p, q)

    def bgain_direct(self, val):
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x44, 0x00, 0x00, p, q)

    # ------------------------------------------------------------------ Image processing

    def stabilizer_on(self):
        return self._cmd(0x01, 0x04, 0x34, 0x02)

    def stabilizer_off(self):
        return self._cmd(0x01, 0x04, 0x34, 0x03)

    def stabilizer_hold(self):
        return self._cmd(0x01, 0x04, 0x34, 0x00)

    def stabilizer_level(self, level):
        """2=Super, 3=Super+"""
        return self._cmd(0x01, 0x7E, 0x04, 0x34, level & 0x0F)

    def wdr_on(self):
        return self._cmd(0x01, 0x04, 0x3D, 0x02)

    def wdr_off(self):
        return self._cmd(0x01, 0x04, 0x3D, 0x03)

    def ve_on(self):
        return self._cmd(0x01, 0x04, 0x3D, 0x06)

    def defog_on(self, level=1):
        """level: 1=low, 2=mid, 3=high"""
        return self._cmd(0x01, 0x04, 0x37, 0x02, level & 0x03)

    def defog_off(self):
        return self._cmd(0x01, 0x04, 0x37, 0x03, 0x00)

    def nr_direct(self, level):
        """NR level: 0=off, 1-5=levels, 0x7F=2D/3D independent"""
        return self._cmd(0x01, 0x04, 0x53, level & 0x7F)

    def aperture_direct(self, val):
        """0x00=off to 0x0F=max edge enhancement"""
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x42, 0x00, 0x00, p, q)

    def high_sensitivity_on(self):
        return self._cmd(0x01, 0x04, 0x5E, 0x02)

    def high_sensitivity_off(self):
        return self._cmd(0x01, 0x04, 0x5E, 0x03)

    # ------------------------------------------------------------------ ICR (day/night)

    def icr_on(self):
        return self._cmd(0x01, 0x04, 0x01, 0x02)

    def icr_off(self):
        return self._cmd(0x01, 0x04, 0x01, 0x03)

    def auto_icr_on(self):
        return self._cmd(0x01, 0x04, 0x51, 0x02)

    def auto_icr_off(self):
        return self._cmd(0x01, 0x04, 0x51, 0x03)

    def auto_icr_threshold(self, val):
        p, q = (val >> 4) & 0xF, val & 0xF
        return self._cmd(0x01, 0x04, 0x21, 0x00, 0x00, p, q)

    # ------------------------------------------------------------------ Other

    def picture_flip_on(self):
        return self._cmd(0x01, 0x04, 0x66, 0x02)

    def picture_flip_off(self):
        return self._cmd(0x01, 0x04, 0x66, 0x03)

    def lr_reverse_on(self):
        return self._cmd(0x01, 0x04, 0x61, 0x02)

    def lr_reverse_off(self):
        return self._cmd(0x01, 0x04, 0x61, 0x03)

    def freeze_on(self):
        return self._cmd(0x01, 0x04, 0x62, 0x02)

    def freeze_off(self):
        return self._cmd(0x01, 0x04, 0x62, 0x03)

    def bw_on(self):
        return self._cmd(0x01, 0x04, 0x63, 0x04)

    def bw_off(self):
        return self._cmd(0x01, 0x04, 0x63, 0x00)

    # ------------------------------------------------------------------ Memory presets

    def memory_set(self, slot):
        """Save current state to preset slot (0-15)."""
        return self._cmd(0x01, 0x04, 0x3F, 0x01, slot & 0x0F)

    def memory_recall(self, slot):
        """Recall preset slot (0-15)."""
        return self._cmd(0x01, 0x04, 0x3F, 0x02, slot & 0x0F)

    def memory_reset(self, slot):
        return self._cmd(0x01, 0x04, 0x3F, 0x00, slot & 0x0F)

    # ------------------------------------------------------------------ Temperature

    def temp_inq(self):
        """Read internal temperature (hex value, ±3°C accuracy)."""
        r = self._inq(0x09, 0x04, 0x24)
        if r and len(r) >= 2:
            return (r[0] << 4) | r[1]
        return None

    # ------------------------------------------------------------------ Power

    def power_on(self):
        return self._cmd(0x01, 0x04, 0x00, 0x02)

    def power_off(self):
        return self._cmd(0x01, 0x04, 0x00, 0x03)

    # ------------------------------------------------------------------ Lens init

    def lens_init(self):
        return self._cmd(0x01, 0x04, 0x19, 0x01)

    def camera_reset(self):
        return self._cmd(0x01, 0x04, 0x19, 0x03)

"""Ground station entry point: sets up Qt + GStreamer and shows the main window."""
import os
import sys

os.environ.setdefault(
    'GST_PLUGIN_PATH',
    '/usr/lib/x86_64-linux-gnu/gstreamer-1.0'
)

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst

from PyQt6.QtWidgets import QApplication

from ground.ui.main_window import GroundStation


def main():
    Gst.init(None)
    app = QApplication(sys.argv)
    window = GroundStation()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

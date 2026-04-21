"""Entry point for the SteelVoiceMix GUI."""

from __future__ import annotations

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .main_window import MixerGUI
from .settings import APP_NAME


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("fusion")

    # Make Ctrl+C in the launching terminal quit cleanly. Python signal
    # handlers only run when the interpreter gets a chance between Qt events,
    # so nudge it every 250 ms.
    signal.signal(signal.SIGINT, lambda *_: QApplication.quit())
    signal.signal(signal.SIGTERM, lambda *_: QApplication.quit())
    interpreter_nudge = QTimer()
    interpreter_nudge.start(250)
    interpreter_nudge.timeout.connect(lambda: None)

    window = MixerGUI()
    window.show()

    sys.exit(app.exec())

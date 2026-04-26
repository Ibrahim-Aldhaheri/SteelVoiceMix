"""Entry point for the SteelVoiceMix GUI."""

from __future__ import annotations

import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .i18n import setup_translator
from .main_window import MixerGUI
from .settings import APP_NAME

# Per-user socket name — no path, QLocalServer places it under
# $XDG_RUNTIME_DIR (or /tmp) automatically. Including the UID keeps the
# name unique on multi-user systems.
_SERVER_NAME = f"{APP_NAME}-gui-{os.getuid()}"


def _signal_existing_instance() -> bool:
    """If another instance is already listening, tell it to raise its
    window and return True. Return False if no instance is running."""
    sock = QLocalSocket()
    sock.connectToServer(_SERVER_NAME)
    if not sock.waitForConnected(200):
        return False
    sock.write(b"show\n")
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _install_single_instance_server(window: MixerGUI) -> QLocalServer | None:
    """Claim the single-instance socket and raise the main window whenever
    another launcher tells us to. Returns the server (or None if claim fails)."""
    # Clear any stale socket left over from a crash. Safe because we've
    # already verified no live instance is listening (above).
    QLocalServer.removeServer(_SERVER_NAME)
    server = QLocalServer()
    if not server.listen(_SERVER_NAME):
        print(
            f"[steelvoicemix-gui] warning: single-instance server could not "
            f"claim '{_SERVER_NAME}': {server.errorString()}",
            file=sys.stderr,
        )
        return None

    def on_new_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def on_ready_read():
            payload = bytes(conn.readAll()).decode(errors="ignore")
            if "show" in payload:
                window._show_window()
            conn.disconnectFromServer()

        conn.readyRead.connect(on_ready_read)

    server.newConnection.connect(on_new_connection)
    return server


def main() -> None:
    # Short-circuit before spinning up Qt if another instance is already
    # running — we just need to ask it to raise its window.
    if _signal_existing_instance():
        return

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("fusion")
    # Keep a reference so the translator isn't GC'd; no-op if no .qm matches.
    app._translator = setup_translator(app)

    # Make Ctrl+C in the launching terminal quit cleanly. Python signal
    # handlers only run when the interpreter gets a chance between Qt events,
    # so nudge it every 250 ms.
    signal.signal(signal.SIGINT, lambda *_: QApplication.quit())
    signal.signal(signal.SIGTERM, lambda *_: QApplication.quit())
    interpreter_nudge = QTimer()
    interpreter_nudge.start(250)
    interpreter_nudge.timeout.connect(lambda: None)

    window = MixerGUI()
    # Keep a reference so the server isn't GC'd while the app runs.
    app._single_instance_server = _install_single_instance_server(window)
    window.show()

    sys.exit(app.exec())

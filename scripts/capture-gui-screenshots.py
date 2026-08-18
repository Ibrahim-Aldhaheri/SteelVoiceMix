#!/usr/bin/env python3
"""Rebuild README screenshots from a fully isolated, synthetic GUI state."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="steelvoicemix-screenshot-") as tmp:
        root = Path(tmp)
        runtime = root / "runtime"
        config = root / "config"
        runtime.mkdir(mode=0o700)
        config.mkdir()
        os.environ.update({
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(config),
            "XDG_RUNTIME_DIR": str(runtime),
            "QT_QPA_PLATFORM": "offscreen",
            "STEELVOICEMIX_STUB_SYSTEM": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        })
        sys.path.insert(0, str(repo))

        from PySide6.QtWidgets import QApplication
        import gui.daemon_client as dc
        import gui.game_eq as ge
        import gui.update_checker as uc

        dc.DaemonClient.run = lambda self: None
        dc.DaemonClient.send_command = lambda self, cmd, **kwargs: None
        dc.DaemonClient.stop = lambda self: None
        ge.GameWatcher.start = lambda self: None
        uc.UpdateChecker.start = lambda self, *args, **kwargs: None

        from gui.main_window import MixerGUI
        from gui.theme import apply_theme

        app = QApplication.instance() or QApplication(["steelvoicemix-screenshot"])
        apply_theme("light")
        win = MixerGUI()
        win.resize(1180, 880)
        win.show()
        win.signals.connected.emit()
        win.signals.oled_presence_changed.emit(True)
        win.signals.battery_updated.emit(78, "discharging")
        win.signals.chatmix_changed.emit(74, 56)
        win.signals.eq_enabled_changed.emit(True)
        win.signals.media_sink_changed.emit(True)
        for _ in range(5):
            app.processEvents()

        output = repo / "screenshots"
        output.mkdir(exist_ok=True)
        if not win.grab().save(str(output / "main-window.png"), "PNG"):
            raise RuntimeError("could not save main-window.png")

        win.overlay.set_orientation("vertical")
        win.overlay.show_volumes(74, 56, "center")
        for _ in range(3):
            app.processEvents()
        if not win.overlay.grab().save(str(output / "dial-overlay.png"), "PNG"):
            raise RuntimeError("could not save dial-overlay.png")
        win.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

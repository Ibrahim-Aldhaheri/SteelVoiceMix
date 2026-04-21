#!/usr/bin/python3
"""nova-mixer GUI — connects to the Rust daemon over a Unix socket."""

import json
import os
import signal
import socket
import sys
import subprocess
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QSystemTrayIcon, QMenu, QCheckBox, QComboBox,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QIcon, QAction, QFont, QPainter, QColor

APP_NAME = "nova-mixer"
APP_ICON = "audio-headset"

SETTINGS_FILE = Path.home() / ".config" / "nova-mixer" / "settings.conf"


def socket_path() -> str:
    """Get the daemon socket path (must match Rust daemon)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "nova-mixer.sock")
    return f"/tmp/nova-mixer-{os.getuid()}.sock"


OVERLAY_POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left", "center")
OVERLAY_ORIENTATIONS = ("horizontal", "vertical")


BOOL_KEYS = {"overlay", "autostart"}


def load_settings() -> dict:
    defaults = {
        "overlay": True,
        "autostart": True,
        "overlay_position": "top-right",
        "overlay_orientation": "horizontal",
    }
    if not SETTINGS_FILE.exists():
        return defaults
    try:
        settings = defaults.copy()
        for line in SETTINGS_FILE.read_text().strip().split("\n"):
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key, val = k.strip(), v.strip()
            if key in BOOL_KEYS:
                settings[key] = val.lower() in ("true", "1", "yes")
            else:
                settings[key] = val
        return settings
    except Exception:
        return defaults


def save_settings(settings: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in settings.items():
        if isinstance(v, bool):
            lines.append(f"{k}={str(v).lower()}")
        else:
            lines.append(f"{k}={v}")
    SETTINGS_FILE.write_text("\n".join(lines) + "\n")


class DialOverlay(QWidget):
    """Floating overlay that appears briefly when the dial is turned."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.orientation = "horizontal"
        self.setFixedSize(280, 80)

        self.game_vol = 100
        self.chat_vol = 100
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    def set_orientation(self, orientation: str):
        if orientation not in OVERLAY_ORIENTATIONS:
            orientation = "horizontal"
        self.orientation = orientation
        if orientation == "vertical":
            self.setFixedSize(170, 230)
        else:
            self.setFixedSize(280, 80)
        self.update()

    def show_volumes(self, game_vol: int, chat_vol: int, position: str = "top-right"):
        self.game_vol = game_vol
        self.chat_vol = chat_vol

        screen = QApplication.primaryScreen().geometry()
        margin = 24
        if position == "top-left":
            x, y = margin, margin
        elif position == "bottom-right":
            x = screen.width() - self.width() - margin
            y = screen.height() - self.height() - margin
        elif position == "bottom-left":
            x = margin
            y = screen.height() - self.height() - margin
        elif position == "center":
            x = (screen.width() - self.width()) // 2
            y = (screen.height() - self.height()) // 2
        else:  # top-right (default)
            x = screen.width() - self.width() - margin
            y = margin
        self.move(x, y)

        self.update()
        self.show()
        self._hide_timer.start(1500)

    def _fade_out(self):
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        palette = QApplication.palette()
        bg_color = palette.window().color()
        bg_color.setAlpha(220)
        text_color = palette.windowText().color()

        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        if self.orientation == "vertical":
            self._paint_vertical(painter, text_color)
        else:
            self._paint_horizontal(painter, text_color)

        painter.end()

    def _paint_horizontal(self, painter, text_color):
        bar_x, bar_w = 70, 190
        painter.setPen(text_color)
        painter.setFont(QFont("", 11))

        painter.drawText(10, 30, "🎮 Game")
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(bar_x, 18, bar_w, 16, 4, 4)
        game_w = int(bar_w * self.game_vol / 100)
        painter.setBrush(QColor(76, 175, 80))
        painter.drawRoundedRect(bar_x, 18, game_w, 16, 4, 4)
        painter.drawText(bar_x + bar_w + 5, 30, f"{self.game_vol}%")

        painter.drawText(10, 62, "💬 Chat")
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(bar_x, 50, bar_w, 16, 4, 4)
        chat_w = int(bar_w * self.chat_vol / 100)
        painter.setBrush(QColor(33, 150, 243))
        painter.drawRoundedRect(bar_x, 50, chat_w, 16, 4, 4)
        painter.drawText(bar_x + bar_w + 5, 62, f"{self.chat_vol}%")

    def _paint_vertical(self, painter, text_color):
        bar_w = 26
        bar_h = 140
        bar_top = 40
        game_x = 30
        chat_x = self.width() - game_x - bar_w

        painter.setPen(text_color)
        painter.setFont(QFont("", 11))
        painter.drawText(game_x - 14, 22, "🎮 Game")
        painter.drawText(chat_x - 14, 22, "💬 Chat")

        # Game column — fills bottom-up
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(game_x, bar_top, bar_w, bar_h, 5, 5)
        game_fill = int(bar_h * self.game_vol / 100)
        painter.setBrush(QColor(76, 175, 80))
        painter.drawRoundedRect(
            game_x, bar_top + bar_h - game_fill, bar_w, game_fill, 5, 5
        )

        # Chat column
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(chat_x, bar_top, bar_w, bar_h, 5, 5)
        chat_fill = int(bar_h * self.chat_vol / 100)
        painter.setBrush(QColor(33, 150, 243))
        painter.drawRoundedRect(
            chat_x, bar_top + bar_h - chat_fill, bar_w, chat_fill, 5, 5
        )

        painter.setPen(text_color)
        painter.drawText(game_x - 6, bar_top + bar_h + 22, f"{self.game_vol}%")
        painter.drawText(chat_x - 6, bar_top + bar_h + 22, f"{self.chat_vol}%")


class DaemonSignals(QObject):
    """Signals emitted from the socket reader thread to update the GUI."""
    connected = Signal()
    disconnected = Signal()
    chatmix_changed = Signal(int, int)
    status_message = Signal(str)
    battery_updated = Signal(int, str)


class DaemonClient:
    """Connects to the Rust daemon over a Unix socket and subscribes to events."""

    def __init__(self, signals: DaemonSignals):
        self.signals = signals
        self.running = True
        self._sock = None

    def run(self):
        """Connect and read events in a loop. Reconnects on failure."""
        while self.running:
            try:
                self._connect_and_subscribe()
            except Exception:
                pass

            if self.running:
                self.signals.status_message.emit("🔍 Connecting to daemon...")
                import time
                time.sleep(2)

    def _connect_and_subscribe(self):
        path = socket_path()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(5)
        self._sock.connect(path)
        self._sock.settimeout(None)

        # Subscribe to events
        self._sock.sendall(b'{"cmd":"subscribe"}\n')

        buf = b""
        while self.running:
            try:
                self._sock.settimeout(2)
                data = self._sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle_event(json.loads(line))
            except socket.timeout:
                continue
            except Exception:
                break

        try:
            self._sock.close()
        except Exception:
            pass

    def _handle_event(self, event: dict):
        ev_type = event.get("event", "")

        if ev_type == "chatmix":
            game = event.get("game", 0)
            chat = event.get("chat", 0)
            self.signals.chatmix_changed.emit(game, chat)

        elif ev_type == "battery":
            level = event.get("level", 0)
            status = event.get("status", "offline")
            self.signals.battery_updated.emit(level, status)

        elif ev_type == "connected":
            self.signals.connected.emit()

        elif ev_type == "disconnected":
            self.signals.disconnected.emit()

        elif ev_type == "status":
            # Initial status on subscribe
            if event.get("connected"):
                self.signals.connected.emit()
                game = event.get("game_vol", 100)
                chat = event.get("chat_vol", 100)
                self.signals.chatmix_changed.emit(game, chat)
                bat = event.get("battery")
                if isinstance(bat, dict):
                    self.signals.battery_updated.emit(
                        bat.get("level", 0),
                        bat.get("status", "offline"),
                    )
            else:
                self.signals.disconnected.emit()

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("nova-mixer")
        self.setFixedSize(360, 420)
        self.setWindowIcon(QIcon.fromTheme(APP_ICON))

        self.signals = DaemonSignals()
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.status_message.connect(self._on_status)
        self.signals.battery_updated.connect(self._on_battery)

        self.settings = load_settings()
        self.overlay = DialOverlay()
        self.overlay.set_orientation(
            self.settings.get("overlay_orientation", "horizontal")
        )
        self._build_ui()
        self._build_tray()
        self._start_daemon_client()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        self.status_label = QLabel("🔍 Connecting to daemon...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        layout.addWidget(self.status_label)

        # Game volume
        game_row = QHBoxLayout()
        game_label = QLabel("🎮 Game")
        game_label.setFixedWidth(70)
        game_label.setStyleSheet("font-size: 12px;")
        self.game_bar = QProgressBar()
        self.game_bar.setRange(0, 100)
        self.game_bar.setValue(100)
        self.game_bar.setTextVisible(True)
        self.game_bar.setFormat("%v%")
        self.game_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #555; border-radius: 4px; height: 22px; }
            QProgressBar::chunk { background: #4CAF50; border-radius: 3px; }
        """)
        game_row.addWidget(game_label)
        game_row.addWidget(self.game_bar)
        layout.addLayout(game_row)

        # Chat volume
        chat_row = QHBoxLayout()
        chat_label = QLabel("💬 Chat")
        chat_label.setFixedWidth(70)
        chat_label.setStyleSheet("font-size: 12px;")
        self.chat_bar = QProgressBar()
        self.chat_bar.setRange(0, 100)
        self.chat_bar.setValue(100)
        self.chat_bar.setTextVisible(True)
        self.chat_bar.setFormat("%v%")
        self.chat_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #555; border-radius: 4px; height: 22px; }
            QProgressBar::chunk { background: #2196F3; border-radius: 3px; }
        """)
        chat_row.addWidget(chat_label)
        chat_row.addWidget(self.chat_bar)
        layout.addLayout(chat_row)

        self.dial_label = QLabel("⚖️ Balanced")
        self.dial_label.setAlignment(Qt.AlignCenter)
        self.dial_label.setStyleSheet("font-size: 11px; color: #888;")
        layout.addWidget(self.dial_label)

        # Battery
        battery_row = QHBoxLayout()
        self.battery_label = QLabel("🔋 Battery")
        self.battery_label.setFixedWidth(90)
        self.battery_label.setStyleSheet("font-size: 12px;")
        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setValue(0)
        self.battery_bar.setTextVisible(True)
        self.battery_bar.setFormat("—")
        self.battery_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #555; border-radius: 4px; height: 22px; }
            QProgressBar::chunk { background: #FF9800; border-radius: 3px; }
        """)
        battery_row.addWidget(self.battery_label)
        battery_row.addWidget(self.battery_bar)
        layout.addLayout(battery_row)

        # Settings
        settings_layout = QVBoxLayout()
        settings_layout.setSpacing(6)

        self.overlay_check = QCheckBox("Show overlay when dial is turned")
        self.overlay_check.setChecked(self.settings.get("overlay", True))
        self.overlay_check.toggled.connect(self._toggle_overlay)
        settings_layout.addWidget(self.overlay_check)

        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("Overlay position:"))
        self.position_combo = QComboBox()
        self.position_combo.addItems(
            ["Top-right", "Top-left", "Bottom-right", "Bottom-left", "Center"]
        )
        current_pos = self.settings.get("overlay_position", "top-right")
        idx = self.position_combo.findText(current_pos.replace("-", " ").title())
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        self.position_combo.currentTextChanged.connect(self._change_position)
        position_row.addWidget(self.position_combo, 1)
        settings_layout.addLayout(position_row)

        orient_row = QHBoxLayout()
        orient_row.addWidget(QLabel("Overlay style:"))
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Horizontal", "Vertical"])
        current_orient = self.settings.get("overlay_orientation", "horizontal")
        idx = self.orient_combo.findText(current_orient.capitalize())
        if idx >= 0:
            self.orient_combo.setCurrentIndex(idx)
        self.orient_combo.currentTextChanged.connect(self._change_orientation)
        orient_row.addWidget(self.orient_combo, 1)
        settings_layout.addLayout(orient_row)

        self.autostart_check = QCheckBox("Start with system")
        self.autostart_check.setChecked(self.settings.get("autostart", True))
        self.autostart_check.toggled.connect(self._toggle_autostart)
        settings_layout.addWidget(self.autostart_check)

        layout.addLayout(settings_layout)
        layout.addStretch()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon.fromTheme(APP_ICON), self)
        self.tray.setToolTip("nova-mixer")

        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show_window()

    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray.showMessage("nova-mixer", "Minimized to tray", QSystemTrayIcon.Information, 2000)

    def _quit(self):
        self.daemon_client.stop()
        QApplication.quit()

    def _start_daemon_client(self):
        self.daemon_client = DaemonClient(self.signals)
        self.daemon_thread = threading.Thread(target=self.daemon_client.run, daemon=True)
        self.daemon_thread.start()

    def _on_connected(self):
        self.status_label.setText("🟢 Connected — ChatMix Active")
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #4CAF50;")

    def _on_disconnected(self):
        self.status_label.setText("🔴 Disconnected — Reconnecting...")
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #f44336;")
        self.game_bar.setValue(0)
        self.chat_bar.setValue(0)
        self.dial_label.setText("⚖️ —")

    def _on_chatmix(self, game_vol, chat_vol):
        self.game_bar.setValue(game_vol)
        self.chat_bar.setValue(chat_vol)

        diff = game_vol - chat_vol
        if abs(diff) < 10:
            pos = "⚖️ Balanced"
        elif diff > 0:
            pos = f"🎮 Game +{diff}"
        else:
            pos = f"💬 Chat +{-diff}"
        self.dial_label.setText(pos)

        if self.settings.get("overlay", True):
            pos = self.settings.get("overlay_position", "top-right")
            if pos not in OVERLAY_POSITIONS:
                pos = "top-right"
            self.overlay.show_volumes(game_vol, chat_vol, pos)

    def _on_battery(self, level, status):
        self.battery_bar.setValue(level)
        if status == "charging":
            self.battery_bar.setFormat(f"⚡ {level}%")
            self.battery_bar.setStyleSheet("""
                QProgressBar { border: 1px solid #555; border-radius: 4px; height: 22px; }
                QProgressBar::chunk { background: #4CAF50; border-radius: 3px; }
            """)
        elif status == "offline":
            self.battery_bar.setFormat("Offline")
            self.battery_bar.setValue(0)
        else:
            self.battery_bar.setFormat(f"{level}%")
            color = "#4CAF50" if level > 50 else "#FF9800" if level > 20 else "#f44336"
            self.battery_bar.setStyleSheet(f"""
                QProgressBar {{ border: 1px solid #555; border-radius: 4px; height: 22px; }}
                QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}
            """)
        self.tray.setToolTip(f"nova-mixer — 🔋 {level}% ({status})")

    def _toggle_overlay(self, checked):
        self.settings["overlay"] = checked
        save_settings(self.settings)

    def _change_position(self, text):
        key = text.lower().replace(" ", "-")
        if key not in OVERLAY_POSITIONS:
            return
        self.settings["overlay_position"] = key
        save_settings(self.settings)
        # Flash a preview so the user sees where it will appear
        self.overlay.show_volumes(self.game_bar.value(), self.chat_bar.value(), key)

    def _change_orientation(self, text):
        key = text.lower()
        if key not in OVERLAY_ORIENTATIONS:
            return
        self.settings["overlay_orientation"] = key
        save_settings(self.settings)
        self.overlay.set_orientation(key)
        self.overlay.show_volumes(
            self.game_bar.value(),
            self.chat_bar.value(),
            self.settings.get("overlay_position", "top-right"),
        )

    def _toggle_autostart(self, checked):
        self.settings["autostart"] = checked
        save_settings(self.settings)
        verb = "enable" if checked else "disable"
        for unit in ("nova-mixer", "nova-mixer-gui"):
            try:
                subprocess.run(
                    ["systemctl", "--user", verb, unit],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

    def _on_status(self, msg):
        self.status_label.setText(msg)


def main():
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


if __name__ == "__main__":
    main()

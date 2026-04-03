#!/usr/bin/python3
"""nova-mixer GUI — minimal monitor for ChatMix status."""

import sys
import subprocess
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QSystemTrayIcon, QMenu, QCheckBox,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QPropertyAnimation, QPoint
from PySide6.QtGui import QIcon, QAction, QFont, QPainter, QColor, QKeySequence, QShortcut

# Import the core mixer
from nova_mixer_core import NovaMixer, log, GAME_SINK, CHAT_SINK, notify

APP_NAME = "nova-mixer"
APP_ICON = "audio-headset"


SETTINGS_FILE = Path.home() / ".config" / "nova-mixer" / "settings.conf"
SERVICE_FILE = Path.home() / ".config" / "systemd" / "user" / "nova-mixer.service"


def load_settings() -> dict:
    """Load settings from config file."""
    defaults = {"overlay": True, "autostart": True}
    if not SETTINGS_FILE.exists():
        return defaults
    try:
        settings = defaults.copy()
        for line in SETTINGS_FILE.read_text().strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                settings[k.strip()] = v.strip().lower() in ("true", "1", "yes")
        return settings
    except Exception:
        return defaults


def save_settings(settings: dict):
    """Save settings to config file."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={str(v).lower()}" for k, v in settings.items()]
    SETTINGS_FILE.write_text("\n".join(lines) + "\n")


class DialOverlay(QWidget):
    """Floating overlay that appears briefly when the dial is turned."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(280, 80)

        self.game_vol = 100
        self.chat_vol = 100
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._opacity = 1.0

    def show_volumes(self, game_vol: int, chat_vol: int):
        """Show overlay with current volumes, auto-hide after 1.5s."""
        self.game_vol = game_vol
        self.chat_vol = chat_vol
        self._opacity = 1.0

        # Position at top-center of screen
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = 60
        self.move(x, y)

        self.update()
        self.show()
        self._hide_timer.start(1500)

    def _fade_out(self):
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(self._opacity)

        # Use system palette for theme awareness
        palette = QApplication.palette()
        bg_color = palette.window().color()
        bg_color.setAlpha(220)
        text_color = palette.windowText().color()

        # Background
        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        # Game bar
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

        # Chat bar
        painter.drawText(10, 62, "💬 Chat")
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(bar_x, 50, bar_w, 16, 4, 4)
        chat_w = int(bar_w * self.chat_vol / 100)
        painter.setBrush(QColor(33, 150, 243))
        painter.drawRoundedRect(bar_x, 50, chat_w, 16, 4, 4)
        painter.drawText(bar_x + bar_w + 5, 62, f"{self.chat_vol}%")

        painter.end()


class MixerSignals(QObject):
    """Signals emitted from the mixer thread to update the GUI."""
    connected = Signal()
    disconnected = Signal()
    chatmix_changed = Signal(int, int)  # game_vol, chat_vol
    status_message = Signal(str)
    battery_updated = Signal(int, str)  # level, status


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("nova-mixer")
        self.setFixedSize(320, 320)
        self.setWindowIcon(QIcon.fromTheme(APP_ICON))

        # Signals bridge between mixer thread and GUI
        self.signals = MixerSignals()
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.status_message.connect(self._on_status)
        self.signals.battery_updated.connect(self._on_battery)

        self.settings = load_settings()
        self.overlay = DialOverlay()
        self._build_ui()
        self._build_tray()
        self._start_mixer()

    # ── UI ──────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Status
        self.status_label = QLabel("🔍 Looking for base station...")
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

        # Dial position indicator
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

        self.autostart_check = QCheckBox("Start with system")
        self.autostart_check.setChecked(self.settings.get("autostart", True))
        self.autostart_check.toggled.connect(self._toggle_autostart)
        settings_layout.addWidget(self.autostart_check)

        layout.addLayout(settings_layout)
        layout.addStretch()

    # ── System Tray ─────────────────────────────────────
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

    # ── Minimize to tray instead of closing ─────────────
    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray.showMessage("nova-mixer", "Minimized to tray", QSystemTrayIcon.Information, 2000)

    def _quit(self):
        self.mixer.running = False
        QApplication.quit()

    # ── Mixer Thread ────────────────────────────────────
    def _start_mixer(self):
        self.mixer = GUIMixer(self.signals)
        self.mixer_thread = threading.Thread(target=self.mixer.run, daemon=True)
        self.mixer_thread.start()

    # ── Signal Handlers ─────────────────────────────────
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

        # Describe dial position
        diff = game_vol - chat_vol
        if abs(diff) < 10:
            pos = "⚖️ Balanced"
        elif diff > 0:
            pos = f"🎮 Game +{diff}"
        else:
            pos = f"💬 Chat +{-diff}"
        self.dial_label.setText(pos)

        # Show overlay
        if self.settings.get("overlay", True):
            self.overlay.show_volumes(game_vol, chat_vol)

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

    def _toggle_autostart(self, checked):
        self.settings["autostart"] = checked
        save_settings(self.settings)
        try:
            if checked:
                subprocess.run(
                    ["systemctl", "--user", "enable", "nova-mixer"],
                    capture_output=True, timeout=5
                )
            else:
                subprocess.run(
                    ["systemctl", "--user", "disable", "nova-mixer"],
                    capture_output=True, timeout=5
                )
        except Exception:
            pass

    def _on_status(self, msg):
        self.status_label.setText(msg)


class GUIMixer(NovaMixer):
    """Extended mixer that emits Qt signals for GUI updates."""

    def __init__(self, signals: MixerSignals):
        super().__init__()
        self.signals = signals

    def _enable_chatmix(self):
        super()._enable_chatmix()
        self.signals.connected.emit()

    def _cleanup_device(self):
        self.signals.disconnected.emit()
        super()._cleanup_device()

    def _set_volume(self, sink, volume):
        super()._set_volume(sink, volume)

    def run(self):
        """Override to emit chatmix signals."""
        log.info("nova-mixer GUI starting...")

        while self.running:
            output_sink = self._find_output_sink()
            if not output_sink:
                self.signals.status_message.emit("🔍 Looking for base station...")
                self._wait_reconnect()
                continue

            if not self._open_device():
                self.signals.status_message.emit("🔍 Base station not found...")
                self._wait_reconnect()
                continue

            try:
                self._enable_chatmix()
                self._create_sinks(output_sink)
                notify("🎧 ChatMix Active", "NovaGame and NovaChat sinks ready.")
            except ConnectionError:
                self._cleanup_device()
                continue

            # Poll battery on connect
            self._poll_battery()

            log.info("Listening for ChatMix dial events...")
            reconnect_needed = False
            battery_timer = 0
            while self.running and not reconnect_needed:
                try:
                    msg = self.dev.read(64, 1000)
                    if not msg:
                        # Poll battery every ~60 seconds (60 * 1s timeout)
                        battery_timer += 1
                        if battery_timer >= 60:
                            self._poll_battery()
                            battery_timer = 0
                        continue
                    if msg[1] == 0x45:  # OPT_CHATMIX
                        game_vol = msg[2]
                        chat_vol = msg[3]
                        self._set_volume(GAME_SINK, game_vol)
                        self._set_volume(CHAT_SINK, chat_vol)
                        self.signals.chatmix_changed.emit(game_vol, chat_vol)
                except OSError:
                    log.warning("Device disconnected")
                    notify("🎧 Disconnected", "Waiting for reconnect...")
                    reconnect_needed = True

            self._cleanup_device()

        self._destroy_sinks()
        log.info("nova-mixer stopped")

    def _poll_battery(self):
        """Query battery and emit signal."""
        result = self.get_battery()
        if result:
            self.signals.battery_updated.emit(result["level"], result["status"])


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray

    # Follow system theme (KDE Breeze, etc.)
    app.setStyle("fusion")  # Fallback — KDE will override with Breeze if available

    window = MixerGUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

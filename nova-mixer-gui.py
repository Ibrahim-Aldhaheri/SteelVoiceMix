#!/usr/bin/python3
"""nova-mixer GUI — minimal monitor for ChatMix status."""

import sys
import threading

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QSystemTrayIcon, QMenu,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QIcon, QAction

# Import the core mixer
from nova_mixer_core import NovaMixer, log, GAME_SINK, CHAT_SINK, notify

APP_NAME = "nova-mixer"
APP_ICON = "audio-headset"


class MixerSignals(QObject):
    """Signals emitted from the mixer thread to update the GUI."""
    connected = Signal()
    disconnected = Signal()
    chatmix_changed = Signal(int, int)  # game_vol, chat_vol
    status_message = Signal(str)


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("nova-mixer")
        self.setFixedSize(320, 200)
        self.setWindowIcon(QIcon.fromTheme(APP_ICON))

        # Signals bridge between mixer thread and GUI
        self.signals = MixerSignals()
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.status_message.connect(self._on_status)

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

            log.info("Listening for ChatMix dial events...")
            reconnect_needed = False
            while self.running and not reconnect_needed:
                try:
                    msg = self.dev.read(64, 1000)
                    if not msg:
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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray

    window = MixerGUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

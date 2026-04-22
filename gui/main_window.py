"""Main SteelVoiceMix window — status dashboard, settings, and tray integration."""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .about import make_about_dialog
from .daemon_client import DaemonClient, DaemonSignals
from .overlay import DialOverlay
from .settings import (
    APP_NAME,
    DISPLAY_NAME,
    OVERLAY_ORIENTATIONS,
    OVERLAY_POSITIONS,
    load as load_settings,
    normalize_orientation,
    normalize_position,
    save as save_settings,
)

APP_ICON = "steelvoicemix"
APP_ICON_FALLBACK = "audio-headset"

log = logging.getLogger(__name__)


def _app_icon() -> QIcon:
    """Return our installed icon, falling back to the generic theme icon
    when running from a source checkout that hasn't been installed yet."""
    return QIcon.fromTheme(APP_ICON, QIcon.fromTheme(APP_ICON_FALLBACK))


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(DISPLAY_NAME)
        self.setFixedSize(360, 480)
        self.setWindowIcon(_app_icon())

        self.signals = DaemonSignals()
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.status_message.connect(self._on_status)
        self.signals.battery_updated.connect(self._on_battery)
        self.signals.media_sink_changed.connect(self._on_media_sink_changed)
        # Track the daemon's reported media-sink state so the toggle button
        # renders correctly; default optimistically to True until the first
        # status event arrives.
        self._media_sink_enabled = True

        self.settings = load_settings()
        self.overlay = DialOverlay()
        self.overlay.set_orientation(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            )
        )

        # Cross-DE: some sessions (GNOME without extensions, minimal WMs)
        # have no status-notifier. Detect that once and skip hide-to-tray.
        self.has_tray = QSystemTrayIcon.isSystemTrayAvailable()
        if not self.has_tray:
            log.warning(
                "System tray not available — closing the window will quit "
                "instead of hiding to tray."
            )

        # Detect Wayland so we can warn about overlay stacking order when
        # the user has forced QT_QPA_PLATFORM=wayland (the launcher defaults
        # to xcb, but the override env var is respected for advanced users).
        self._wayland = (
            os.environ.get("XDG_SESSION_TYPE") == "wayland"
            and os.environ.get("QT_QPA_PLATFORM", "").startswith("wayland")
        )

        self._build_ui()
        if self.has_tray:
            self._build_tray()
        self._start_daemon_client()

    # ---------------------------------------------------------------- layout

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
        self.game_bar = self._make_bar("#4CAF50")
        game_row.addWidget(game_label)
        game_row.addWidget(self.game_bar)
        layout.addLayout(game_row)

        # Chat volume
        chat_row = QHBoxLayout()
        chat_label = QLabel("💬 Chat")
        chat_label.setFixedWidth(70)
        chat_label.setStyleSheet("font-size: 12px;")
        self.chat_bar = self._make_bar("#2196F3")
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
        self.battery_bar.setStyleSheet(
            """
            QProgressBar { border: 1px solid #555; border-radius: 4px; height: 22px; }
            QProgressBar::chunk { background: #FF9800; border-radius: 3px; }
            """
        )
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
        idx = self.position_combo.findText(
            normalize_position(self.settings.get("overlay_position", "top-right"))
            .replace("-", " ")
            .title()
        )
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        self.position_combo.currentTextChanged.connect(self._change_position)
        position_row.addWidget(self.position_combo, 1)
        settings_layout.addLayout(position_row)

        orient_row = QHBoxLayout()
        orient_row.addWidget(QLabel("Overlay style:"))
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Horizontal", "Vertical"])
        idx = self.orient_combo.findText(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
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

        # Media sink toggle — add or remove the NovaMedia virtual output
        # without restarting the daemon. Independent of the dial.
        media_row = QHBoxLayout()
        media_row.addWidget(QLabel("Media sink:"))
        self.media_btn = QPushButton("Remove Media")
        self.media_btn.clicked.connect(self._toggle_media_sink)
        media_row.addWidget(self.media_btn, 1)
        layout.addLayout(media_row)

        # About button — row aligned right
        about_row = QHBoxLayout()
        about_row.addStretch()
        self.about_btn = QPushButton("About…")
        self.about_btn.setFlat(True)
        self.about_btn.setStyleSheet("color: #888; font-size: 11px;")
        self.about_btn.clicked.connect(self._show_about)
        about_row.addWidget(self.about_btn)
        layout.addLayout(about_row)

        layout.addStretch()

        if self._wayland:
            # Launcher forces xcb; this only fires if the user has overridden
            # that, usually knowingly. Keep the hint terse.
            hint = QLabel(
                "⚠ Wayland session detected. The overlay may appear below "
                "fullscreen windows. Unset QT_QPA_PLATFORM or re-run the "
                "installer to restore XCB."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #FF9800; font-size: 10px;")
            layout.addWidget(hint)

    def _make_bar(self, chunk_color: str) -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(100)
        bar.setTextVisible(True)
        bar.setFormat("%v%")
        bar.setStyleSheet(
            f"""
            QProgressBar {{ border: 1px solid #555; border-radius: 4px; height: 22px; }}
            QProgressBar::chunk {{ background: {chunk_color}; border-radius: 3px; }}
            """
        )
        return bar

    def _build_tray(self):
        self.tray = QSystemTrayIcon(_app_icon(), self)
        self.tray.setToolTip(DISPLAY_NAME)

        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    # -------------------------------------------------------- event handlers

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show_window()

    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if not self.has_tray:
            event.accept()
            self._quit()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            DISPLAY_NAME,
            "Minimized to tray",
            QSystemTrayIcon.Information,
            2000,
        )

    def _quit(self):
        self.daemon_client.stop()
        QApplication.quit()

    def _start_daemon_client(self):
        self.daemon_client = DaemonClient(self.signals)
        self.daemon_thread = threading.Thread(
            target=self.daemon_client.run, daemon=True
        )
        self.daemon_thread.start()

    def _on_connected(self):
        self.status_label.setText("🟢 Connected — ChatMix Active")
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #4CAF50;"
        )

    def _on_disconnected(self):
        self.status_label.setText("🔴 Disconnected — Reconnecting...")
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #f44336;"
        )
        self.game_bar.setValue(0)
        self.chat_bar.setValue(0)
        self.dial_label.setText("⚖️ —")

    def _on_chatmix(self, game_vol, chat_vol):
        self.game_bar.setValue(game_vol)
        self.chat_bar.setValue(chat_vol)

        diff = game_vol - chat_vol
        if abs(diff) < 10:
            label = "⚖️ Balanced"
        elif diff > 0:
            label = f"🎮 Game +{diff}"
        else:
            label = f"💬 Chat +{-diff}"
        self.dial_label.setText(label)

        if self.settings.get("overlay", True):
            pos = normalize_position(
                self.settings.get("overlay_position", "top-right")
            )
            self.overlay.show_volumes(game_vol, chat_vol, pos)

    def _on_battery(self, level, status):
        self.battery_bar.setValue(level)
        if status == "charging":
            self.battery_bar.setFormat(f"⚡ {level}%")
            chunk = "#4CAF50"
        elif status == "offline":
            self.battery_bar.setFormat("Offline")
            self.battery_bar.setValue(0)
            chunk = "#FF9800"
        else:
            self.battery_bar.setFormat(f"{level}%")
            chunk = "#4CAF50" if level > 50 else "#FF9800" if level > 20 else "#f44336"

        self.battery_bar.setStyleSheet(
            f"""
            QProgressBar {{ border: 1px solid #555; border-radius: 4px; height: 22px; }}
            QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}
            """
        )
        if self.has_tray:
            self.tray.setToolTip(f"{DISPLAY_NAME} — 🔋 {level}% ({status})")

    def _toggle_overlay(self, checked):
        self.settings["overlay"] = checked
        save_settings(self.settings)

    def _change_position(self, text):
        key = text.lower().replace(" ", "-")
        if key not in OVERLAY_POSITIONS:
            return
        self.settings["overlay_position"] = key
        save_settings(self.settings)
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
            normalize_position(self.settings.get("overlay_position", "top-right")),
        )

    def _toggle_autostart(self, checked):
        self.settings["autostart"] = checked
        save_settings(self.settings)
        verb = "enable" if checked else "disable"
        for unit in (APP_NAME, f"{APP_NAME}-gui"):
            try:
                subprocess.run(
                    ["systemctl", "--user", verb, unit],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass

    def _on_status(self, msg):
        self.status_label.setText(msg)

    def _show_about(self):
        dialog = make_about_dialog(self)
        dialog.exec()

    def _on_media_sink_changed(self, enabled: bool):
        self._media_sink_enabled = enabled
        self.media_btn.setText("Remove Media" if enabled else "Add Media")
        self.media_btn.setToolTip(
            "Destroy the NovaMedia virtual sink"
            if enabled
            else "Create a NovaMedia virtual sink that bypasses the ChatMix dial"
        )

    def _toggle_media_sink(self):
        cmd = "remove-media-sink" if self._media_sink_enabled else "add-media-sink"
        self.daemon_client.send_command(cmd)
        # Disable the button until the daemon confirms the change so quick
        # double-clicks don't queue conflicting commands.
        self.media_btn.setEnabled(False)
        self._media_btn_reenable_timer()

    def _media_btn_reenable_timer(self):
        from PySide6.QtCore import QTimer

        def reenable():
            self.media_btn.setEnabled(True)

        QTimer.singleShot(600, reenable)

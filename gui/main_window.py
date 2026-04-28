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
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
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
    delete_profile,
    list_profiles,
    load as load_settings,
    load_profile,
    normalize_orientation,
    normalize_position,
    save as save_settings,
    save_profile,
)
from .update_checker import UpdateChecker

APP_ICON = "steelvoicemix"
APP_ICON_FALLBACK = "audio-headset"

log = logging.getLogger(__name__)


# Global stylesheet — gives the window a more cohesive look without
# overriding the user's system theme too aggressively. Most of these
# rules just tighten spacing, give buttons consistent padding, and
# soften borders. The progress bars keep their explicit per-bar styles
# (chunk colours) — those override these defaults where needed.
_GLOBAL_QSS = """
QMainWindow {
    background-color: palette(window);
}
QTabWidget::pane {
    border: 1px solid palette(mid);
    border-radius: 6px;
    background: palette(base);
    top: -1px;
}
QTabBar::tab {
    background: palette(window);
    border: 1px solid palette(mid);
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 14px;
    min-width: 60px;
    color: palette(text);
}
QTabBar::tab:selected {
    background: palette(base);
    font-weight: bold;
}
QTabBar::tab:!selected:hover {
    background: palette(midlight);
}
QPushButton {
    padding: 5px 12px;
    border-radius: 4px;
    border: 1px solid palette(mid);
    background: palette(button);
    min-height: 22px;
}
QPushButton:hover {
    background: palette(midlight);
}
QPushButton:pressed {
    background: palette(mid);
}
QPushButton:disabled {
    color: palette(placeholder-text);
}
QPushButton:flat {
    border: none;
    background: transparent;
}
QComboBox {
    padding: 4px 8px;
    border: 1px solid palette(mid);
    border-radius: 4px;
    min-height: 22px;
}
QCheckBox {
    spacing: 8px;
}
QLabel#section-title {
    font-weight: bold;
    font-size: 11px;
    color: palette(placeholder-text);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
}
QFrame[divider="true"] {
    background: palette(mid);
    max-height: 1px;
    min-height: 1px;
    margin: 4px 0;
}
"""


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section-title")
    return label


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setProperty("divider", True)
    return line


# Canonical settings key → exact display string used in the position combo.
# Avoid using .replace("-", " ").title() to derive this — the items in the
# combo keep the dash, so a space-separated lookup never matches and the
# selected index doesn't update on profile load (or on startup if the
# user's saved position isn't the default).
_POSITION_DISPLAY: dict[str, str] = {
    "top-right": "Top-right",
    "top-left": "Top-left",
    "bottom-right": "Bottom-right",
    "bottom-left": "Bottom-left",
    "center": "Center",
}


def _app_icon() -> QIcon:
    """Return our installed icon, falling back to the generic theme icon
    when running from a source checkout that hasn't been installed yet."""
    return QIcon.fromTheme(APP_ICON, QIcon.fromTheme(APP_ICON_FALLBACK))


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(DISPLAY_NAME)
        self.setFixedSize(440, 560)
        self.setWindowIcon(_app_icon())
        self.setStyleSheet(_GLOBAL_QSS)

        self.signals = DaemonSignals()
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.status_message.connect(self._on_status)
        self.signals.battery_updated.connect(self._on_battery)
        self.signals.media_sink_changed.connect(self._on_media_sink_changed)
        self.signals.hdmi_sink_changed.connect(self._on_hdmi_sink_changed)
        self.signals.auto_route_browsers_changed.connect(
            self._on_auto_route_browsers_changed
        )
        # Track the daemon's reported sink-toggle states so the buttons
        # render correctly. Daemon defaults are "off until the user opts in"
        # so we start with False; the first status event corrects them.
        self._media_sink_enabled = False
        self._hdmi_sink_enabled = False
        self._auto_route_browsers = False

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
        self._update_checker = None
        self._start_update_check()

    # ---------------------------------------------------------------- layout

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # Persistent header — connection status always visible above the tabs.
        self.status_label = QLabel("🔍 Connecting to daemon...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold; padding: 4px;")
        root.addWidget(self.status_label)

        tabs = QTabWidget()
        tabs.addTab(self._build_home_tab(), "Home")
        tabs.addTab(self._build_sinks_tab(), "Sinks")
        tabs.addTab(self._build_settings_tab(), "Settings")
        root.addWidget(tabs, 1)

        # Persistent footer — update status + check-now + about, always visible.
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.update_label = QLabel("Up to date")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")
        update_btn = QPushButton("Check for updates")
        update_btn.setFlat(True)
        update_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        update_btn.clicked.connect(self._force_update_check)
        self.about_btn = QPushButton("About…")
        self.about_btn.setFlat(True)
        self.about_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        self.about_btn.clicked.connect(self._show_about)
        footer.addWidget(self.update_label, 1)
        footer.addWidget(update_btn)
        footer.addWidget(self.about_btn)
        root.addLayout(footer)

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
            root.addWidget(hint)

    # ---------------------------------------------------------------- tabs

    def _build_home_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title("ChatMix"))

        game_row = QHBoxLayout()
        game_label = QLabel("🎮 Game")
        game_label.setFixedWidth(70)
        self.game_bar = self._make_bar("#4CAF50")
        game_row.addWidget(game_label)
        game_row.addWidget(self.game_bar)
        layout.addLayout(game_row)

        chat_row = QHBoxLayout()
        chat_label = QLabel("💬 Chat")
        chat_label.setFixedWidth(70)
        self.chat_bar = self._make_bar("#2196F3")
        chat_row.addWidget(chat_label)
        chat_row.addWidget(self.chat_bar)
        layout.addLayout(chat_row)

        self.dial_label = QLabel("⚖️ Balanced")
        self.dial_label.setAlignment(Qt.AlignCenter)
        self.dial_label.setStyleSheet("font-size: 11px; color: palette(placeholder-text);")
        layout.addWidget(self.dial_label)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Headset"))

        battery_row = QHBoxLayout()
        self.battery_label = QLabel("🔋 Battery")
        self.battery_label.setFixedWidth(90)
        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setValue(0)
        self.battery_bar.setTextVisible(True)
        self.battery_bar.setFormat("—")
        self.battery_bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; height: 22px; }"
            "QProgressBar::chunk { background: #FF9800; border-radius: 3px; }"
        )
        battery_row.addWidget(self.battery_label)
        battery_row.addWidget(self.battery_bar)
        layout.addLayout(battery_row)

        layout.addStretch(1)
        return page

    def _build_sinks_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title("Virtual Sinks"))

        media_row = QHBoxLayout()
        media_lbl = QLabel("Media")
        media_lbl.setFixedWidth(70)
        self.media_btn = QPushButton("Add Media")
        self.media_btn.clicked.connect(self._toggle_media_sink)
        media_row.addWidget(media_lbl)
        media_row.addWidget(self.media_btn, 1)
        layout.addLayout(media_row)

        hdmi_row = QHBoxLayout()
        hdmi_lbl = QLabel("HDMI")
        hdmi_lbl.setFixedWidth(70)
        self.hdmi_btn = QPushButton("Add HDMI")
        self.hdmi_btn.clicked.connect(self._toggle_hdmi_sink)
        hdmi_row.addWidget(hdmi_lbl)
        hdmi_row.addWidget(self.hdmi_btn, 1)
        layout.addLayout(hdmi_row)

        sinks_help = QLabel(
            "Media and HDMI sinks bypass the ChatMix dial — useful for "
            "music, browsers, or routing audio to a TV/AVR independently "
            "of the headset."
        )
        sinks_help.setStyleSheet("font-size: 10px; color: palette(placeholder-text); padding-top: 4px;")
        sinks_help.setWordWrap(True)
        layout.addWidget(sinks_help)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Auto-Routing"))

        self.auto_route_check = QCheckBox(
            "Route browsers and media players to SteelMedia automatically"
        )
        self.auto_route_check.setToolTip(
            "When enabled, the daemon moves new browser and media-player "
            "audio streams (Firefox, Chromium, mpv, VLC…) to the SteelMedia "
            "sink so they bypass the ChatMix dial. Manual moves stick — "
            "the daemon only acts on first-seen streams."
        )
        self.auto_route_check.toggled.connect(self._toggle_auto_route_browsers)
        layout.addWidget(self.auto_route_check)

        layout.addStretch(1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title("Overlay"))

        self.overlay_check = QCheckBox("Show overlay when dial is turned")
        self.overlay_check.setChecked(self.settings.get("overlay", True))
        self.overlay_check.toggled.connect(self._toggle_overlay)
        layout.addWidget(self.overlay_check)

        position_row = QHBoxLayout()
        pos_lbl = QLabel("Position")
        pos_lbl.setFixedWidth(70)
        self.position_combo = QComboBox()
        self.position_combo.addItems(list(_POSITION_DISPLAY.values()))
        current_pos = normalize_position(self.settings.get("overlay_position", "top-right"))
        idx = self.position_combo.findText(_POSITION_DISPLAY[current_pos])
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        self.position_combo.currentTextChanged.connect(self._change_position)
        position_row.addWidget(pos_lbl)
        position_row.addWidget(self.position_combo, 1)
        layout.addLayout(position_row)

        orient_row = QHBoxLayout()
        ori_lbl = QLabel("Style")
        ori_lbl.setFixedWidth(70)
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
        orient_row.addWidget(ori_lbl)
        orient_row.addWidget(self.orient_combo, 1)
        layout.addLayout(orient_row)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Startup"))

        self.autostart_check = QCheckBox("Start with system")
        self.autostart_check.setChecked(self.settings.get("autostart", True))
        self.autostart_check.toggled.connect(self._toggle_autostart)
        layout.addWidget(self.autostart_check)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Audio Profiles"))

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Saved:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(140)
        self._refresh_profile_combo()
        profile_row.addWidget(self.profile_combo, 1)
        layout.addLayout(profile_row)

        profile_btns = QHBoxLayout()
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load_selected_profile)
        save_btn = QPushButton("Save…")
        save_btn.clicked.connect(self._save_new_profile)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_selected_profile)
        profile_btns.addWidget(load_btn)
        profile_btns.addWidget(save_btn)
        profile_btns.addWidget(del_btn)
        layout.addLayout(profile_btns)

        profile_help = QLabel(
            "A profile snapshots overlay options + Media/HDMI sink toggles.\n"
            "Save the current setup, switch quickly, restore in one click."
        )
        profile_help.setStyleSheet("font-size: 10px; color: palette(placeholder-text); padding-top: 4px;")
        profile_help.setWordWrap(True)
        layout.addWidget(profile_help)

        layout.addStretch(1)
        return page

    def _make_bar(self, chunk_color: str) -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(100)
        bar.setTextVisible(True)
        bar.setFormat("%v%")
        bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; "
            "height: 22px; text-align: center; }"
            f"QProgressBar::chunk {{ background: {chunk_color}; border-radius: 3px; }}"
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
            "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; "
            "height: 22px; text-align: center; }"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}"
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
        # Best-effort: the setting is always persisted above; systemd-less
        # environments simply won't toggle autostart and that's fine.
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
            "Destroy the SteelMedia virtual sink"
            if enabled
            else "Create a SteelMedia virtual sink that bypasses the ChatMix dial"
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

    def _on_hdmi_sink_changed(self, enabled: bool):
        self._hdmi_sink_enabled = enabled
        self.hdmi_btn.setText("Remove HDMI" if enabled else "Add HDMI")
        self.hdmi_btn.setToolTip(
            "Destroy the SteelHDMI virtual sink"
            if enabled
            else "Create a SteelHDMI virtual sink that loops to your HDMI output"
        )

    def _toggle_hdmi_sink(self):
        cmd = "remove-hdmi-sink" if self._hdmi_sink_enabled else "add-hdmi-sink"
        self.daemon_client.send_command(cmd)
        self.hdmi_btn.setEnabled(False)
        self._hdmi_btn_reenable_timer()

    def _hdmi_btn_reenable_timer(self):
        from PySide6.QtCore import QTimer

        def reenable():
            self.hdmi_btn.setEnabled(True)

        QTimer.singleShot(600, reenable)

    def _on_auto_route_browsers_changed(self, enabled: bool):
        self._auto_route_browsers = enabled
        # Block the toggled signal so this echo doesn't re-send to the daemon.
        was_blocked = self.auto_route_check.blockSignals(True)
        self.auto_route_check.setChecked(enabled)
        self.auto_route_check.blockSignals(was_blocked)

    def _toggle_auto_route_browsers(self, checked: bool):
        self.daemon_client.send_command(
            "set-auto-route-browsers", enabled=bool(checked)
        )

    # -------------------------------------------------------------- profiles

    def _refresh_profile_combo(self):
        names = list_profiles(self.settings)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
        else:
            self.profile_combo.addItem("(no saved profiles)")
            self.profile_combo.setEnabled(False)
            return
        self.profile_combo.setEnabled(True)

    def _save_new_profile(self):
        name, ok = QInputDialog.getText(
            self, "Save profile", "Profile name:"
        )
        if not ok or not name.strip():
            return
        try:
            save_profile(
                self.settings,
                name.strip(),
                media_enabled=self._media_sink_enabled,
                hdmi_enabled=self._hdmi_sink_enabled,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid profile name", str(e))
            return
        self._refresh_profile_combo()
        idx = self.profile_combo.findText(name.strip())
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)

    def _load_selected_profile(self):
        name = self.profile_combo.currentText()
        if not name or name.startswith("("):
            return
        profile = load_profile(self.settings, name)
        if profile is None:
            return
        # Re-render the GUI controls from the (now updated) settings dict.
        self.overlay_check.setChecked(self.settings.get("overlay", True))
        current_pos = normalize_position(self.settings.get("overlay_position", "top-right"))
        idx = self.position_combo.findText(_POSITION_DISPLAY[current_pos])
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        idx = self.orient_combo.findText(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
        if idx >= 0:
            self.orient_combo.setCurrentIndex(idx)
        self.overlay.set_orientation(
            normalize_orientation(self.settings.get("overlay_orientation", "horizontal"))
        )

        # Apply daemon-side sink toggles via the existing socket commands so
        # the daemon's persisted state stays consistent with the profile.
        sinks = profile.get("sinks", {}) if isinstance(profile, dict) else {}
        want_media = bool(sinks.get("media", False))
        want_hdmi = bool(sinks.get("hdmi", False))
        if want_media != self._media_sink_enabled:
            self.daemon_client.send_command(
                "add-media-sink" if want_media else "remove-media-sink"
            )
        if want_hdmi != self._hdmi_sink_enabled:
            self.daemon_client.send_command(
                "add-hdmi-sink" if want_hdmi else "remove-hdmi-sink"
            )

    def _delete_selected_profile(self):
        name = self.profile_combo.currentText()
        if not name or name.startswith("("):
            return
        ok = QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        delete_profile(self.settings, name)
        self._refresh_profile_combo()

    # -------------------------------------------------------- update checker

    def _start_update_check(self):
        """Spawn the background update check on first show."""
        if getattr(self, "_update_checker", None) is not None:
            return
        self._update_checker = UpdateChecker(self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.no_update.connect(self._on_no_update)
        self._update_checker.no_release_found.connect(self._on_no_release_found)
        self._update_checker.failed.connect(self._on_update_failed)
        self.update_label.setText("Checking for updates…")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")
        self._update_checker.start()

    def _force_update_check(self):
        """Forced re-check from the user-visible button."""
        self._update_checker = None
        self.update_label.setText("Checking…")
        self._start_update_check()

    def _on_update_available(self, latest_tag: str, current_version: str):
        self.update_label.setText(
            f"Update available: {latest_tag} (you have {current_version})"
        )
        self.update_label.setStyleSheet("color: #FF9800; font-size: 10px; font-weight: bold;")

    def _on_no_update(self):
        self.update_label.setText("Up to date")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")

    def _on_no_release_found(self):
        # Reachable upstream but no version tag found — typical for repos
        # that haven't cut a release yet, or for forks. Different from
        # offline; don't blame the network.
        self.update_label.setText("No published release found")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")

    def _on_update_failed(self):
        self.update_label.setText("Update check failed (offline?)")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")

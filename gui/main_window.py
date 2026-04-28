"""Main SteelVoiceMix window — shell that hosts the tab widgets and
routes daemon events to them.

The heavy per-feature logic lives in `gui/tabs/*` modules. This file
keeps responsibility for the parts that genuinely belong to the window
itself: the persistent header (connection status), the tab strip, the
persistent footer (update checker + about), the system-tray plumbing,
and the cross-tab signal routing from the daemon client.
"""

from __future__ import annotations

import logging
import os
import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
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
    DISPLAY_NAME,
    load as load_settings,
    normalize_orientation,
    normalize_position,
)
from .tabs.equalizer import EqualizerTab
from .tabs.home import HomeTab
from .tabs.microphone import MicrophoneTab
from .tabs.settings import SettingsTab
from .tabs.sinks import SinksTab
from .tabs.surround import SurroundTab
from .update_checker import UpdateChecker
from .widgets import GLOBAL_QSS, app_icon

log = logging.getLogger(__name__)


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(DISPLAY_NAME)
        self.setFixedSize(480, 600)
        self.setWindowIcon(app_icon())
        self.setStyleSheet(GLOBAL_QSS)

        self.settings = load_settings()
        self.overlay = DialOverlay()
        self.overlay.set_orientation(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            )
        )

        self.signals = DaemonSignals()
        self.daemon_client = DaemonClient(self.signals)

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
        self._wire_daemon_signals()
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
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; padding: 4px;"
        )
        root.addWidget(self.status_label)

        # Tabs — instantiated as full widgets so they own their state and
        # handlers. The window only routes daemon events to them.
        self.home_tab = HomeTab()
        self.sinks_tab = SinksTab(self.daemon_client)
        self.eq_tab = EqualizerTab(self.daemon_client, self.settings)
        self.surround_tab = SurroundTab(self.daemon_client)
        self.mic_tab = MicrophoneTab(self.daemon_client)
        self.settings_tab = SettingsTab(self.settings, self.overlay, self.sinks_tab)

        tabs = QTabWidget()
        tabs.addTab(self.home_tab, "Home")
        tabs.addTab(self.sinks_tab, "Sinks")
        tabs.addTab(self.eq_tab, "Equalizer")
        tabs.addTab(self.surround_tab, "Surround")
        tabs.addTab(self.mic_tab, "Microphone")
        tabs.addTab(self.settings_tab, "Settings")
        root.addWidget(tabs, 1)

        # Persistent footer — update status + check-now + about, always visible.
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.update_label = QLabel("Up to date")
        self.update_label.setStyleSheet(
            "color: palette(placeholder-text); font-size: 10px;"
        )
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

    # -------------------------------------------------------- daemon routing

    def _wire_daemon_signals(self) -> None:
        """Connect each daemon signal to the right tab method (or to a
        local handler for window-chrome things like the status label).

        Keeping the routing flat in one place means a new event type only
        needs one new line here; new tabs only need their constructor and
        their slot methods, not signal-bookkeeping changes."""
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.status_message.connect(self._on_status_message)

        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.battery_updated.connect(self.home_tab.on_battery)
        self.signals.battery_updated.connect(self._on_battery_for_tray)

        self.signals.media_sink_changed.connect(self.sinks_tab.on_media_changed)
        self.signals.hdmi_sink_changed.connect(self.sinks_tab.on_hdmi_changed)
        # The EQ tab also tracks sink state so it can show / hide the
        # Media + HDMI rows in the channel combo dynamically.
        self.signals.media_sink_changed.connect(self.eq_tab.on_media_sink_changed)
        self.signals.hdmi_sink_changed.connect(self.eq_tab.on_hdmi_sink_changed)
        self.signals.auto_route_browsers_changed.connect(
            self.sinks_tab.on_auto_route_changed
        )

        self.signals.eq_enabled_changed.connect(self.eq_tab.on_enabled_changed)
        self.signals.eq_bands_changed.connect(self.eq_tab.on_bands_changed)
        self.signals.eq_full_state.connect(self.eq_tab.on_full_state)

        self.signals.surround_enabled_changed.connect(
            self.surround_tab.on_enabled_changed
        )
        self.signals.surround_hrir_changed.connect(
            self.surround_tab.on_hrir_changed
        )

    # ----------------------------------------------------- header + chatmix

    def _on_connected(self) -> None:
        self.status_label.setText("🟢 Connected — ChatMix Active")
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #4CAF50;"
        )

    def _on_disconnected(self) -> None:
        self.status_label.setText("🔴 Disconnected — Reconnecting...")
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #f44336;"
        )
        self.home_tab.on_disconnected()

    def _on_status_message(self, msg: str) -> None:
        self.status_label.setText(msg)

    def _on_battery_for_tray(self, level: int, status: str) -> None:
        if self.has_tray:
            self.tray.setToolTip(f"{DISPLAY_NAME} — 🔋 {level}% ({status})")

    def _on_chatmix(self, game_vol: int, chat_vol: int) -> None:
        self.home_tab.on_chatmix(game_vol, chat_vol)
        # Tray tooltip carries the latest battery state, set in HomeTab.
        # Overlay positioning is window-level state — keep it here.
        if self.settings.get("overlay", True):
            pos = normalize_position(
                self.settings.get("overlay_position", "top-right")
            )
            self.overlay.show_volumes(game_vol, chat_vol, pos)

    # ------------------------------------------------------------------- tray

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
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

    def _tray_clicked(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._show_window()

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
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

    def _quit(self) -> None:
        self.daemon_client.stop()
        QApplication.quit()

    # -------------------------------------------------------------- daemon

    def _start_daemon_client(self) -> None:
        self.daemon_thread = threading.Thread(
            target=self.daemon_client.run, daemon=True
        )
        self.daemon_thread.start()

    # ------------------------------------------------------------------- about

    def _show_about(self) -> None:
        dialog = make_about_dialog(self)
        dialog.exec()

    # -------------------------------------------------------- update checker

    def _start_update_check(self) -> None:
        """Spawn the background update check on first show."""
        if getattr(self, "_update_checker", None) is not None:
            return
        self._update_checker = UpdateChecker(self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.no_update.connect(self._on_no_update)
        self._update_checker.no_release_found.connect(self._on_no_release_found)
        self._update_checker.failed.connect(self._on_update_failed)
        self.update_label.setText("Checking for updates…")
        self.update_label.setStyleSheet(
            "color: palette(placeholder-text); font-size: 10px;"
        )
        self._update_checker.start()

    def _force_update_check(self) -> None:
        """Forced re-check from the user-visible button."""
        self._update_checker = None
        self.update_label.setText("Checking…")
        self._start_update_check()

    def _on_update_available(self, latest_tag: str, current_version: str) -> None:
        self.update_label.setText(
            f"Update available: {latest_tag} (you have {current_version})"
        )
        self.update_label.setStyleSheet(
            "color: #FF9800; font-size: 10px; font-weight: bold;"
        )

    def _on_no_update(self) -> None:
        self.update_label.setText("Up to date")
        self.update_label.setStyleSheet(
            "color: palette(placeholder-text); font-size: 10px;"
        )

    def _on_no_release_found(self) -> None:
        # Reachable upstream but no version tag found — typical for repos
        # that haven't cut a release yet, or for forks. Different from
        # offline; don't blame the network.
        self.update_label.setText("No published release found")
        self.update_label.setStyleSheet(
            "color: palette(placeholder-text); font-size: 10px;"
        )

    def _on_update_failed(self) -> None:
        self.update_label.setText("Update check failed (offline?)")
        self.update_label.setStyleSheet(
            "color: palette(placeholder-text); font-size: 10px;"
        )

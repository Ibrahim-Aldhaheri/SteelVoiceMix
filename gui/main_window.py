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

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QSystemTrayIcon,
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
        # Window grew to give EQ sliders room to breathe and to leave
        # space for future controls (ASM preset import, default HRIR
        # toggle, etc.). Tabs are wrapped in QScrollArea so that even on
        # smaller screens the content can scroll instead of getting
        # crushed — the EQ tab in particular has fixed-height slider
        # columns that would otherwise pressure the layout.
        self.setFixedSize(880, 720)
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

        # Persistent header — connection status as a coloured pill so
        # the daemon-link state is unmistakable. The pill's `state`
        # property drives the QSS colour (ok = green, bad = red,
        # default = neutral).
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel(DISPLAY_NAME)
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        self.status_label = QLabel("🔍  Connecting…")
        self.status_label.setObjectName("status-pill")
        self.status_label.setProperty("state", "")
        self.status_label.setAlignment(Qt.AlignCenter)
        header.addWidget(self.status_label, 0, alignment=Qt.AlignVCenter)
        root.addLayout(header)

        # Tabs — instantiated as full widgets so they own their state and
        # handlers. The window only routes daemon events to them.
        self.home_tab = HomeTab()
        self.sinks_tab = SinksTab(self.daemon_client)
        self.eq_tab = EqualizerTab(self.daemon_client, self.settings)
        self.surround_tab = SurroundTab(self.daemon_client)
        self.mic_tab = MicrophoneTab(self.daemon_client)
        self.settings_tab = SettingsTab(self.settings, self.overlay, self.sinks_tab)

        # Sidebar nav: a vertical QListWidget on the left that drives a
        # QStackedWidget on the right. With six pages, top tabs ran out
        # of horizontal room and felt cramped; the sidebar gives each
        # entry generous padding plus an emoji icon.
        self.nav = QListWidget()
        self.nav.setObjectName("sidebar")
        self.nav.setFixedWidth(168)
        self.nav.setIconSize(QSize(20, 20))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stack = QStackedWidget()

        for label, widget in (
            ("🏠   Home", self.home_tab),
            ("🔊   Sinks", self.sinks_tab),
            ("🎛   Equalizer", self.eq_tab),
            ("🎬   Surround", self.surround_tab),
            ("🎙   Microphone", self.mic_tab),
            ("⚙   Settings", self.settings_tab),
        ):
            item = QListWidgetItem(label)
            self.nav.addItem(item)
            # Each tab page goes into a scroll area — Qt's QStackedWidget
            # gives every page the same fixed slot, and tabs vary widely
            # in content size (Equalizer is the tallest by far). Wrapping
            # in QScrollArea means a tab that wants more height gets a
            # scroll bar instead of pushing siblings off the bottom.
            scroller = QScrollArea()
            scroller.setWidget(widget)
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QScrollArea.NoFrame)
            scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.stack.addWidget(scroller)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

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
        self._set_status_pill("●  Connected", "ok")

    def _on_disconnected(self) -> None:
        self._set_status_pill("●  Reconnecting…", "bad")
        self.home_tab.on_disconnected()

    def _on_status_message(self, msg: str) -> None:
        # Free-form status (e.g. "Connecting to daemon…") — neutral pill,
        # no colour. The connected/disconnected handlers later replace it
        # with the proper coloured state.
        self._set_status_pill(msg, "")

    def _set_status_pill(self, text: str, state: str) -> None:
        """Update the header status pill's text and `state` property.
        QSS reads the property to drive the colour, so a `polish` round
        is needed after the property changes for the new style to apply."""
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        # Re-evaluate the stylesheet so the new property selector takes
        # effect — Qt doesn't restyle automatically when properties
        # change at runtime.
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

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
        # Only show the minimize-to-tray toast when the user has
        # explicitly opted in via Settings — this was the most-flagged
        # annoyance: a toast on every X-button click adds up fast.
        if self.settings.get("notify_minimize_hint", False):
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

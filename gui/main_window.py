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
from PySide6.QtGui import QAction, QKeySequence, QShortcut
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
from .hrir_default import bundled_default_path, has_default
from .overlay import DialOverlay
from .settings import (
    DISPLAY_NAME,
    load as load_settings,
    normalize_orientation,
    normalize_position,
    save as save_settings,
)
from .theme import apply_theme, normalize_mode
from .tabs.equalizer import EqualizerTab
from .tabs.home import HomeTab
from .tabs.microphone import MicrophoneTab
from .game_eq import GameProfileManager, GameWatcher
from .voice_test import VoiceTestService
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
        # Responsive window: minimum size keeps the layout readable
        # but the user can drag larger. Tabs are wrapped in
        # QScrollArea so the EQ tab's fixed-height slider columns
        # don't get crushed if the window goes smaller than the
        # content.
        self.setMinimumSize(820, 660)
        self.resize(900, 740)
        self.setWindowIcon(app_icon())
        self.setStyleSheet(GLOBAL_QSS)

        self.settings = load_settings()
        # Apply the persisted theme BEFORE the window builds so first
        # paint already reflects the user's choice. Auto follows the
        # OS via QStyleHints.colorScheme() (Qt 6.5+).
        apply_theme(normalize_mode(self.settings.get("theme_mode", "auto")))
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
        self._install_default_sink_shortcut()

        # Universal cleanup: aboutToQuit fires for every exit path
        # (tray Quit action, X button when no tray, SIGTERM, SIGINT,
        # programmatic QApplication.quit). Without this, app.py's
        # SIGTERM handler called QApplication.quit() directly and
        # bypassed _quit(), leaving the GameWatcher QThread running
        # when the app exited — Qt aborted with "QThread destroyed
        # while still running" (visible in the KDE crash dialog).
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cleanup_on_quit)

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
        self.status_label = QLabel(self.tr("🔍  Connecting…"))
        self.status_label.setObjectName("status-pill")
        self.status_label.setProperty("state", "")
        self.status_label.setAlignment(Qt.AlignCenter)
        header.addWidget(self.status_label, 0, alignment=Qt.AlignVCenter)
        root.addLayout(header)

        # Tabs — instantiated as full widgets so they own their state and
        # handlers. The window only routes daemon events to them.
        # Shared voice-test service — owns the pw-loopback subprocess
        # so the Microphone tab AND the Equalizer tab (Mic channel)
        # can both drive the same "Hear yourself" toggle in sync.
        self.voice_test = VoiceTestService(self)
        self.home_tab = HomeTab(self.daemon_client)
        self.sinks_tab = SinksTab(self.daemon_client)
        self.surround_tab = SurroundTab(self.daemon_client)
        self.mic_tab = MicrophoneTab(
            self.daemon_client, self.settings, self.voice_test,
        )
        # EqualizerTab hosts the Auto Game-EQ card at the bottom and
        # needs the GameProfileManager to drive it. Manager needs
        # the EQ tab's per-channel bands cache for snapshots, so we
        # resolve the chicken-and-egg by building the EQ tab without
        # the manager first, then patching it in once both exist.
        self.eq_tab = EqualizerTab(
            self.daemon_client, self.settings,
            voice_test=self.voice_test,
        )
        self.game_eq_manager = GameProfileManager(
            self.daemon_client, self.settings, self.eq_tab._bands_by_channel
        )
        self.eq_tab._game_eq_manager = self.game_eq_manager
        self.eq_tab._game_eq_manager.detected_changed.connect(
            self.eq_tab._on_detected_changed
        )
        self.eq_tab._game_eq_manager.applied_changed.connect(
            self.eq_tab._on_auto_applied
        )
        self.eq_tab._game_eq_manager.bands_to_load.connect(
            self.eq_tab._on_auto_bands_load
        )
        self.settings_tab = SettingsTab(
            self.settings, self.overlay, self.sinks_tab, self.daemon_client,
            eq_tab=self.eq_tab, mic_tab=self.mic_tab,
        )

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
            (self.tr("🏠   Home"), self.home_tab),
            (self.tr("🔊   Sinks"), self.sinks_tab),
            (self.tr("🎛   Equalizer"), self.eq_tab),
            (self.tr("🎬   Surround"), self.surround_tab),
            (self.tr("🎙   Microphone"), self.mic_tab),
            (self.tr("⚙   Settings"), self.settings_tab),
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
        self.update_label = QLabel(self.tr("Up to date"))
        self.update_label.setStyleSheet(
            "color: palette(placeholder-text); font-size: 10px;"
        )
        update_btn = QPushButton(self.tr("Check for updates"))
        update_btn.setFlat(True)
        update_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        update_btn.clicked.connect(self._force_update_check)
        self.about_btn = QPushButton(self.tr("About…"))
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
        # Home dashboard mirrors the same toggles in its status pills.
        self.signals.media_sink_changed.connect(self.home_tab.on_media_enabled)
        self.signals.hdmi_sink_changed.connect(self.home_tab.on_hdmi_enabled)
        self.signals.auto_route_browsers_changed.connect(
            self.sinks_tab.on_auto_route_changed
        )

        self.signals.eq_enabled_changed.connect(self.eq_tab.on_enabled_changed)
        self.signals.eq_enabled_changed.connect(self.home_tab.on_eq_enabled)
        self.signals.eq_bands_changed.connect(self.eq_tab.on_bands_changed)
        self.signals.eq_full_state.connect(self.eq_tab.on_full_state)

        self.signals.surround_enabled_changed.connect(
            self.surround_tab.on_enabled_changed
        )
        self.signals.surround_enabled_changed.connect(
            self.home_tab.on_surround_enabled
        )
        self.signals.surround_hrir_changed.connect(
            self.surround_tab.on_hrir_changed
        )
        # First-run auto-apply: send the bundled HRIR to the daemon if
        # the user hasn't picked one yet. Hook into the same hrir-
        # changed signal so we only act after the daemon's initial
        # Status reaches us; the marker in settings.json prevents the
        # auto-apply from firing more than once across launches.
        self.signals.surround_hrir_changed.connect(
            self._maybe_apply_default_hrir
        )

        self.signals.mic_state_changed.connect(self.mic_tab.on_mic_state_changed)
        self.signals.mic_state_changed.connect(self.home_tab.on_mic_state)

        # Game-EQ watcher (manager already built above so SettingsTab
        # can wire to its detected_changed signal). Watcher polls
        # pactl in a background thread and emits a snapshot dict
        # every time the active-clients list changes.
        self.game_watcher = GameWatcher()
        self.game_watcher.games_changed.connect(
            self.game_eq_manager.on_games_changed
        )
        self.game_watcher.start()
        self.signals.mic_default_source_changed.connect(
            self.mic_tab.on_mic_default_source_changed
        )
        self.signals.sidetone_changed.connect(self.mic_tab.on_sidetone_changed)
        self.signals.notifications_enabled_changed.connect(
            self.settings_tab.on_daemon_notifications_changed
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

    def _maybe_apply_default_hrir(self, current_path: str) -> None:
        """One-shot first-run hook: if the daemon reports no HRIR path
        yet AND we have the bundled default available AND we've never
        auto-applied before, send the bundled path so surround can
        come up out of the box. The settings marker
        (`surround_default_applied`) ensures this fires at most once;
        if the user later clears the path, they stay cleared."""
        if self.settings.get("surround_default_applied", False):
            return
        if current_path:
            # Daemon already has a path (persisted from a previous run
            # or set by another GUI instance). Mark as applied and
            # never auto-act again.
            self.settings["surround_default_applied"] = True
            save_settings(self.settings)
            return
        if not has_default():
            # Broken install — bundled WAV is missing. Don't keep
            # retrying, but don't mark applied either: a reinstall
            # might fix it.
            return
        path = str(bundled_default_path())
        self.daemon_client.send_command("set-surround-hrir", path=path)
        self.settings["surround_default_applied"] = True
        save_settings(self.settings)

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

    def _install_default_sink_shortcut(self) -> None:
        """Bind the configured key sequence to gui.sink_cycle.
        Context = ApplicationShortcut so the shortcut fires while
        ANY of our windows has focus (main window, tray menu open,
        etc.) — not just the main window. Genuine system-wide
        hotkeys (firing while the game has focus) need
        steelvoicemix-cli bound in the DE's keyboard settings."""
        # Always tear down the previous shortcut first so re-installs
        # on toggle or combo change don't leak.
        old = getattr(self, "_default_sink_shortcut", None)
        if old is not None:
            try:
                old.setEnabled(False)
                old.deleteLater()
            except Exception:
                pass
            self._default_sink_shortcut = None
        if not self.settings.get("default_sink_cycle_enabled", False):
            return
        combo = self.settings.get("default_sink_cycle_combo", "Ctrl+Shift+S")
        seq = QKeySequence(combo)
        if seq.isEmpty():
            return
        # Owned by self so the shortcut is GC-safe and lives as long
        # as the window does.
        self._default_sink_shortcut = QShortcut(seq, self)
        self._default_sink_shortcut.setContext(Qt.ApplicationShortcut)
        self._default_sink_shortcut.activated.connect(
            self._on_default_sink_shortcut
        )

    def reload_default_sink_shortcut(self) -> None:
        """Public re-entry point used by SettingsTab when the user
        toggles the feature on/off or changes the combo. Tears down
        and rebuilds the QShortcut so the new state takes effect
        without restarting the app."""
        self._install_default_sink_shortcut()

    def _on_default_sink_shortcut(self) -> None:
        from .sink_cycle import cycle_default_sink
        prev, new = cycle_default_sink(
            exclude=self.settings.get("default_sink_cycle_exclude") or [],
        )
        if not new:
            self._show_tray_message(
                "Cycle default sink failed",
                "No SteelVoiceMix sinks loaded.",
            )
        else:
            self._show_tray_message(
                "Default sink",
                f"{new}" if new == prev else f"{prev or '?'} → {new}",
            )

    def _show_tray_message(self, title: str, body: str) -> None:
        """Best-effort tray toast — silently no-op if no tray exists."""
        if getattr(self, "tray", None) is None:
            return
        try:
            self.tray.showMessage(title, body, msecs=2000)
        except Exception:
            pass

    def _quit(self) -> None:
        # User-driven quit: ask Qt to exit. aboutToQuit will fire
        # _cleanup_on_quit which actually stops the threads.
        QApplication.quit()

    def _cleanup_on_quit(self) -> None:
        """Bound to QApplication.aboutToQuit. Idempotent — safe to
        run multiple times."""
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True
        # Stop the voice-test loopback if it was left on so a
        # restart of the app doesn't leave an orphaned subprocess.
        try:
            self.voice_test.stop()
        except Exception:
            pass
        # Stop the game watcher's poll loop so the QThread joins
        # cleanly before the QApplication exits. Without this, Qt
        # aborts with "QThread: Destroyed while thread is still
        # running" — that's the SIGABRT users saw on systemd stop.
        try:
            self.game_watcher.stop()
            self.game_watcher.wait(2000)
        except Exception:
            pass
        try:
            self.daemon_client.stop()
        except Exception:
            pass

    # -------------------------------------------------------------- daemon

    def _start_daemon_client(self) -> None:
        self.daemon_thread = threading.Thread(
            target=self.daemon_client.run, daemon=True
        )
        self.daemon_thread.start()

    # ------------------------------------------------------------------- about

    def _show_about(self) -> None:
        # Cache the dialog instance — rebuilding it every click costs
        # noticeable time on first paint (icon theme lookup + WM
        # appearance handshake). Lazy-init keeps users who never open
        # About from paying any cost at all.
        if getattr(self, "_about_dialog", None) is None:
            self._about_dialog = make_about_dialog(self)
        self._about_dialog.exec()

    # -------------------------------------------------------- update checker

    def _start_update_check(self, *, force: bool = False) -> None:
        """Spawn the background update check. When `force=True` the
        checker bypasses the 24-hour on-disk cache and queries GitHub
        every time — used by the manual 'Check for updates' button so
        it actually checks instead of replaying yesterday's answer."""
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
        if force:
            self._update_checker.force_check()
        else:
            self._update_checker.start()

    def _force_update_check(self) -> None:
        """Forced re-check from the user-visible button. Wipes the
        cache before querying so the result reflects what's on GitHub
        right now, not whatever was cached up to 24 h ago."""
        self._update_checker = None
        self.update_label.setText("Checking…")
        self._start_update_check(force=True)

    def _on_update_available(self, latest_tag: str, current_version: str) -> None:
        self.update_label.setText(
            f"Update available: {latest_tag} (you have {current_version})"
        )
        self.update_label.setStyleSheet(
            "color: #FF9800; font-size: 10px; font-weight: bold;"
        )

    def _on_no_update(self) -> None:
        self.update_label.setText(self.tr("Up to date"))
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

"""Settings tab — overlay options, autostart, audio profiles."""

from __future__ import annotations

import logging
import subprocess

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


_ALPHA_REPO = "abokhalil/steelvoicemix-dev"
_STABLE_REPO = "abokhalil/steelvoicemix"

_ALPHA_ENABLE_CMD = (
    f"sudo dnf copr enable {_ALPHA_REPO} -y && "
    "sudo dnf install steelvoicemix-dev -y"
)
_ALPHA_DISABLE_CMD = (
    "sudo dnf swap steelvoicemix-dev steelvoicemix -y && "
    f"sudo dnf copr disable {_ALPHA_REPO} -y"
)

from ..settings import (
    APP_NAME,
    OVERLAY_ORIENTATIONS,
    OVERLAY_POSITIONS,
    delete_profile,
    list_profiles,
    load_profile,
    normalize_orientation,
    normalize_position,
    reset_to_defaults_preserving_profiles,
    save as save_settings,
    save_profile,
)
from ..theme import THEME_MODES, apply_theme, normalize_mode
from ..widgets import POSITION_DISPLAY, card, labelled_toggle

log = logging.getLogger(__name__)


class SettingsTab(QWidget):
    def __init__(self, settings: dict, overlay, sinks_tab, daemon_client, parent=None):
        """`overlay` is the DialOverlay instance. `sinks_tab` is the
        SinksTab — needed to apply Media/HDMI toggles when a profile
        loads. `daemon_client` is needed by the Reset button to issue
        a daemon-side `reset-state` alongside the GUI's own wipe."""
        super().__init__(parent)
        self._settings = settings
        self._overlay = overlay
        self._sinks_tab = sinks_tab
        self._daemon = daemon_client
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Overlay card -------------------------------------------------
        overlay_row, self.overlay_toggle = labelled_toggle(
            "Show overlay when dial is turned"
        )
        self.overlay_toggle.setChecked(self._settings.get("overlay", True))
        self.overlay_toggle.toggled.connect(self._toggle_overlay)

        position_row = QHBoxLayout()
        pos_lbl = QLabel("Position")
        pos_lbl.setFixedWidth(80)
        self.position_combo = QComboBox()
        self.position_combo.addItems(list(POSITION_DISPLAY.values()))
        current_pos = normalize_position(
            self._settings.get("overlay_position", "top-right")
        )
        idx = self.position_combo.findText(POSITION_DISPLAY[current_pos])
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        self.position_combo.currentTextChanged.connect(self._change_position)
        position_row.addWidget(pos_lbl)
        position_row.addWidget(self.position_combo, 1)

        orient_row = QHBoxLayout()
        ori_lbl = QLabel("Style")
        ori_lbl.setFixedWidth(80)
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Horizontal", "Vertical"])
        idx = self.orient_combo.findText(
            normalize_orientation(
                self._settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
        if idx >= 0:
            self.orient_combo.setCurrentIndex(idx)
        self.orient_combo.currentTextChanged.connect(self._change_orientation)
        orient_row.addWidget(ori_lbl)
        orient_row.addWidget(self.orient_combo, 1)

        layout.addWidget(card("Overlay", overlay_row, position_row, orient_row))

        # Appearance card ---------------------------------------------
        # Auto = follow the system colour scheme (Qt 6.5+ honours the
        # XDG portal hint). Light/Dark force our packaged palettes.
        # Implementation lives in gui/theme.py — swapping QPalette
        # propagates everywhere the QSS uses palette(...) refs.
        theme_row = QHBoxLayout()
        theme_lbl = QLabel("Theme")
        theme_lbl.setFixedWidth(80)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Auto (system)", "Light", "Dark"])
        current_theme = normalize_mode(self._settings.get("theme_mode", "auto"))
        self.theme_combo.setCurrentIndex(
            {"auto": 0, "light": 1, "dark": 2}.get(current_theme, 0)
        )
        self.theme_combo.currentIndexChanged.connect(self._change_theme)
        theme_row.addWidget(theme_lbl)
        theme_row.addWidget(self.theme_combo, 1)

        theme_help = QLabel(
            "Auto follows your desktop's light / dark setting. Pick "
            "Light or Dark to override."
        )
        theme_help.setWordWrap(True)
        theme_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(card("Appearance", theme_row, theme_help))

        # Startup card -------------------------------------------------
        autostart_row, self.autostart_toggle = labelled_toggle(
            "Start with system"
        )
        self.autostart_toggle.setChecked(self._settings.get("autostart", True))
        self.autostart_toggle.toggled.connect(self._toggle_autostart)

        start_min_row, self.start_min_toggle = labelled_toggle(
            "Start minimised to system tray",
            tooltip=(
                "When enabled, the app launches hidden in the tray "
                "instead of opening its window. Click the tray icon "
                "to bring the window up. Ignored on sessions without "
                "a system tray (the app shows normally)."
            ),
        )
        self.start_min_toggle.setChecked(
            self._settings.get("start_minimized", False)
        )
        self.start_min_toggle.toggled.connect(self._toggle_start_minimized)

        layout.addWidget(card("Startup", autostart_row, start_min_row))

        # Notifications card -------------------------------------------
        # Two distinct toggles:
        #   - Minimize-to-tray toast: GUI-side (closeEvent in main
        #     window). Off by default — was the most-flagged annoyance
        #     in the v0.2.x era.
        #   - Daemon connect / disconnect notifications: emitted by
        #     the Rust daemon via notify-send when the headset comes
        #     up or drops. On by default (matches the legacy
        #     --no-notify-as-only-control behaviour).
        minimize_row, self.minimize_toggle = labelled_toggle(
            "Show toast when minimised to tray",
            tooltip=(
                "When the window is closed with the X button, it hides "
                "to the system tray. Enable this to see a confirmation "
                "toast every time that happens. Off by default."
            ),
        )
        self.minimize_toggle.setChecked(
            self._settings.get("notify_minimize_hint", False)
        )
        self.minimize_toggle.toggled.connect(self._toggle_minimize_hint)

        daemon_notif_row, self.daemon_notif_toggle = labelled_toggle(
            "Show 🎧 connect / disconnect notifications",
            tooltip=(
                "Desktop notifications emitted by the daemon when the "
                "base station connects or drops. Distinct from the "
                "minimize-to-tray toast above — those are GUI-side."
            ),
        )
        # Default on, mirrors the daemon's default. The first
        # mic-state / Status snapshot from the daemon will correct it
        # if the user persisted a different value.
        self.daemon_notif_toggle.setChecked(True)
        self.daemon_notif_toggle.toggled.connect(self._toggle_daemon_notifs)

        layout.addWidget(card("Notifications", minimize_row, daemon_notif_row))

        # Profiles card ------------------------------------------------
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Saved:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(140)
        self._refresh_profile_combo()
        profile_row.addWidget(self.profile_combo, 1)

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

        profile_help = QLabel(
            "A profile snapshots overlay options + Media/HDMI sink toggles. "
            "Save the current setup, switch quickly, restore in one click."
        )
        profile_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        profile_help.setWordWrap(True)

        layout.addWidget(
            card("Audio Profiles", profile_row, profile_btns, profile_help)
        )

        # Alpha channel card -------------------------------------------
        # The GUI can't actually run sudo, so this card just shows the
        # commands and a "Copy to clipboard" button. Users paste into
        # a terminal. Keeps the UX honest about the elevation step
        # while still being more discoverable than burying the info
        # in a README.
        # Title row hosts a coloured "ALPHA" pill so the user can
        # tell at a glance this card is the experimental one — the
        # rest of the Settings tab is stable preferences.
        alpha_title_row = QHBoxLayout()
        alpha_title = QLabel("Alpha Channel")
        alpha_title.setStyleSheet("font-size: 12px; font-weight: bold;")
        alpha_pill = QLabel("ALPHA")
        alpha_pill.setStyleSheet(
            "background: #FF9800; color: white; "
            "font-size: 9px; font-weight: bold; "
            "padding: 2px 6px; border-radius: 8px;"
        )
        alpha_title_row.addWidget(alpha_title)
        alpha_title_row.addWidget(alpha_pill)
        alpha_title_row.addStretch(1)

        alpha_btns = QHBoxLayout()
        self.alpha_enable_btn = QPushButton("📋 Switch to alpha")
        self.alpha_enable_btn.setToolTip(
            "Copy the dnf commands that swap to the alpha COPR repo."
        )
        self.alpha_enable_btn.clicked.connect(self._copy_alpha_enable)
        self.alpha_disable_btn = QPushButton("📋 Back to stable")
        self.alpha_disable_btn.setToolTip(
            "Copy the dnf commands that switch back to the stable repo."
        )
        self.alpha_disable_btn.clicked.connect(self._copy_alpha_disable)
        alpha_btns.addWidget(self.alpha_enable_btn)
        alpha_btns.addWidget(self.alpha_disable_btn)
        alpha_btns.addStretch(1)

        alpha_help = QLabel(
            "Alpha builds rebuild from the dev branch on every commit "
            "— bleeding-edge features, but expect rough edges. "
            "Clicking either button copies the dnf commands to your "
            "clipboard; paste into a terminal to actually switch. "
            "After the swap, run `sudo dnf upgrade steelvoicemix` to "
            "pull the new build. Going back to stable downgrades "
            "cleanly via dnf."
        )
        alpha_help.setWordWrap(True)
        alpha_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        # Title is bolted in via the row above so we pass title=None to
        # card() and add our custom title row as the first child.
        layout.addWidget(card(None, alpha_title_row, alpha_btns, alpha_help))

        # Reset card --------------------------------------------------
        reset_row = QHBoxLayout()
        self.reset_btn = QPushButton("Reset to defaults…")
        self.reset_btn.setToolTip(
            "Reset every preference (overlay, sinks, EQ, surround, "
            "notification toggles) to its default value. Saved audio "
            "profiles are preserved."
        )
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        reset_row.addWidget(self.reset_btn)
        reset_row.addStretch(1)

        reset_help = QLabel(
            "Wipes overlay options, autostart, notification prefs, EQ "
            "state, surround state, and the Media / HDMI sink toggles "
            "back to their factory defaults. Saved audio profiles are "
            "kept — delete them individually above if you want them "
            "gone too."
        )
        reset_help.setWordWrap(True)
        reset_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(card("Reset", reset_row, reset_help))

        layout.addStretch(1)

    # --------------------------------------------------------------- handlers

    def _toggle_overlay(self, checked: bool) -> None:
        self._settings["overlay"] = checked
        save_settings(self._settings)

    def _change_position(self, text: str) -> None:
        key = text.lower().replace(" ", "-")
        if key not in OVERLAY_POSITIONS:
            return
        self._settings["overlay_position"] = key
        save_settings(self._settings)
        # Trigger a preview-position render via the overlay's last-known
        # values. We reach across into the host window via the overlay's
        # own state — chatmix events will resnap on the next dial turn.
        self._overlay.show_volumes(
            getattr(self._overlay, "game_vol", 100),
            getattr(self._overlay, "chat_vol", 100),
            key,
        )

    def _change_orientation(self, text: str) -> None:
        key = text.lower()
        if key not in OVERLAY_ORIENTATIONS:
            return
        self._settings["overlay_orientation"] = key
        save_settings(self._settings)
        self._overlay.set_orientation(key)
        self._overlay.show_volumes(
            getattr(self._overlay, "game_vol", 100),
            getattr(self._overlay, "chat_vol", 100),
            normalize_position(self._settings.get("overlay_position", "top-right")),
        )

    def _toggle_minimize_hint(self, checked: bool) -> None:
        self._settings["notify_minimize_hint"] = checked
        save_settings(self._settings)

    def _toggle_start_minimized(self, checked: bool) -> None:
        self._settings["start_minimized"] = checked
        save_settings(self._settings)


    def _change_theme(self, index: int) -> None:
        mode = ("auto", "light", "dark")[index] if 0 <= index <= 2 else "auto"
        if mode not in THEME_MODES:
            mode = "auto"
        self._settings["theme_mode"] = mode
        save_settings(self._settings)
        apply_theme(mode)

    def _copy_to_clipboard(self, text: str, label: str) -> None:
        cb = QApplication.clipboard()
        cb.setText(text)
        QMessageBox.information(
            self,
            "Copied",
            f"{label} commands copied to clipboard. Paste into a "
            "terminal:\n\n" + text,
        )

    def _copy_alpha_enable(self) -> None:
        self._copy_to_clipboard(_ALPHA_ENABLE_CMD, "Alpha-enable")

    def _copy_alpha_disable(self) -> None:
        self._copy_to_clipboard(_ALPHA_DISABLE_CMD, "Stable-restore")

    def _toggle_daemon_notifs(self, checked: bool) -> None:
        # Pure daemon-side state — we just send the command and let
        # the broadcast event refresh the toggle. The daemon owns
        # persistence here (in daemon.json), not settings.json.
        self._daemon.send_command("set-notifications-enabled", enabled=checked)

    def on_daemon_notifications_changed(self, enabled: bool) -> None:
        """Daemon broadcast: the connect / disconnect notification
        toggle changed. Re-apply with signals blocked so the echo
        doesn't loop back as another set-notifications-enabled."""
        was_blocked = self.daemon_notif_toggle.blockSignals(True)
        try:
            self.daemon_notif_toggle.setChecked(enabled)
        finally:
            self.daemon_notif_toggle.blockSignals(was_blocked)

    def _toggle_autostart(self, checked: bool) -> None:
        self._settings["autostart"] = checked
        save_settings(self._settings)
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

    # ---------------------------------------------------------------- profiles

    def _refresh_profile_combo(self) -> None:
        names = list_profiles(self._settings)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
            self.profile_combo.setEnabled(True)
        else:
            self.profile_combo.addItem("(no saved profiles)")
            self.profile_combo.setEnabled(False)

    def _save_new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Save profile", "Profile name:")
        if not ok or not name.strip():
            return
        try:
            save_profile(
                self._settings,
                name.strip(),
                media_enabled=self._sinks_tab.media_enabled,
                hdmi_enabled=self._sinks_tab.hdmi_enabled,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid profile name", str(e))
            return
        self._refresh_profile_combo()
        idx = self.profile_combo.findText(name.strip())
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)

    def _load_selected_profile(self) -> None:
        name = self.profile_combo.currentText()
        if not name or name.startswith("("):
            return
        profile = load_profile(self._settings, name)
        if profile is None:
            return
        # Re-render the GUI controls from the (now updated) settings dict.
        self.overlay_toggle.setChecked(self._settings.get("overlay", True))
        current_pos = normalize_position(
            self._settings.get("overlay_position", "top-right")
        )
        idx = self.position_combo.findText(POSITION_DISPLAY[current_pos])
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        idx = self.orient_combo.findText(
            normalize_orientation(
                self._settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
        if idx >= 0:
            self.orient_combo.setCurrentIndex(idx)
        self._overlay.set_orientation(
            normalize_orientation(
                self._settings.get("overlay_orientation", "horizontal")
            )
        )

        # Apply daemon-side sink toggles via the existing socket commands so
        # the daemon's persisted state stays consistent with the profile.
        sinks = profile.get("sinks", {}) if isinstance(profile, dict) else {}
        self._sinks_tab.apply_profile(
            want_media=bool(sinks.get("media", False)),
            want_hdmi=bool(sinks.get("hdmi", False)),
        )

    # ------------------------------------------------------------------ reset

    def _on_reset_clicked(self) -> None:
        """Confirm + execute a full preferences reset. Daemon-side
        runtime state is wiped via the `reset-state` command; GUI-side
        settings.json is reset to DEFAULTS in-place but the user's
        saved audio profiles are explicitly preserved."""
        confirm = QMessageBox.warning(
            self,
            "Reset to defaults",
            "This will reset every preference back to its default:\n\n"
            "  • Overlay options + autostart\n"
            "  • Notification toggles\n"
            "  • EQ state (sliders + per-channel tunings)\n"
            "  • Surround on/off + HRIR path\n"
            "  • Media / HDMI sink toggles\n"
            "  • Browser auto-routing\n\n"
            "Saved audio profiles are KEPT.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        # Daemon first — broadcasts events that the GUI will pick up
        # to refresh each tab. Then wipe settings.json (preserves
        # profiles + resets the surround_default_applied marker so
        # the bundled HRIR re-applies on next launch).
        self._daemon.send_command("reset-state")
        reset_to_defaults_preserving_profiles(self._settings)
        # Re-render the local Settings tab controls from the freshly
        # wiped values. Other tabs follow their own daemon-event
        # paths.
        self._reapply_settings_to_widgets()

    def _reapply_settings_to_widgets(self) -> None:
        """Push the current `self._settings` values back into the
        Settings tab widgets without firing the toggle/save handlers
        — used after a reset so the visible state matches what's now
        on disk."""
        for toggle, key, default in (
            (self.overlay_toggle, "overlay", True),
            (self.autostart_toggle, "autostart", True),
            (self.start_min_toggle, "start_minimized", False),
            (self.minimize_toggle, "notify_minimize_hint", False),
        ):
            was_blocked = toggle.blockSignals(True)
            try:
                toggle.setChecked(self._settings.get(key, default))
            finally:
                toggle.blockSignals(was_blocked)
        # Position + orientation combos.
        current_pos = normalize_position(
            self._settings.get("overlay_position", "top-right")
        )
        idx = self.position_combo.findText(POSITION_DISPLAY[current_pos])
        if idx >= 0:
            was_blocked = self.position_combo.blockSignals(True)
            try:
                self.position_combo.setCurrentIndex(idx)
            finally:
                self.position_combo.blockSignals(was_blocked)
        idx = self.orient_combo.findText(
            normalize_orientation(
                self._settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
        if idx >= 0:
            was_blocked = self.orient_combo.blockSignals(True)
            try:
                self.orient_combo.setCurrentIndex(idx)
            finally:
                self.orient_combo.blockSignals(was_blocked)
        # Theme combo + immediate re-apply so the live window flips
        # back to the default palette as part of the reset.
        mode = normalize_mode(self._settings.get("theme_mode", "auto"))
        was_blocked = self.theme_combo.blockSignals(True)
        try:
            self.theme_combo.setCurrentIndex(
                {"auto": 0, "light": 1, "dark": 2}.get(mode, 0)
            )
        finally:
            self.theme_combo.blockSignals(was_blocked)
        apply_theme(mode)

    def _delete_selected_profile(self) -> None:
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
        delete_profile(self._settings, name)
        self._refresh_profile_combo()

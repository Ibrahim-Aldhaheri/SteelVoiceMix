"""Settings tab — overlay options, autostart, audio profiles."""

from __future__ import annotations

import logging
import subprocess
import urllib.parse
import webbrowser

from PySide6.QtCore import Qt
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
    "sudo dnf upgrade steelvoicemix --refresh -y"
)
_ALPHA_DISABLE_CMD = (
    f"sudo dnf copr disable {_ALPHA_REPO} -y && "
    f"sudo dnf copr enable {_STABLE_REPO} -y && "
    "sudo dnf distro-sync steelvoicemix -y"
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
    def __init__(
        self,
        settings: dict,
        overlay,
        sinks_tab,
        daemon_client,
        eq_tab=None,
        mic_tab=None,
        parent=None,
    ):
        """`overlay` is the DialOverlay instance. `sinks_tab` is the
        SinksTab — needed to apply Media/HDMI toggles when a profile
        loads. `daemon_client` is needed by the Reset button to issue
        a daemon-side `reset-state` alongside the GUI's own wipe.
        `eq_tab` + `mic_tab` are read at profile-save time to snapshot
        the live EQ + mic state, and used at load time to drive the
        daemon commands that restore them."""
        super().__init__(parent)
        self._settings = settings
        self._overlay = overlay
        self._sinks_tab = sinks_tab
        self._daemon = daemon_client
        self._eq_tab = eq_tab
        self._mic_tab = mic_tab
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
        theme_lbl = QLabel(self.tr("Theme"))
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

        # Language selector -------------------------------------------
        # Translation coverage is partial; strings without a
        # translation fall back to English. RTL languages (Arabic,
        # Hebrew, etc.) flip the layoutDirection automatically when
        # selected.
        from ..i18n import SUPPORTED_LANGUAGES
        from ..widgets import alpha_badge as _badge
        lang_row = QHBoxLayout()
        lang_lbl = QLabel(self.tr("Language"))
        lang_lbl.setFixedWidth(80)
        # BETA marker: only English + Arabic exist, and Arabic
        # coverage is partial — many strings still fall back to
        # English. Visually marking the row matches the user's
        # mental model so they don't think the gaps are bugs.
        lang_beta = _badge(
            self.tr("BETA"),
            tooltip=self.tr(
                "Beta — translations are partial. Untranslated "
                "strings fall back to English."
            ),
        )
        self.lang_combo = QComboBox()
        self.lang_combo.addItem(self.tr("System default"), "system")
        for code, label in SUPPORTED_LANGUAGES:
            self.lang_combo.addItem(label, code)
        current_lang = self._settings.get("ui_language", "system")
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == current_lang:
                self.lang_combo.setCurrentIndex(i)
                break
        self.lang_combo.currentIndexChanged.connect(self._change_language)
        lang_row.addWidget(lang_lbl)
        lang_row.addWidget(lang_beta, 0, Qt.AlignVCenter)
        lang_row.addWidget(self.lang_combo, 1)
        lang_help = QLabel(
            self.tr(
                "Translation coverage is partial — strings without a "
                "translation fall back to English. Restart the GUI for "
                "the language change to take full effect."
            )
        )
        lang_help.setWordWrap(True)
        lang_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(card(
            self.tr("Appearance"), theme_row, theme_help, lang_row, lang_help,
        ))

        # Startup card -------------------------------------------------
        autostart_row, self.autostart_toggle = labelled_toggle(
            self.tr("Start with system")
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

        layout.addWidget(card(self.tr("Startup"), autostart_row, start_min_row))

        # Shortcut card -----------------------------------------------
        # Optional QShortcut to cycle the system default sink between
        # SteelGame / SteelChat / SteelMedia / SteelHDMI. Off by
        # default — Qt shortcuts only fire while the GUI has focus,
        # so this is most useful on a multi-monitor setup or with
        # the GUI parked on a side screen. Users wanting global
        # (system-wide) shortcuts can bind their DE's keyboard
        # settings to a shell command — TBD whether we ship a CLI.
        cycle_row, self.cycle_toggle = labelled_toggle(
            "Cycle default sink shortcut",
        )
        self.cycle_toggle.setChecked(
            bool(self._settings.get("default_sink_cycle_enabled", False))
        )
        self.cycle_toggle.toggled.connect(self._toggle_cycle_shortcut)

        from PySide6.QtGui import QKeySequence
        from PySide6.QtWidgets import QKeySequenceEdit
        cycle_combo_row = QHBoxLayout()
        combo_lbl = QLabel("Key combo")
        combo_lbl.setFixedWidth(80)
        self.cycle_keyseq_edit = QKeySequenceEdit(
            QKeySequence(
                self._settings.get("default_sink_cycle_combo", "Ctrl+Shift+S")
            )
        )
        self.cycle_keyseq_edit.setMaximumWidth(220)
        self.register_btn = QPushButton("Register")
        self.register_btn.setToolTip(
            "Save the key combo currently shown in the field. After "
            "registering, the shortcut takes effect immediately — no "
            "GUI restart needed."
        )
        self.register_btn.clicked.connect(self._register_cycle_combo)
        cycle_combo_row.addWidget(combo_lbl)
        cycle_combo_row.addWidget(self.cycle_keyseq_edit, 1)
        cycle_combo_row.addWidget(self.register_btn)

        # Exclude-from-cycle multi-select. Some users want certain
        # sinks (typically SteelChat) skipped — Discord stays put on
        # SteelChat regardless of the system default, so cycling
        # there mid-game is a footgun.
        from PySide6.QtWidgets import QCheckBox
        excludes = self._settings.get("default_sink_cycle_exclude") or []
        excludes_set = set(excludes)
        exclude_row = QHBoxLayout()
        exclude_lbl = QLabel("Exclude")
        exclude_lbl.setFixedWidth(80)
        exclude_row.addWidget(exclude_lbl)
        self._cycle_exclude_checkboxes: dict[str, QCheckBox] = {}
        for sink in ("SteelGame", "SteelChat", "SteelMedia", "SteelHDMI"):
            cb = QCheckBox(sink.removeprefix("Steel"))
            cb.setChecked(sink in excludes_set)
            cb.toggled.connect(self._save_cycle_excludes)
            exclude_row.addWidget(cb)
            self._cycle_exclude_checkboxes[sink] = cb
        exclude_row.addStretch(1)

        cycle_help = QLabel(
            "Qt shortcuts only fire while the GUI has focus — for "
            "system-wide bindings, point your desktop's keyboard "
            "settings at <code>steelvoicemix-cli sink cycle</code>. "
            "Restart the GUI after changing the combo to apply."
        )
        cycle_help.setWordWrap(True)
        cycle_help.setTextFormat(Qt.RichText)
        cycle_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(card(
            self.tr("Shortcuts"),
            cycle_row, cycle_combo_row, exclude_row, cycle_help,
        ))

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

        layout.addWidget(card(self.tr("Notifications"), minimize_row, daemon_notif_row))

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
            card(self.tr("Audio Profiles"), profile_row, profile_btns, profile_help)
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

        # Help / Report issue card -----------------------------------
        help_row = QHBoxLayout()
        self.report_btn = QPushButton("📋  Copy diagnostic + open new issue")
        self.report_btn.setToolTip(
            "Captures the daemon's recent journal output, the GUI "
            "version, and your settings.json (sanitised), copies it "
            "to the clipboard, and opens the SteelVoiceMix 'New "
            "Issue' page in your browser. Paste into the body."
        )
        self.report_btn.clicked.connect(self._on_report_issue)
        help_row.addWidget(self.report_btn)
        help_row.addStretch(1)
        help_text = QLabel(
            "Filing a bug report is a 2-step flow: this button does "
            "step 1 (copy diagnostic) and opens step 2 (the issue "
            "page) — paste the clipboard into the body."
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        layout.addWidget(card(self.tr("Report Issue"), help_row, help_text))

        # Reset card --------------------------------------------------
        reset_row = QHBoxLayout()
        self.reset_btn = QPushButton(self.tr("Reset to defaults…"))
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

        layout.addWidget(card(self.tr("Reset"), reset_row, reset_help))

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

    def _toggle_cycle_shortcut(self, checked: bool) -> None:
        self._settings["default_sink_cycle_enabled"] = checked
        save_settings(self._settings)
        self._reload_shortcut_on_window()

    def _register_cycle_combo(self) -> None:
        seq = self.cycle_keyseq_edit.keySequence()
        combo = seq.toString()
        if not combo:
            QMessageBox.warning(
                self,
                "No key combo",
                "Click the Key combo field and press the keys you want "
                "to assign first, then click Register.",
            )
            return
        self._settings["default_sink_cycle_combo"] = combo
        save_settings(self._settings)
        self._reload_shortcut_on_window()
        QMessageBox.information(
            self, "Shortcut registered",
            f"Default-sink cycle is now bound to {combo}. The shortcut "
            "fires while any SteelVoiceMix window has focus.",
        )

    def _reload_shortcut_on_window(self) -> None:
        """Ask the main window to rebuild its QShortcut so the new
        toggle / combo takes effect without a restart."""
        win = self.window()
        if hasattr(win, "reload_default_sink_shortcut"):
            try:
                win.reload_default_sink_shortcut()
            except Exception:
                pass

    def _save_cycle_excludes(self) -> None:
        excludes = [
            sink for sink, cb in self._cycle_exclude_checkboxes.items()
            if cb.isChecked()
        ]
        self._settings["default_sink_cycle_exclude"] = excludes
        save_settings(self._settings)


    def _change_theme(self, index: int) -> None:
        mode = ("auto", "light", "dark")[index] if 0 <= index <= 2 else "auto"
        if mode not in THEME_MODES:
            mode = "auto"
        self._settings["theme_mode"] = mode
        save_settings(self._settings)
        apply_theme(mode)

    def _change_language(self, _index: int) -> None:
        """Save the picked language, swap the QTranslator, flip the
        layoutDirection, and offer to restart the GUI so all already-
        constructed widget text re-translates. Qt's installTranslator
        only affects strings looked up *after* the swap — existing
        widgets keep their cached text until they're rebuilt or the
        process restarts. A restart is the only fully reliable path."""
        code = self.lang_combo.currentData() or "system"
        previous = self._settings.get("ui_language", "system")
        self._settings["ui_language"] = code
        save_settings(self._settings)
        from ..i18n import apply_layout_direction, reset_translator
        app = QApplication.instance()
        reset_translator(app, code)
        apply_layout_direction(app, code)
        # Skip the prompt when the language didn't actually change —
        # the dropdown also fires currentIndexChanged on initial set.
        if code == previous:
            return
        choice = QMessageBox.question(
            self,
            self.tr("Restart required"),
            self.tr(
                "Language changes only fully apply after a restart of "
                "the GUI. Restart now?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if choice == QMessageBox.Yes:
            self._restart_gui()

    def _restart_gui(self) -> None:
        """Re-exec the running GUI process. We only restart the GUI
        layer — the Rust daemon stays up so audio doesn't blip."""
        import os
        import sys
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            # Fallback: just quit; the user can relaunch from the
            # tray or the menu.
            QApplication.instance().quit()

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

    # ---------------------------------------------------- bug report

    def _on_report_issue(self) -> None:
        """Bundle a diagnostic block (version + last 100 daemon
        journal lines + sanitised settings.json), copy to the
        clipboard, and open the GitHub New Issue page so the user
        only has to paste."""
        from ..settings import APP_VERSION  # avoid circular at import time
        diag_lines: list[str] = [
            "## Diagnostic",
            "",
            f"- SteelVoiceMix version: {APP_VERSION}",
            f"- Python: {self._py_version()}",
            f"- Distro: {self._distro_string()}",
            "",
            "### Daemon journal (last 100 lines)",
            "",
            "```",
            self._journal_tail(),
            "```",
            "",
            "### Settings",
            "",
            "```json",
            self._sanitised_settings_json(),
            "```",
        ]
        body = "\n".join(diag_lines)
        QApplication.clipboard().setText(body)
        # Open new-issue page with explicit placeholder text so the
        # user knows what to replace.
        url = (
            "https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix/issues/new?"
            + urllib.parse.urlencode({
                "title": "-- put the subject here --",
                "body": (
                    "-- describe your issue here --\n\n"
                    "<!-- Paste diagnostic from clipboard below -->\n"
                ),
            })
        )
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
        QMessageBox.information(
            self,
            "Diagnostic copied",
            "Diagnostic copied to clipboard. The browser is opening "
            "the SteelVoiceMix issue tracker — paste the clipboard "
            "into the issue body.",
        )

    def _py_version(self) -> str:
        import sys
        return sys.version.split()[0]

    def _distro_string(self) -> str:
        try:
            with open("/etc/os-release") as f:
                kv = dict(
                    line.strip().split("=", 1)
                    for line in f if "=" in line
                )
            name = kv.get("PRETTY_NAME", "?").strip('"')
            return name
        except Exception:
            return "unknown"

    def _journal_tail(self) -> str:
        try:
            r = subprocess.run(
                [
                    "journalctl", "--user", "-u", "steelvoicemix",
                    "-n", "100", "--no-pager",
                ],
                capture_output=True, text=True, timeout=8,
            )
            return r.stdout if r.returncode == 0 else r.stderr
        except Exception as e:
            return f"(journalctl failed: {e})"

    def _sanitised_settings_json(self) -> str:
        import json
        # Profiles can be large + are user-named — truncate to keys
        # for the report.
        sanitised = dict(self._settings)
        if isinstance(sanitised.get("profiles"), dict):
            sanitised["profiles"] = sorted(sanitised["profiles"].keys())
        return json.dumps(sanitised, indent=2, default=str)

    # ---------------------------------------------- profile state helpers

    def _gather_eq_state(self) -> dict[str, list[dict]] | None:
        """Snapshot the EQ tab's live per-channel band cache. Returns
        None when the EQ tab isn't wired up (defensive — happens
        only in tests or partial-init scenarios)."""
        if self._eq_tab is None or not hasattr(self._eq_tab, "_bands_by_channel"):
            return None
        return {
            ch: list(bands)
            for ch, bands in self._eq_tab._bands_by_channel.items()
        }

    def _gather_mic_state(self) -> dict | None:
        """Snapshot the MicrophoneTab's local mirror of MicState plus
        the volume_stabilizer_kind combo selection."""
        if self._mic_tab is None or not hasattr(self._mic_tab, "_state"):
            return None
        out: dict = {
            key: dict(value) for key, value in self._mic_tab._state.items()
        }
        out["volume_stabilizer_kind"] = getattr(
            self._mic_tab, "_volume_stabilizer_kind", "broadcast"
        )
        return out

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
                eq_state=self._gather_eq_state(),
                mic_state=self._gather_mic_state(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid profile name", str(e))
            return
        self._refresh_profile_combo()
        idx = self.profile_combo.findText(name.strip())
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)

    def _apply_eq_from_profile(self, eq_section: dict) -> None:
        """Send one set-eq-channel per channel for the bands carried
        in the profile. Daemon respawns the matching filter chain
        per channel so the user hears the change immediately."""
        if not isinstance(eq_section, dict):
            return
        for ch in ("game", "chat", "media", "hdmi", "mic"):
            bands = eq_section.get(ch)
            if isinstance(bands, list) and bands:
                self._daemon.send_command(
                    "set-eq-channel", channel=ch, bands=bands,
                )

    def _apply_mic_from_profile(self, mic_section: dict) -> None:
        """Send one daemon command per mic feature so the chain
        respawns with the saved settings. Volume Stabilizer kind is
        bundled into its set-mic-volume-stabilizer call."""
        if not isinstance(mic_section, dict):
            return
        kind = mic_section.get("volume_stabilizer_kind", "broadcast")
        commands = (
            ("noise_gate", "set-mic-noise-gate"),
            ("noise_reduction", "set-mic-noise-reduction"),
            ("ai_noise_cancellation", "set-mic-ai-nc"),
            ("volume_stabilizer", "set-mic-volume-stabilizer"),
        )
        for key, cmd in commands:
            feat = mic_section.get(key)
            if not isinstance(feat, dict):
                continue
            kwargs = {
                "enabled": bool(feat.get("enabled", False)),
                "strength": int(feat.get("strength", 0)),
            }
            if key == "volume_stabilizer":
                kwargs["kind"] = kind
            self._daemon.send_command(cmd, **kwargs)

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

        # Apply EQ + mic state if the profile carries them. The
        # daemon broadcasts back EqBandsChanged / MicStateChanged
        # events that the EQ + Mic tabs already listen to, so the
        # GUI sliders snap into place without manual re-render.
        self._apply_eq_from_profile(profile.get("eq", {}))
        self._apply_mic_from_profile(profile.get("mic", {}))

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

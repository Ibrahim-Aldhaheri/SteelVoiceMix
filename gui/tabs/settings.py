"""Settings tab — overlay options, autostart, audio profiles."""

from __future__ import annotations

import logging
import subprocess

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
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
    save as save_settings,
    save_profile,
)
from ..widgets import POSITION_DISPLAY, divider, section_title

log = logging.getLogger(__name__)


class SettingsTab(QWidget):
    def __init__(self, settings: dict, overlay, sinks_tab, parent=None):
        """`overlay` is the DialOverlay instance. `sinks_tab` is the
        SinksTab — needed to apply Media/HDMI toggles when a profile
        loads."""
        super().__init__(parent)
        self._settings = settings
        self._overlay = overlay
        self._sinks_tab = sinks_tab
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(section_title("Overlay"))

        self.overlay_check = QCheckBox("Show overlay when dial is turned")
        self.overlay_check.setChecked(self._settings.get("overlay", True))
        self.overlay_check.toggled.connect(self._toggle_overlay)
        layout.addWidget(self.overlay_check)

        position_row = QHBoxLayout()
        pos_lbl = QLabel("Position")
        pos_lbl.setFixedWidth(70)
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
        layout.addLayout(position_row)

        orient_row = QHBoxLayout()
        ori_lbl = QLabel("Style")
        ori_lbl.setFixedWidth(70)
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
        layout.addLayout(orient_row)

        layout.addWidget(divider())
        layout.addWidget(section_title("Startup"))

        self.autostart_check = QCheckBox("Start with system")
        self.autostart_check.setChecked(self._settings.get("autostart", True))
        self.autostart_check.toggled.connect(self._toggle_autostart)
        layout.addWidget(self.autostart_check)

        layout.addWidget(divider())
        layout.addWidget(section_title("Audio Profiles"))

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
        profile_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-top: 4px;"
        )
        profile_help.setWordWrap(True)
        layout.addWidget(profile_help)

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
        self.overlay_check.setChecked(self._settings.get("overlay", True))
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

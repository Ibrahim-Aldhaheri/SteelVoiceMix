"""Sinks tab — Media + HDMI virtual-sink toggles, browser auto-routing."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..widgets import card, labelled_toggle


class SinksTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._media_enabled = False
        self._hdmi_enabled = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Virtual sinks card -------------------------------------------
        media_row = QHBoxLayout()
        media_lbl = QLabel("🎵  Media")
        media_lbl.setFixedWidth(80)
        self.media_btn = QPushButton("Add Media")
        self.media_btn.clicked.connect(self._toggle_media)
        media_row.addWidget(media_lbl)
        media_row.addWidget(self.media_btn, 1)

        hdmi_row = QHBoxLayout()
        hdmi_lbl = QLabel("📺  HDMI")
        hdmi_lbl.setFixedWidth(80)
        self.hdmi_btn = QPushButton("Add HDMI")
        self.hdmi_btn.clicked.connect(self._toggle_hdmi)
        hdmi_row.addWidget(hdmi_lbl)
        hdmi_row.addWidget(self.hdmi_btn, 1)

        sinks_help = QLabel(
            "Media and HDMI sinks bypass the ChatMix dial — useful for "
            "music, browsers, or routing audio to a TV/AVR independently "
            "of the headset."
        )
        sinks_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        sinks_help.setWordWrap(True)

        layout.addWidget(card("Virtual Sinks", media_row, hdmi_row, sinks_help))

        # Auto-routing card --------------------------------------------
        # Marked ALPHA — author hasn't pushed on it and treats it as
        # nice-to-have (per the user's flagged-as-experimental note).
        auto_row, self.auto_route_toggle = labelled_toggle(
            "Route browsers and media players to SteelMedia automatically",
            tooltip=(
                "Alpha — lightly tested. When enabled, the daemon moves "
                "new browser and media-player audio streams (Firefox, "
                "Chromium, mpv, VLC…) to the SteelMedia sink so they "
                "bypass the ChatMix dial. Manual moves stick — the "
                "daemon only acts on first-seen streams."
            ),
            badge="ALPHA",
        )
        self.auto_route_toggle.toggled.connect(self._toggle_auto_route)

        layout.addWidget(card("Auto-Routing", auto_row))

        layout.addStretch(1)

    # ------------------------------------------------- public state queries

    @property
    def media_enabled(self) -> bool:
        return self._media_enabled

    @property
    def hdmi_enabled(self) -> bool:
        return self._hdmi_enabled

    # ---------------------------------------------------- daemon-event hooks

    def on_media_changed(self, enabled: bool) -> None:
        self._media_enabled = enabled
        self.media_btn.setText("Remove Media" if enabled else "Add Media")
        self.media_btn.setToolTip(
            "Destroy the SteelMedia virtual sink"
            if enabled
            else "Create a SteelMedia virtual sink that bypasses the ChatMix dial"
        )

    def on_hdmi_changed(self, enabled: bool) -> None:
        self._hdmi_enabled = enabled
        self.hdmi_btn.setText("Remove HDMI" if enabled else "Add HDMI")
        self.hdmi_btn.setToolTip(
            "Destroy the SteelHDMI virtual sink"
            if enabled
            else "Create a SteelHDMI virtual sink that loops to your HDMI output"
        )

    def on_auto_route_changed(self, enabled: bool) -> None:
        was_blocked = self.auto_route_toggle.blockSignals(True)
        self.auto_route_toggle.setChecked(enabled)
        self.auto_route_toggle.blockSignals(was_blocked)

    # ---------------------------------------------------------- input handlers

    def _toggle_media(self) -> None:
        cmd = "remove-media-sink" if self._media_enabled else "add-media-sink"
        self._daemon.send_command(cmd)
        # Disable the button until the daemon confirms the change so quick
        # double-clicks don't queue conflicting commands.
        self.media_btn.setEnabled(False)
        QTimer.singleShot(600, lambda: self.media_btn.setEnabled(True))

    def _toggle_hdmi(self) -> None:
        cmd = "remove-hdmi-sink" if self._hdmi_enabled else "add-hdmi-sink"
        self._daemon.send_command(cmd)
        self.hdmi_btn.setEnabled(False)
        QTimer.singleShot(600, lambda: self.hdmi_btn.setEnabled(True))

    def _toggle_auto_route(self, checked: bool) -> None:
        self._daemon.send_command(
            "set-auto-route-browsers", enabled=bool(checked)
        )

    # ---------------------------------------------------- profile load helper

    def apply_profile(self, want_media: bool, want_hdmi: bool) -> None:
        """Profile loader: align the daemon's sink state with the profile."""
        if want_media != self._media_enabled:
            self._daemon.send_command(
                "add-media-sink" if want_media else "remove-media-sink"
            )
        if want_hdmi != self._hdmi_enabled:
            self._daemon.send_command(
                "add-hdmi-sink" if want_hdmi else "remove-hdmi-sink"
            )

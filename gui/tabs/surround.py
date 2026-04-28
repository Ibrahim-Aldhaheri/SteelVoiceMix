"""Surround tab — virtual 7.1 over headphones via HRIR convolution.

The tab is opt-in by design: the daemon refuses to enable surround
without an HRIR file, and we don't bundle one (license tangle). UX
flow:

  1. User downloads an HRIR (HeSuVi presets, Impulcifer-personal HRTFs,
     SADIE / CIPIC research files, etc.) into a known location.
  2. User clicks Browse, picks the WAV.
  3. User toggles Enable. Audio apps now see a SteelSurround 7.1 sink;
     PipeWire's filter-chain convolves each surround channel into
     binaural stereo on the headset.

If the HRIR file is missing, malformed, or the channel count doesn't
match HeSuVi's 14-channel layout, PipeWire's chain-spawn fails and
the daemon logs the error — the GUI surfaces a generic 'enable
failed' message rather than try to diagnose the file.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..widgets import divider, section_title


class SurroundTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._enabled: bool = False
        self._hrir_path: str = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(section_title("Virtual Surround (7.1)"))

        intro = QLabel(
            "Apps see a SteelSurround 7.1 sink; PipeWire convolves "
            "each surround channel with your HRIR file and feeds the "
            "result to the headset as binaural stereo. Useful for "
            "5.1 / 7.1 games and movies; stereo content is unaffected."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text); padding-bottom: 4px;"
        )
        layout.addWidget(intro)

        layout.addWidget(divider())
        layout.addWidget(section_title("HRIR file"))

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("(no HRIR file selected)")
        self.path_edit.setMinimumWidth(220)
        path_row.addWidget(self.path_edit, 1)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(self.browse_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setEnabled(False)
        path_row.addWidget(self.clear_btn)
        layout.addLayout(path_row)

        hrir_help = QLabel(
            "HeSuVi-format 14-channel WAV expected. Get presets from "
            "the HeSuVi GitHub release (Atmos, DTS Headphone, GoodHurt, "
            "etc.) or generate a personalised HRTF with Impulcifer. "
            "Other layouts may load but produce unexpected positioning."
        )
        hrir_help.setWordWrap(True)
        hrir_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        layout.addWidget(hrir_help)

        layout.addWidget(divider())
        layout.addWidget(section_title("Enable"))

        self.enable_check = QCheckBox("Enable virtual surround")
        self.enable_check.setEnabled(False)
        self.enable_check.toggled.connect(self._on_toggled)
        layout.addWidget(self.enable_check)

        self.status_label = QLabel("Pick an HRIR file to enable surround.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-top: 4px;"
        )
        layout.addWidget(self.status_label)

        layout.addStretch(1)

    # ---------------------------------------------------- daemon-event hooks

    def on_enabled_changed(self, enabled: bool) -> None:
        self._enabled = enabled
        was_blocked = self.enable_check.blockSignals(True)
        self.enable_check.setChecked(enabled)
        self.enable_check.blockSignals(was_blocked)
        self._refresh_status_label()

    def on_hrir_changed(self, path: str) -> None:
        self._hrir_path = path
        # The line edit is read-only; we set its visible text so the
        # user always sees what the daemon currently believes is the
        # HRIR file.
        self.path_edit.setText(path)
        self.clear_btn.setEnabled(bool(path))
        self.enable_check.setEnabled(bool(path))
        if not path and self._enabled:
            # Daemon clears enable when HRIR is cleared mid-run; the
            # event will arrive separately but the UI shouldn't lag.
            self._enabled = False
            was_blocked = self.enable_check.blockSignals(True)
            self.enable_check.setChecked(False)
            self.enable_check.blockSignals(was_blocked)
        self._refresh_status_label()

    # --------------------------------------------------------- input handlers

    def _on_browse(self) -> None:
        # Default to ~/Downloads when there's no prior path — most
        # users land HeSuVi WAVs there. If a path is already set, open
        # the dialog in its parent directory.
        start_dir = os.path.expanduser("~/Downloads")
        if self._hrir_path:
            parent = os.path.dirname(self._hrir_path)
            if parent and os.path.isdir(parent):
                start_dir = parent
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose HRIR WAV",
            start_dir,
            "WAV files (*.wav);;All files (*)",
        )
        if not path:
            return
        self._daemon.send_command("set-surround-hrir", path=path)

    def _on_clear(self) -> None:
        self._daemon.send_command("set-surround-hrir", path=None)

    def _on_toggled(self, checked: bool) -> None:
        self._daemon.send_command(
            "set-surround-enabled", enabled=bool(checked)
        )

    def _refresh_status_label(self) -> None:
        if not self._hrir_path:
            self.status_label.setText(
                "Pick an HRIR file to enable surround."
            )
        elif self._enabled:
            self.status_label.setText(
                "🟢 SteelSurround sink active. Set apps to output to "
                "SteelSurround for 7.1 → binaural conversion."
            )
        else:
            self.status_label.setText(
                f"HRIR ready: {os.path.basename(self._hrir_path)}. "
                "Toggle Enable to load the chain."
            )

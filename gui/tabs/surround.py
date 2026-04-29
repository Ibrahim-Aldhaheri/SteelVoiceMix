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
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..hrir_default import DefaultHrirFetcher, cached_default_path
from ..widgets import card, labelled_toggle


class SurroundTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._enabled: bool = False
        self._hrir_path: str = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Intro card ----------------------------------------------------
        intro = QLabel(
            "Apps see a SteelSurround 7.1 sink; PipeWire convolves "
            "each surround channel with your HRIR file and feeds the "
            "result to the headset as binaural stereo. Useful for "
            "5.1 / 7.1 games and movies; stereo content is unaffected."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 11px;")
        layout.addWidget(card("Virtual Surround (7.1)", intro))

        # HRIR file card -----------------------------------------------
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("(no HRIR file selected)")
        self.path_edit.setMinimumWidth(220)
        path_row.addWidget(self.path_edit, 1)
        self.default_btn = QPushButton("Use Default")
        self.default_btn.setToolTip(
            "Fetch a generic HeSuVi-format HRIR from upstream "
            "(EAC_Default.wav, ~165 KB) and use it. You can replace it "
            "with your own file via Browse at any time."
        )
        self.default_btn.clicked.connect(self._on_use_default)
        path_row.addWidget(self.default_btn)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(self.browse_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setEnabled(False)
        path_row.addWidget(self.clear_btn)

        hrir_help = QLabel(
            "HeSuVi-format 14-channel WAV expected. The Use Default "
            "button fetches a generic reference HRIR from upstream "
            "(works fine for casual use); for tuned positioning try "
            "the HeSuVi GitHub release (Atmos / DTS Headphone / "
            "GoodHurt presets) or generate a personalised HRTF with "
            "Impulcifer."
        )
        hrir_help.setWordWrap(True)
        hrir_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        layout.addWidget(card("HRIR File", path_row, hrir_help))

        # Enable card --------------------------------------------------
        toggle_row, self.enable_toggle = labelled_toggle(
            "Enable virtual surround",
            tooltip="Loads the SteelSurround 7.1 sink + HRIR convolver chain.",
        )
        self.enable_toggle.setEnabled(False)
        self.enable_toggle.toggled.connect(self._on_toggled)

        self.status_label = QLabel("Pick an HRIR file to enable surround.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(card("Enable", toggle_row, self.status_label))

        layout.addStretch(1)

    # ---------------------------------------------------- daemon-event hooks

    def on_enabled_changed(self, enabled: bool) -> None:
        self._enabled = enabled
        was_blocked = self.enable_toggle.blockSignals(True)
        self.enable_toggle.setChecked(enabled)
        self.enable_toggle.blockSignals(was_blocked)
        self._refresh_status_label()

    def on_hrir_changed(self, path: str) -> None:
        self._hrir_path = path
        self.path_edit.setText(path)
        self.clear_btn.setEnabled(bool(path))
        self.enable_toggle.setEnabled(bool(path))
        if not path and self._enabled:
            self._enabled = False
            was_blocked = self.enable_toggle.blockSignals(True)
            self.enable_toggle.setChecked(False)
            self.enable_toggle.blockSignals(was_blocked)
        self._refresh_status_label()

    # --------------------------------------------------------- input handlers

    def _on_use_default(self) -> None:
        """Fetch the default HRIR (or use the cached copy if it's
        already on disk) and tell the daemon to use that path."""
        cached = cached_default_path()
        if cached.is_file() and cached.stat().st_size > 0:
            # Already cached — short-circuit the network round-trip.
            self._daemon.send_command("set-surround-hrir", path=str(cached))
            return
        self.default_btn.setEnabled(False)
        self.default_btn.setText("Downloading…")
        self._hrir_fetcher = DefaultHrirFetcher(self)
        self._hrir_fetcher.finished_with_path.connect(self._on_default_fetched)
        self._hrir_fetcher.start()

    def _on_default_fetched(self, path: str, error: str) -> None:
        self.default_btn.setEnabled(True)
        self.default_btn.setText("Use Default")
        if error or not path:
            QMessageBox.warning(
                self,
                "Default HRIR fetch failed",
                error or "Could not fetch the default HRIR from upstream.",
            )
            return
        self._daemon.send_command("set-surround-hrir", path=path)

    def _on_browse(self) -> None:
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

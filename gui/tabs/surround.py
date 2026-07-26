"""Surround tab — virtual 7.1 over headphones.

Rewritten for a non-technical audience. The mechanism (HRIR convolution
of a 7.1 stream into binaural stereo via a PipeWire filter chain) is
unchanged and the daemon commands are identical; only the framing is
different. The page now reads as a three-step flow — what it does, pick
a sound profile, turn it on — with the jargon (HRIR, HeSuVi,
Impulcifer) tucked behind a "?" for people who want it.
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

from ..hrir_default import bundled_default_path, has_default
from ..widgets import (
    ACCENT,
    card,
    collapsible_help,
    help_text,
    labelled_toggle,
)


class SurroundTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._enabled: bool = False
        self._hrir_path: str = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # --- What it does + the on/off switch, together at the top ---
        intro = help_text(
            self.tr(
                "Makes 5.1 and 7.1 games and movies sound like they come "
                "from around you, on ordinary headphones. Stereo music and "
                "calls are left unchanged."
            )
        )
        tech_row, tech_body = collapsible_help(
            self.tr(
                "Technically: apps see a SteelSurround 7.1 output; PipeWire "
                "convolves each channel with an HRIR (head-related impulse "
                "response) file and mixes the result down to binaural "
                "stereo. HeSuVi-format 14-channel WAVs work; for tuned "
                "positioning use a HeSuVi release (Atmos / DTS Headphone) "
                "or a personalised HRTF from Impulcifer."
            ),
            label=self.tr("Virtual Surround"),
        )

        toggle_row, self.enable_toggle = labelled_toggle(
            self.tr("Turn on virtual surround"),
        )
        self.enable_toggle.setEnabled(False)
        self.enable_toggle.toggled.connect(self._on_toggled)

        self.status_label = help_text(
            self.tr("Choose a sound profile below first.")
        )

        layout.addWidget(card(
            None, tech_row, tech_body, intro, toggle_row, self.status_label,
        ))

        # --- Sound profile picker ---
        profile_help = help_text(
            self.tr(
                "Surround needs a sound profile that describes how audio "
                "should reach your ears. Use the built-in one to get "
                "started, or load your own."
            )
        )
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(self.tr("No profile selected"))
        self.path_edit.setMinimumWidth(220)
        path_row.addWidget(self.path_edit, 1)

        self.default_btn = QPushButton(self.tr("Use built-in profile"))
        self.default_btn.setProperty("cta", True)
        self.default_btn.setToolTip(
            self.tr(
                "Use the bundled reference profile (EAC_Default.wav). Good "
                "for casual use; you can switch to your own file anytime."
            )
        )
        self.default_btn.clicked.connect(self._on_use_default)
        path_row.addWidget(self.default_btn)

        self.browse_btn = QPushButton(self.tr("Choose file…"))
        self.browse_btn.setToolTip(
            self.tr("Load your own HRIR WAV (HeSuVi format).")
        )
        self.browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(self.browse_btn)

        self.clear_btn = QPushButton(self.tr("Remove"))
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setEnabled(False)
        path_row.addWidget(self.clear_btn)

        layout.addWidget(card(
            self.tr("Sound profile"), profile_help, path_row,
        ))

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
        # Once a profile is chosen, the built-in button is no longer the
        # primary call-to-action; de-emphasise it so "Turn on" leads.
        self.default_btn.setProperty("cta", not bool(path))
        self.default_btn.style().unpolish(self.default_btn)
        self.default_btn.style().polish(self.default_btn)
        if not path and self._enabled:
            self._enabled = False
            was_blocked = self.enable_toggle.blockSignals(True)
            self.enable_toggle.setChecked(False)
            self.enable_toggle.blockSignals(was_blocked)
        self._refresh_status_label()

    # --------------------------------------------------------- input handlers

    def _on_use_default(self) -> None:
        if not has_default():
            QMessageBox.warning(
                self,
                self.tr("Built-in profile missing"),
                self.tr(
                    "The bundled sound profile is missing — your install "
                    "may be incomplete. Try reinstalling, or load your own "
                    "profile with Choose file."
                ),
            )
            return
        self._daemon.send_command(
            "set-surround-hrir", path=str(bundled_default_path())
        )

    def _on_browse(self) -> None:
        start_dir = os.path.expanduser("~/Downloads")
        if self._hrir_path:
            parent = os.path.dirname(self._hrir_path)
            if parent and os.path.isdir(parent):
                start_dir = parent
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Choose a sound profile (WAV)"),
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
                self.tr("Choose a sound profile below first.")
            )
        elif self._enabled:
            self.status_label.setText(
                self.tr(
                    "On. Set an app's output to “SteelSurround” to hear it "
                    "in surround."
                )
            )
            self.status_label.setStyleSheet(
                f"color: {ACCENT}; font-size: 11px; font-weight: bold;"
            )
            return
        else:
            self.status_label.setText(
                self.tr("Profile ready — flip the switch to turn it on.")
            )
        self.status_label.setStyleSheet("")
        self.status_label.setObjectName("help-text")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

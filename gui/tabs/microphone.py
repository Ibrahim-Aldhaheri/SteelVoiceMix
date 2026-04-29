"""Microphone tab — placeholder for noise gate, noise reduction, and AI
noise cancellation.

Currently a stub: the controls are laid out and disabled, with a note
explaining that the daemon side isn't wired up yet. Built early so the
tab structure exists before the features land — that way enabling each
feature is purely additive (flip `setEnabled(True)`, wire the slider
signal to a `daemon.send_command(...)`).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..widgets import card, labelled_toggle


class MicrophoneTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        not_ready = QLabel(
            "Microphone processing isn't wired up yet. The controls "
            "below preview the planned UI; toggling them does nothing "
            "until the daemon-side capture-path filter chain lands."
        )
        not_ready.setWordWrap(True)
        not_ready.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text);"
        )
        layout.addWidget(card("Microphone Processing", not_ready))

        self.gate_toggle, self.gate_slider, self.gate_value = self._add_feature_card(
            layout,
            "Noise Gate",
            "Mutes the mic when input level falls below the threshold. "
            "Higher = more aggressive (cuts more background sound, more "
            "likely to clip the start of soft speech).",
        )
        self.nr_toggle, self.nr_slider, self.nr_value = self._add_feature_card(
            layout,
            "Noise Reduction",
            "Spectral noise suppression — removes constant background "
            "hum, fan noise, keyboard clatter. Higher strength is more "
            "aggressive but can introduce artefacts on voice.",
        )
        self.ai_toggle, self.ai_slider, self.ai_value = self._add_feature_card(
            layout,
            "AI Noise Cancellation",
            "Neural-network-based denoiser. Heavier than spectral NR but "
            "handles non-stationary noise (typing, dog barks, road "
            "noise). Higher strength = more aggressive cleanup.",
        )

        layout.addStretch(1)

    def _add_feature_card(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        description: str,
    ):
        toggle_row, toggle = labelled_toggle("Enabled")
        toggle.setEnabled(False)

        slider_row = QHBoxLayout()
        strength_lbl = QLabel("Strength")
        strength_lbl.setFixedWidth(80)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        slider.setEnabled(False)
        value_lbl = QLabel("50")
        value_lbl.setFixedWidth(36)
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda v, lbl=value_lbl: lbl.setText(str(v)))
        slider_row.addWidget(strength_lbl)
        slider_row.addWidget(slider, 1)
        slider_row.addWidget(value_lbl)

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        parent_layout.addWidget(card(title, toggle_row, slider_row, desc))
        return toggle, slider, value_lbl

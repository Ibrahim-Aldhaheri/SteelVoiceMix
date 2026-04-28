"""Microphone tab — placeholder for noise gate, noise reduction, and AI
noise cancellation.

Currently a stub: the controls are laid out and disabled, with a note
explaining that the daemon side isn't wired up yet. Built early so the
tab structure exists before the features land — that way enabling each
feature is purely additive (flip `setEnabled(True)`, wire the slider
signal to a `daemon.send_command(...)`).

Architecture sketch for when the daemon catches up:
  - Each filter inserts on the headset's MIC capture path, NOT the
    playback chains. Separate filter-chain instance per filter type so
    they can be toggled independently.
  - Sliders send `set-mic-<feature>` commands with a strength value
    (0–100) that the daemon clamps + maps to filter-graph parameters.
  - Status events deliver per-feature state; this tab listens via
    `on_<feature>_changed(enabled, strength)` slots.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..widgets import divider, section_title


class MicrophoneTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(section_title("Microphone Processing"))

        not_ready = QLabel(
            "Microphone processing is not wired up yet. The controls "
            "below are previews of the planned UI; toggling them does "
            "nothing until the daemon-side capture-path filter chain "
            "lands."
        )
        not_ready.setWordWrap(True)
        not_ready.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-bottom: 4px;"
        )
        layout.addWidget(not_ready)

        self.gate_check, self.gate_slider, self.gate_value = self._build_feature_row(
            layout,
            "Noise Gate",
            "Mutes the mic when input level falls below the threshold. "
            "Higher = more aggressive (cuts more background sound, more "
            "likely to clip the start of soft speech).",
        )

        layout.addWidget(divider())

        self.nr_check, self.nr_slider, self.nr_value = self._build_feature_row(
            layout,
            "Noise Reduction",
            "Spectral noise suppression — removes constant background "
            "hum, fan noise, keyboard clatter. Higher strength is more "
            "aggressive but can introduce artefacts on voice.",
        )

        layout.addWidget(divider())

        self.aink_check, self.aink_slider, self.aink_value = self._build_feature_row(
            layout,
            "AI Noise Cancellation",
            "Neural-network-based denoiser. Heavier than spectral NR but "
            "handles non-stationary noise (typing, dog barks, road "
            "noise). Higher strength = more aggressive cleanup.",
        )

        layout.addStretch(1)

    def _build_feature_row(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        description: str,
    ) -> tuple[QCheckBox, QSlider, QLabel]:
        """Render one toggle + sensitivity-slider row. Disabled until
        the daemon supports it; returns the widgets so wiring can be
        added later without restructuring the layout."""
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("font-weight: bold;")
        parent_layout.addWidget(title_lbl)

        check = QCheckBox("Enabled")
        check.setEnabled(False)
        parent_layout.addWidget(check)

        slider_row = QHBoxLayout()
        strength_lbl = QLabel("Strength")
        strength_lbl.setFixedWidth(70)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        slider.setEnabled(False)
        value_lbl = QLabel("50")
        value_lbl.setFixedWidth(30)
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda v, lbl=value_lbl: lbl.setText(str(v)))
        slider_row.addWidget(strength_lbl)
        slider_row.addWidget(slider, 1)
        slider_row.addWidget(value_lbl)
        parent_layout.addLayout(slider_row)

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-bottom: 4px;"
        )
        parent_layout.addWidget(desc)

        return check, slider, value_lbl

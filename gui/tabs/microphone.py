"""Microphone tab — wired to the daemon's mic-capture filter chain.

Three independently-toggleable features (Noise Gate, Noise Reduction,
AI Noise Cancellation), each with an Enabled toggle and a 0..100
strength slider. All three feed the same set-mic-* daemon commands;
the daemon reacts by spawning / re-spawning a single PipeWire
filter chain whose nodes reflect whichever combination is on.

Slider drags are debounced like the EQ tab's: while the user is
moving the slider we only update the visible label, then 250 ms
after the last change (or immediately on slider-released) we fire
one `set-mic-*` command. Without this, every pixel of slider travel
would respawn the LADSPA chain — same problem we hit on the EQ.

Daemon broadcasts mic-state-changed events whenever any feature
toggles or strength changes. We re-apply the snapshot to all three
toggles + sliders, blocking signals so the echo doesn't fire its
own command back at the daemon.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..settings import save as save_settings
from ..widgets import card, labelled_toggle

# Daemon command + MicState key names. The daemon's MicState fields
# are noise_gate / noise_reduction / ai_noise_cancellation; the
# matching commands are set-mic-noise-gate / set-mic-noise-reduction
# / set-mic-ai-nc.
_FEATURES = (
    ("noise_gate", "set-mic-noise-gate"),
    ("noise_reduction", "set-mic-noise-reduction"),
    ("ai_noise_cancellation", "set-mic-ai-nc"),
)


class MicrophoneTab(QWidget):
    def __init__(self, daemon_client, settings: dict, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        # Settings dict is shared with the rest of the GUI so the
        # one-time "default source promoted" marker persists across
        # launches alongside overlay/profile prefs.
        self._settings = settings

        # Local mirror of the daemon's MicState so we can issue
        # incremental updates (only the changed feature's command)
        # rather than redundantly re-sending the full state every
        # interaction.
        self._state: dict[str, dict] = {
            key: {"enabled": False, "strength": 50}
            for key, _cmd in _FEATURES
        }

        # Per-feature debounce timers. Each slider drag updates a
        # pending strength value; the timer fires 250 ms after the
        # last change to send a single command.
        self._pending_strength: dict[str, int] = {}
        self._commit_timers: dict[str, QTimer] = {}

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        intro = QLabel(
            "Apply capture-side processing to your headset's microphone. "
            "Apps record from the SteelMic source — selectable in your "
            "system audio settings or per-app input picker."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text);"
        )
        layout.addWidget(card("Microphone Processing", intro))

        self.gate_toggle, self.gate_slider, self.gate_value = self._add_feature_card(
            layout,
            "noise_gate",
            "Noise Gate",
            "Mutes the mic when input level falls below the threshold. "
            "Higher strength = more aggressive (cuts more background "
            "sound; may clip the start of soft speech). Provided by "
            "swh-plugins' gate_1410.",
        )
        self.nr_toggle, self.nr_slider, self.nr_value = self._add_feature_card(
            layout,
            "noise_reduction",
            "Noise Reduction",
            "Mild RNNoise denoiser — removes constant background hum, "
            "fan noise, keyboard clatter. Capped at 50% VAD threshold "
            "for a lighter touch than AI NC.",
        )
        self.ai_toggle, self.ai_slider, self.ai_value = self._add_feature_card(
            layout,
            "ai_noise_cancellation",
            "AI Noise Cancellation",
            "Aggressive RNNoise — handles non-stationary noise (typing, "
            "dog barks, road noise) but can clip quieter speech at high "
            "strength. If both NR and AI NC are on, only AI NC runs.",
        )

        notes = QLabel(
            "Requires the noise-suppression-for-voice and swh-plugins "
            "packages. If a feature fails to come up, check that the "
            "plugins are installed (Fedora: `dnf install "
            "noise-suppression-for-voice swh-plugins`)."
        )
        notes.setWordWrap(True)
        notes.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-top: 4px;"
        )
        layout.addWidget(notes)

        layout.addStretch(1)

    # ---------------------------------------------------- daemon-event hook

    def on_mic_state_changed(self, state: dict) -> None:
        """Re-apply a daemon snapshot of the full MicState. Signals
        are blocked while we mutate widgets so the echo doesn't loop
        back as another set-mic-* command."""
        for key, _cmd in _FEATURES:
            feat = state.get(key) or {}
            enabled = bool(feat.get("enabled", False))
            strength = int(feat.get("strength", 0))
            self._state[key]["enabled"] = enabled
            self._state[key]["strength"] = strength
            toggle, slider, value_lbl = self._widgets_for(key)
            was_blocked = toggle.blockSignals(True)
            try:
                toggle.setChecked(enabled)
            finally:
                toggle.blockSignals(was_blocked)
            was_blocked = slider.blockSignals(True)
            try:
                slider.setValue(strength)
            finally:
                slider.blockSignals(was_blocked)
            value_lbl.setText(str(strength))
            slider.setEnabled(enabled)

    # --------------------------------------------------------- internals

    def _widgets_for(self, key: str):
        return {
            "noise_gate": (self.gate_toggle, self.gate_slider, self.gate_value),
            "noise_reduction": (self.nr_toggle, self.nr_slider, self.nr_value),
            "ai_noise_cancellation": (self.ai_toggle, self.ai_slider, self.ai_value),
        }[key]

    def _command_for(self, key: str) -> str:
        return dict(_FEATURES)[key]

    def _add_feature_card(
        self,
        parent_layout: QVBoxLayout,
        key: str,
        title: str,
        description: str,
    ):
        toggle_row, toggle = labelled_toggle("Enabled")
        toggle.toggled.connect(lambda checked, k=key: self._on_toggled(k, checked))

        slider_row = QHBoxLayout()
        strength_lbl = QLabel("Strength")
        strength_lbl.setFixedWidth(80)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        slider.setEnabled(False)  # Re-enabled when toggle flips on
        value_lbl = QLabel("50")
        value_lbl.setFixedWidth(36)
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(
            lambda v, k=key, lbl=value_lbl: self._on_slider_changed(k, v, lbl)
        )
        slider.sliderReleased.connect(
            lambda k=key, s=slider: self._on_slider_released(k, s)
        )
        slider_row.addWidget(strength_lbl)
        slider_row.addWidget(slider, 1)
        slider_row.addWidget(value_lbl)

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        parent_layout.addWidget(card(title, toggle_row, slider_row, desc))

        # Debounce timer for this feature's slider drags.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(250)
        timer.timeout.connect(lambda k=key: self._commit_pending_strength(k))
        self._commit_timers[key] = timer
        return toggle, slider, value_lbl

    # ---------------------------------------------------------- handlers

    def _on_toggled(self, key: str, checked: bool) -> None:
        self._state[key]["enabled"] = checked
        # Slider only useful when the feature is on — disable it
        # otherwise so the user has a clear visual signal.
        _, slider, _ = self._widgets_for(key)
        slider.setEnabled(checked)
        # Send the full feature state — daemon's command takes both.
        self._daemon.send_command(
            self._command_for(key),
            enabled=checked,
            strength=self._state[key]["strength"],
        )

    def _on_slider_changed(self, key: str, value: int, label: QLabel) -> None:
        label.setText(str(value))
        self._state[key]["strength"] = value
        self._pending_strength[key] = value
        self._commit_timers[key].start()

    def _on_slider_released(self, key: str, slider: QSlider) -> None:
        # Commit immediately on release so the user gets fast
        # feedback when they let go (no 250 ms wait).
        self._pending_strength[key] = slider.value()
        self._commit_timers[key].stop()
        self._commit_pending_strength(key)

    def _commit_pending_strength(self, key: str) -> None:
        strength = self._pending_strength.pop(key, None)
        if strength is None:
            return
        self._daemon.send_command(
            self._command_for(key),
            enabled=self._state[key]["enabled"],
            strength=int(strength),
        )

    # ------------------------------------------------------- default-source

    def on_mic_default_source_changed(self, active: bool) -> None:
        """Daemon broadcast: SteelMic is now (or no longer) the
        system default source. The first time it goes active in a
        user's life, surface a one-shot dialog explaining what just
        changed — without it, the swap is invisible and people get
        confused why their default mic suddenly says SteelMic. After
        the first time, persisted via settings.json so we don't
        nag on every subsequent toggle."""
        if not active:
            return
        if self._settings.get("mic_default_promoted_shown", False):
            return
        QMessageBox.information(
            self,
            "Default microphone changed",
            "Heads up: SteelMic is now your system's default "
            "microphone source. Apps that follow the system default "
            "(most do) will pick up the processed audio automatically. "
            "If you'd rather use a different mic, switch the default "
            "back from your system audio settings — disabling all "
            "microphone features here also restores the previous "
            "default.\n\nThis notice only shows once.",
        )
        self._settings["mic_default_promoted_shown"] = True
        save_settings(self._settings)

"""Equalizer tab — 10-band parametric EQ with per-channel state.

This tab is going to grow significantly: a searchable preset library,
Custom-N preset auto-creation when sliders are modified, up to 5
favourites per channel, and Media + HDMI added to the channel selector.
Each of those features is a self-contained block of UI + handlers, so
the file is structured to make adding them straightforward — the slider
grid stays put, and new sections layer below it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..eq_presets import (
    delete_user_preset,
    find_preset,
    is_user_preset,
    list_presets,
    save_user_preset,
)
from ..widgets import section_title


# Common parametric-EQ preset JSONs use 10 filter slots
# (parametricEQ.filter1..filter10), so 10 bands lets those load 1:1 into
# our state.
NUM_EQ_BANDS = 10


def _default_eq_band(idx: int) -> dict:
    """Default starting band for slot `idx` (0..9). Mirrors the Rust
    `default_channel_bands()`: low shelf at 32 Hz, peaking 64 → 8 k, high
    shelf at 16 k. Used both as initial state pre-handshake and as a
    safety net if the daemon ever sends a malformed band."""
    template = [
        (32.0, 0.7, "lowshelf"),
        (64.0, 1.0, "peaking"),
        (125.0, 1.0, "peaking"),
        (250.0, 1.0, "peaking"),
        (500.0, 1.0, "peaking"),
        (1000.0, 1.0, "peaking"),
        (2000.0, 1.0, "peaking"),
        (4000.0, 1.0, "peaking"),
        (8000.0, 1.0, "peaking"),
        (16000.0, 0.7, "highshelf"),
    ]
    f, q, t = template[max(0, min(idx, len(template) - 1))]
    return {"freq": f, "q": q, "gain": 0.0, "type": t, "enabled": True}


def _default_channel_bands() -> list[dict]:
    return [_default_eq_band(i) for i in range(NUM_EQ_BANDS)]


def _format_freq(hz: float) -> str:
    """Compact frequency label. Sub-1 kHz → 'NNN Hz', otherwise kHz."""
    if hz < 1000:
        return f"{int(round(hz))} Hz"
    khz = hz / 1000.0
    if abs(khz - round(khz)) < 0.05:
        return f"{int(round(khz))} kHz"
    return f"{khz:.1f} kHz"


def _band_name_for(freq: float) -> str:
    """Musical band name from centre frequency. Boundaries follow the
    common audio-engineering split — keeps labels meaningful even when a
    preset places bands at non-standard frequencies."""
    if freq < 60:
        return "Sub Bass"
    if freq < 120:
        return "Bass"
    if freq < 250:
        return "Low Bass"
    if freq < 500:
        return "Lower Mids"
    if freq < 1000:
        return "Low Mids"
    if freq < 2000:
        return "Mids"
    if freq < 4000:
        return "Upper Mids"
    if freq < 8000:
        return "Presence"
    if freq < 14000:
        return "Brilliance"
    return "Air"


class EqualizerTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._eq_enabled = False

        # Per-channel band data. Each channel carries its own
        # {freq, q, gain, type, enabled} list. Defaults match the Rust
        # daemon's default_channel_bands() so the GUI shows a sane shape
        # before the first status snapshot.
        self._bands_by_channel: dict[str, list[dict]] = {
            "game": _default_channel_bands(),
            "chat": _default_channel_bands(),
        }
        self._current_channel: str = "game"

        # Slider commits are debounced. While the user drags, we just
        # update the visible label — sending a daemon command per pixel
        # of slider travel queues hundreds of chain respawns and stalls
        # the GUI for minutes. The timer fires 250 ms after the last
        # change and flushes everything to the daemon in one shot.
        self._pending_band_value: dict[int, int] = {}
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(250)
        self._commit_timer.timeout.connect(self._commit_pending_changes)

        self._build_ui()
        self._render_sliders_for_channel(self._current_channel)

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(section_title(f"{NUM_EQ_BANDS}-band parametric EQ"))

        self.eq_check = QCheckBox("Enable parametric EQ (🎮 Game + 💬 Chat)")
        self.eq_check.setToolTip(
            "Inserts a PipeWire filter chain between the SteelGame and "
            "SteelChat sinks and the headset. The user-facing sinks stay "
            "put across toggles, so Discord and other apps don't lose "
            "their connection."
        )
        self.eq_check.toggled.connect(self._toggle_enabled)
        layout.addWidget(self.eq_check)

        # Per-channel selector: tune [Game] and [Chat] independently.
        # Sliders display the selected channel's bands; switching the
        # combo loads that channel's stored values. Emoji icons match
        # the Home-tab convention (🎮 / 💬).
        ch_row = QHBoxLayout()
        ch_row.addWidget(QLabel("Channel:"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(["🎮 Game", "💬 Chat"])
        self.channel_combo.setMinimumWidth(140)
        self.channel_combo.currentTextChanged.connect(self._on_channel_changed)
        ch_row.addWidget(self.channel_combo, 1)
        layout.addLayout(ch_row)

        # Preset row: searchable dropdown filtered by the current channel,
        # plus Load / Save / Delete actions. The combo is editable so the
        # user can type to filter (the QCompleter does substring match
        # against built-in + user preset names).
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setEditable(True)
        self.preset_combo.setInsertPolicy(QComboBox.NoInsert)
        completer = self.preset_combo.completer()
        if completer is not None:
            completer.setFilterMode(Qt.MatchContains)
            completer.setCompletionMode(QCompleter.PopupCompletion)
        self.preset_combo.setMinimumWidth(160)
        self.preset_combo.currentTextChanged.connect(self._on_preset_text_changed)
        preset_row.addWidget(self.preset_combo, 1)
        self.preset_load_btn = QPushButton("Load")
        self.preset_load_btn.clicked.connect(self._on_preset_load)
        self.preset_save_btn = QPushButton("Save…")
        self.preset_save_btn.clicked.connect(self._on_preset_save)
        self.preset_delete_btn = QPushButton("Delete")
        self.preset_delete_btn.clicked.connect(self._on_preset_delete)
        self.preset_delete_btn.setEnabled(False)
        preset_row.addWidget(self.preset_load_btn)
        preset_row.addWidget(self.preset_save_btn)
        preset_row.addWidget(self.preset_delete_btn)
        layout.addLayout(preset_row)
        # Populate the combo for the initial channel before any signals fire.
        self._refresh_preset_combo()

        # 10 vertical sliders, one per band. The musical name + frequency
        # labels are populated dynamically from the current channel's
        # band data — preset loads can move bands around without us
        # having to relabel manually.
        self.band_sliders: list[QSlider] = []
        self.band_value_labels: list[QLabel] = []
        self.band_name_labels: list[QLabel] = []
        self.band_freq_labels: list[QLabel] = []

        bands_row = QHBoxLayout()
        bands_row.setSpacing(4)
        for idx in range(NUM_EQ_BANDS):
            band_col = QVBoxLayout()
            band_col.setSpacing(3)
            band_col.setAlignment(Qt.AlignHCenter)

            value_lbl = QLabel("0.0")
            value_lbl.setAlignment(Qt.AlignCenter)
            value_lbl.setStyleSheet(
                "font-size: 10px; font-weight: bold; min-width: 36px;"
            )
            self.band_value_labels.append(value_lbl)
            band_col.addWidget(value_lbl)

            slider = QSlider(Qt.Vertical)
            # Slider unit = 0.1 dB. Range: -120 to 120 → -12.0 to +12.0 dB.
            slider.setRange(-120, 120)
            slider.setValue(0)
            slider.setTickPosition(QSlider.TicksRight)
            slider.setTickInterval(60)
            slider.setMinimumHeight(200)
            slider.setFixedWidth(28)
            band_num = idx + 1
            slider.valueChanged.connect(
                lambda v, b=band_num, lbl=value_lbl: self._on_slider_changed(b, v, lbl)
            )
            slider.sliderReleased.connect(
                lambda b=band_num, s=slider: self._on_slider_released(b, s)
            )
            self.band_sliders.append(slider)
            band_col.addWidget(slider, 0, alignment=Qt.AlignHCenter)

            name_lbl = QLabel("")
            name_lbl.setAlignment(Qt.AlignCenter)
            name_lbl.setStyleSheet("font-size: 9px; font-weight: bold;")
            name_lbl.setWordWrap(True)
            self.band_name_labels.append(name_lbl)
            band_col.addWidget(name_lbl)

            freq_lbl = QLabel("")
            freq_lbl.setAlignment(Qt.AlignCenter)
            freq_lbl.setStyleSheet(
                "font-size: 9px; color: palette(placeholder-text);"
            )
            self.band_freq_labels.append(freq_lbl)
            band_col.addWidget(freq_lbl)

            bands_row.addLayout(band_col)
        layout.addLayout(bands_row)

        eq_help = QLabel(
            "Drag a slider to boost or cut a frequency band by up to "
            "±12 dB. Each release respawns the filter chain with the new "
            "gains (~100 ms audio glitch per change). Live param updates "
            "without respawn are planned for a follow-up."
        )
        eq_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-top: 4px;"
        )
        eq_help.setWordWrap(True)
        layout.addWidget(eq_help)

        layout.addStretch(1)

    # ---------------------------------------------------- daemon-event hooks

    def on_enabled_changed(self, enabled: bool) -> None:
        self._eq_enabled = enabled
        was_blocked = self.eq_check.blockSignals(True)
        self.eq_check.setChecked(enabled)
        self.eq_check.blockSignals(was_blocked)

    def on_bands_changed(self, channel: str, bands: list) -> None:
        """Daemon broadcast: bands for `channel` changed (perhaps because
        we just sent the change, perhaps from another client or a preset
        load). Update the local cache; if it's the channel currently on
        screen, refresh sliders + labels too."""
        if channel not in self._bands_by_channel:
            return
        self._bands_by_channel[channel] = list(bands)
        if channel == self._current_channel:
            self._render_sliders_for_channel(channel)

    def on_full_state(self, state: dict) -> None:
        """Initial Status snapshot delivered both channels' band data at
        once. Cache both and refresh the visible sliders."""
        for ch in ("game", "chat"):
            if ch in state:
                self._bands_by_channel[ch] = list(state[ch])
        self._render_sliders_for_channel(self._current_channel)

    # ---------------------------------------------------------- input handlers

    def _toggle_enabled(self, checked: bool) -> None:
        self._daemon.send_command("set-eq-enabled", enabled=bool(checked))

    def _on_slider_changed(self, band: int, value_tenths: int, label: QLabel) -> None:
        """User moved a slider. Update the live value label, store the
        new value in the current channel's bands array, and queue a
        debounced daemon commit."""
        gain_db = value_tenths / 10.0
        sign = "+" if gain_db > 0 else ""
        label.setText(f"{sign}{gain_db:.1f}")
        bands = self._bands_by_channel[self._current_channel]
        if 1 <= band <= len(bands):
            bands[band - 1]["gain"] = gain_db
        self._pending_band_value[band] = value_tenths
        self._commit_timer.start()

    def _on_slider_released(self, band: int, slider: QSlider) -> None:
        """Slider released — commit *now* without waiting for the debounce."""
        self._pending_band_value[band] = slider.value()
        self._commit_timer.stop()
        self._commit_pending_changes()

    def _commit_pending_changes(self) -> None:
        """Flush queued band-gain changes to the daemon for the currently
        selected channel."""
        channel = self._current_channel
        for band, value_tenths in self._pending_band_value.items():
            gain_db = value_tenths / 10.0
            self._daemon.send_command(
                "set-eq-band-gain",
                channel=channel,
                band=band,
                gain_db=gain_db,
            )
        self._pending_band_value.clear()

    def _on_channel_changed(self, text: str) -> None:
        """Combo box changed — load the selected channel's stored bands
        into the sliders. The combo items carry emoji prefixes
        ('🎮 Game' / '💬 Chat'), so we extract the trailing word to map
        back to the daemon's channel keys ('game' / 'chat')."""
        last_word = text.strip().split()[-1].lower() if text.strip() else ""
        if last_word not in self._bands_by_channel:
            return
        self._current_channel = last_word
        # Cancel any pending commit from the previous channel.
        self._commit_timer.stop()
        self._pending_band_value.clear()
        self._render_sliders_for_channel(last_word)
        # Preset list is channel-scoped — repopulate the dropdown so the
        # user only sees presets for the channel they're looking at.
        self._refresh_preset_combo()

    # ---------------------------------------------------------------- presets

    def _refresh_preset_combo(self) -> None:
        """Rebuild the preset combo for the current channel. Block
        signals while we mutate the model so the active selection
        change doesn't fire spurious 'load this preset' edits."""
        was_blocked = self.preset_combo.blockSignals(True)
        try:
            self.preset_combo.clear()
            for preset in list_presets(self._current_channel):
                self.preset_combo.addItem(preset["name"])
        finally:
            self.preset_combo.blockSignals(was_blocked)
        self._update_delete_button()

    def _on_preset_text_changed(self, _text: str) -> None:
        # Delete is only meaningful for user-saved presets — toggle it
        # whenever the dropdown's selected name changes.
        self._update_delete_button()

    def _update_delete_button(self) -> None:
        name = self.preset_combo.currentText().strip()
        self.preset_delete_btn.setEnabled(
            bool(name) and is_user_preset(name, self._current_channel)
        )

    def _on_preset_load(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            return
        preset = find_preset(name, self._current_channel)
        if preset is None:
            QMessageBox.information(
                self,
                "Preset not found",
                f"No preset named '{name}' on the {self._current_channel} channel.",
            )
            return
        # Apply locally first so the sliders update without waiting on
        # the daemon round-trip — then send the atomic set-eq-channel so
        # the chain respawns once with the full preset.
        self._bands_by_channel[self._current_channel] = [
            dict(b) for b in preset["bands"]
        ]
        self._render_sliders_for_channel(self._current_channel)
        self._daemon.send_command(
            "set-eq-channel",
            channel=self._current_channel,
            bands=preset["bands"],
        )

    def _on_preset_save(self) -> None:
        suggested = self.preset_combo.currentText().strip() or "My Preset"
        name, ok = QInputDialog.getText(
            self,
            "Save preset",
            f"Save current {self._current_channel} EQ as:",
            text=suggested,
        )
        if not ok or not name.strip():
            return
        try:
            save_user_preset(
                name.strip(),
                self._current_channel,
                self._bands_by_channel[self._current_channel],
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save preset", str(e))
            return
        self._refresh_preset_combo()
        # Reselect the saved name so Delete becomes available immediately.
        idx = self.preset_combo.findText(name.strip())
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)

    def _on_preset_delete(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name or not is_user_preset(name, self._current_channel):
            return
        ok = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete preset '{name}' from the {self._current_channel} channel?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        delete_user_preset(name, self._current_channel)
        self._refresh_preset_combo()

    def _render_sliders_for_channel(self, channel: str) -> None:
        """Push the stored bands for `channel` into the slider widgets
        AND refresh the per-band name + frequency labels. Preset loads
        change frequencies, so the labels can't be static."""
        bands = self._bands_by_channel.get(channel) or _default_channel_bands()
        for idx in range(len(self.band_sliders)):
            band = bands[idx] if idx < len(bands) else _default_eq_band(idx)
            gain_db = float(band.get("gain", 0.0))
            freq = float(band.get("freq", 1000.0))

            slider = self.band_sliders[idx]
            value_lbl = self.band_value_labels[idx]
            name_lbl = self.band_name_labels[idx]
            freq_lbl = self.band_freq_labels[idx]

            value_tenths = int(round(gain_db * 10))
            was_blocked = slider.blockSignals(True)
            slider.setValue(value_tenths)
            slider.blockSignals(was_blocked)
            sign = "+" if gain_db > 0 else ""
            value_lbl.setText(f"{sign}{gain_db:.1f}")
            name_lbl.setText(_band_name_for(freq))
            freq_lbl.setText(_format_freq(freq))

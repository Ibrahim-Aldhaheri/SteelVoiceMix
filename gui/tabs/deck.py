"""Deck tab — base-station + headset hardware controls (OLED + ANC today)."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..daemon_client import (
    ANC_MODES,
    MIC_GAINS,
    PM_SHUTDOWN_VALUES,
    WIRELESS_MODES,
)
from ..widgets import (
    NoWheelComboBox,
    NoWheelSlider,
    alpha_badge,
    bind_debounced_slider,
    card,
    labelled_toggle,
    mode_picker,
)


def _check_button_silently(buttons: dict[str, QPushButton], key: str) -> None:
    """Set the matching button checked without firing its `clicked`
    signal — used by daemon-echo handlers so a round-trip from the
    daemon doesn't re-fire as another set-X command."""
    btn = buttons.get(key)
    if btn is None:
        return
    was_blocked = btn.blockSignals(True)
    try:
        btn.setChecked(True)
    finally:
        btn.blockSignals(was_blocked)


def _help_label(text: str) -> QLabel:
    """Standard small-italic placeholder-coloured help text under a card."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("font-size: 10px; color: palette(placeholder-text);")
    return lbl


def _value_label(text: str) -> QLabel:
    """Right-aligned, fixed-width value display next to a 1..10 slider."""
    lbl = QLabel(text)
    lbl.setFixedWidth(64)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lbl


def _ten_step_slider(initial: int, enabled: bool) -> NoWheelSlider:
    """Standard 1..10 horizontal slider with ticks and unit step."""
    s = NoWheelSlider(Qt.Horizontal)
    s.setRange(1, 10)
    s.setSingleStep(1)
    s.setPageStep(1)
    s.setTickInterval(1)
    s.setTickPosition(QSlider.TicksBelow)
    s.setValue(initial)
    s.setEnabled(enabled)
    return s


class DeckTab(QWidget):
    def __init__(self, daemon_client=None, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        # Two independent gates control whether the knob set is
        # interactive: oled_present (USB-side: deck plugged in?) and
        # deck_control_enabled (user-side: am I authorising daemon
        # writes?). Both must be true for the controls to fire.
        self._oled_present = False
        self._deck_control_enabled = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Status banner shown when the deck is not detected on USB.
        # Sits above the master toggle so the user sees it first.
        self._status_banner = QLabel(
            self.tr(
                "⚠ Deck not detected. Plug in the base station's USB cable; "
                "controls below stay disabled until the device shows up on the bus."
            )
        )
        self._status_banner.setWordWrap(True)
        self._status_banner.setAlignment(Qt.AlignCenter)
        self._status_banner.setStyleSheet(
            "background: rgba(255, 152, 0, 0.18);"
            "border: 1px solid rgba(255, 152, 0, 0.6);"
            "border-radius: 4px;"
            "color: palette(text);"
            "font-size: 11px;"
            "padding: 8px;"
        )
        layout.addWidget(self._status_banner)

        layout.addWidget(self._build_master_toggle_card())
        # Container so we can disable() the entire knob set in one
        # call when either gate (presence / control toggle) is off —
        # visually greyed and unclickable, no jumpy reflow.
        self._controls_container = QWidget()
        controls_layout = QVBoxLayout(self._controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)
        controls_layout.addWidget(self._build_oled_card())
        controls_layout.addWidget(self._build_anc_card())
        controls_layout.addWidget(self._build_wireless_card())
        controls_layout.addWidget(self._build_mic_hw_card())
        controls_layout.addWidget(self._build_pm_shutdown_card())
        self._controls_container.setEnabled(False)
        layout.addWidget(self._controls_container)

        layout.addStretch(1)

    # --------------------------------------------------------- helpers

    def _send(self, cmd: str, **kwargs) -> None:
        """Daemon-command sender that no-ops when the GUI was started
        without a daemon client (test harnesses, headless preview)."""
        if self._daemon is not None:
            self._daemon.send_command(cmd, **kwargs)

    def _bind_unit_slider(
        self,
        slider: QSlider,
        value_label: QLabel,
        send_with_level: Callable[[int], None],
    ) -> object:
        """Convenience for "1..10 slider with `N / 10` label"."""
        return bind_debounced_slider(
            self, slider, value_label, lambda v: f"{v} / 10", send_with_level,
        )

    # ----------------------------------------------------- card builders

    def _build_master_toggle_card(self) -> QWidget:
        # Defaults to OFF on a fresh install so a normal user's existing
        # device settings (set via SteelSeries GG, headset hardware
        # buttons, etc.) aren't silently overwritten on first launch.
        toggle_row, self.deck_control_toggle = labelled_toggle(
            self.tr("Allow SteelVoiceMix to manage deck settings"),
            tooltip=self.tr(
                "When off, the daemon never writes to the headset — it "
                "only reads state for display. Turn this on to let the "
                "controls below take effect on your device."
            ),
        )
        self.deck_control_toggle.setChecked(False)
        # Stays disabled until on_oled_presence_changed(True) confirms
        # the deck is on the bus. Pure preference toggle — keeping it
        # disabled when there's no device prevents the user from
        # flipping it expecting an immediate effect.
        self.deck_control_toggle.setEnabled(False)
        self.deck_control_toggle.toggled.connect(self._on_deck_control_toggled)
        return card(
            self.tr("Deck Control"),
            toggle_row,
            _help_label(self.tr(
                "Default off. Existing device settings (configured via "
                "SteelSeries GG, headset hardware buttons, or another "
                "tool) stay untouched until you opt in. Settings you "
                "configure below while this is off are remembered and "
                "applied as soon as you enable it."
            )),
        )

    def _build_oled_card(self) -> QWidget:
        row = QHBoxLayout()
        icon = QLabel("💡")
        icon.setFixedWidth(36)
        icon.setStyleSheet("font-size: 18px;")
        self.oled_brightness_slider = _ten_step_slider(8, self._daemon is not None)
        self.oled_brightness_value = _value_label("8 / 10")
        row.addWidget(icon)
        row.addWidget(self.oled_brightness_slider, 1)
        row.addWidget(self.oled_brightness_value)

        self._oled_brightness_timer = self._bind_unit_slider(
            self.oled_brightness_slider,
            self.oled_brightness_value,
            lambda v: self._send("set-oled-brightness", level=v),
        )

        return card(
            self.tr("OLED Brightness"),
            row,
            _help_label(self.tr(
                "Base-station screen brightness. Re-applied automatically "
                "on every reconnect — the deck does not remember this "
                "across power cycles."
            )),
        )

    def _build_anc_card(self) -> QWidget:
        # The headset's hardware ANC button cycles the same three modes;
        # the daemon mirrors hardware-button presses back via
        # on_anc_mode_changed.
        mode_row, self._anc_buttons, self._anc_button_group = mode_picker(
            self,
            (
                ("off", self.tr("Off")),
                ("transparent", self.tr("Transparent")),
                ("on", self.tr("ANC On")),
            ),
            initial="off",
            on_select=self._send_anc_mode,
            enabled=self._daemon is not None,
        )

        # Transparent intensity only audibly affects the headset in
        # transparent mode; gate the slider's enabled-state on that.
        slider_row = QHBoxLayout()
        slider_icon = QLabel("🎚")
        slider_icon.setFixedWidth(36)
        slider_icon.setStyleSheet("font-size: 16px;")
        self.anc_transparent_slider = _ten_step_slider(5, enabled=False)
        self.anc_transparent_value = _value_label("5 / 10")
        slider_row.addWidget(slider_icon)
        slider_row.addWidget(self.anc_transparent_slider, 1)
        slider_row.addWidget(self.anc_transparent_value)

        self._anc_transparent_timer = self._bind_unit_slider(
            self.anc_transparent_slider,
            self.anc_transparent_value,
            lambda v: self._send("set-anc-transparent-level", level=v),
        )

        return card(
            self.tr("Headset ANC"),
            mode_row,
            slider_row,
            _help_label(self.tr(
                "Active Noise Cancellation. The headset's hardware "
                "button cycles the same three modes; changes there "
                "reflect here automatically. Transparent intensity "
                "(1..10) only matters in Transparent mode."
            )),
        )

    def _build_wireless_card(self) -> QWidget:
        mode_row, self._wireless_buttons, self._wireless_button_group = mode_picker(
            self,
            (
                ("speed", self.tr("Speed (low latency)")),
                ("range", self.tr("Range (long distance)")),
            ),
            initial="speed",
            on_select=lambda m: self._send("set-wireless-mode", mode=m),
            enabled=self._daemon is not None,
        )
        return card(
            self.tr("Wireless Mode"),
            mode_row,
            _help_label(self.tr(
                "Switching modes briefly drops the wireless link. "
                "Bind a keyboard shortcut to "
                "<code>steelvoicemix-cli wireless-mode toggle</code> "
                "for a one-press flip when you walk to another room."
            )),
        )

    def _build_mic_hw_card(self) -> QWidget:
        gain_row = QHBoxLayout()
        gain_label = QLabel(self.tr("Gain"))
        gain_label.setFixedWidth(64)
        gain_row.addWidget(gain_label)
        picker_row, self._mic_gain_buttons, self._mic_gain_button_group = mode_picker(
            self,
            (("low", self.tr("Low")), ("high", self.tr("High"))),
            initial="high",
            on_select=lambda g: self._send("set-mic-gain", gain=g),
            enabled=self._daemon is not None,
        )
        # mode_picker returns a row layout; embed it inside our labelled row.
        gain_row.addLayout(picker_row, 1)

        # Mic volume — 1..10 (1=mute, 10=100%).
        vol_row = QHBoxLayout()
        vol_label = QLabel(self.tr("Volume"))
        vol_label.setFixedWidth(64)
        self.mic_volume_slider = _ten_step_slider(10, self._daemon is not None)
        self.mic_volume_value = _value_label("10 / 10")
        vol_row.addWidget(vol_label)
        vol_row.addWidget(self.mic_volume_slider, 1)
        vol_row.addWidget(self.mic_volume_value)
        self._mic_volume_timer = self._bind_unit_slider(
            self.mic_volume_slider,
            self.mic_volume_value,
            lambda v: self._send("set-mic-volume", level=v),
        )

        # Mic-mute LED brightness — red ring around the mic when muted.
        led_row = QHBoxLayout()
        led_label = QLabel(self.tr("Mute LED"))
        led_label.setFixedWidth(64)
        self.mic_led_slider = _ten_step_slider(10, self._daemon is not None)
        self.mic_led_value = _value_label("10 / 10")
        led_row.addWidget(led_label)
        led_row.addWidget(self.mic_led_slider, 1)
        led_row.addWidget(self.mic_led_value)
        self._mic_led_timer = self._bind_unit_slider(
            self.mic_led_slider,
            self.mic_led_value,
            lambda v: self._send("set-mic-led-brightness", level=v),
        )

        # ALPHA banner — these three writes are byte-exact ports from
        # ASM but the maintainer doesn't have a way to verify the
        # device actually honours them (no second mic to compare gain
        # / volume against, mute LED is hard to eyeball at granular
        # 1..10 steps). Surface that prominently so users go in with
        # the right expectations.
        alpha_row = QHBoxLayout()
        alpha_text = QLabel(self.tr(
            "Untested on hardware — bytes are byte-exact from ASM but "
            "the maintainer can't verify the device honours them. "
            "Report behaviour on GitHub if you try these."
        ))
        alpha_text.setWordWrap(True)
        alpha_text.setStyleSheet("font-size: 11px;")
        alpha_row.addWidget(alpha_badge(), 0, Qt.AlignTop)
        alpha_row.addWidget(alpha_text, 1)

        return card(
            self.tr("Microphone (hardware)"),
            alpha_row,
            gain_row, vol_row, led_row,
            _help_label(self.tr(
                "Hardware mic settings on the headset itself, written "
                "via the base-station HID. Distinct from the Microphone "
                "tab which controls software-side capture processing "
                "(gate, noise reduction, AI noise cancellation)."
            )),
        )

    def _build_pm_shutdown_card(self) -> QWidget:
        # 7 discrete options doesn't fit a slider; use a combobox.
        # Wire values stay raw ("never", "1m", ...) so the daemon
        # protocol round-trip is translation-stable.
        row = QHBoxLayout()
        icon = QLabel("⏻")
        icon.setFixedWidth(36)
        icon.setStyleSheet("font-size: 16px;")
        self.pm_shutdown_combo = NoWheelComboBox()
        for value, label in (
            ("never", self.tr("Never")),
            ("1m", self.tr("1 minute")),
            ("5m", self.tr("5 minutes")),
            ("10m", self.tr("10 minutes")),
            ("15m", self.tr("15 minutes")),
            ("30m", self.tr("30 minutes")),
            ("60m", self.tr("60 minutes")),
        ):
            self.pm_shutdown_combo.addItem(label, userData=value)
        self.pm_shutdown_combo.setCurrentIndex(
            PM_SHUTDOWN_VALUES.index("30m")  # GG default
        )
        self.pm_shutdown_combo.setEnabled(self._daemon is not None)
        self.pm_shutdown_combo.currentIndexChanged.connect(
            self._on_pm_shutdown_combo_changed
        )
        row.addWidget(icon)
        row.addWidget(self.pm_shutdown_combo, 1)

        return card(
            self.tr("Auto Power-Off"),
            row,
            _help_label(self.tr(
                "How long the headset stays powered on with no audio "
                "before auto-sleeping. Setting it to Never keeps it on "
                "indefinitely (drains battery faster)."
            )),
        )

    # ---------------------------------------------------- daemon-event hooks

    def on_oled_brightness_changed(self, level: int) -> None:
        clamped = max(1, min(10, int(level)))
        was_blocked = self.oled_brightness_slider.blockSignals(True)
        try:
            self.oled_brightness_slider.setValue(clamped)
        finally:
            self.oled_brightness_slider.blockSignals(was_blocked)
        self.oled_brightness_value.setText(f"{clamped} / 10")

    def on_oled_presence_changed(self, present: bool) -> None:
        self._oled_present = bool(present)
        self._status_banner.setVisible(not self._oled_present)
        # Master toggle stays clickable so the user can change their
        # preference even with the device offline; daemon will apply
        # on the next reconnect.
        self.deck_control_toggle.setEnabled(
            self._daemon is not None and self._oled_present
        )
        self._refresh_controls_enabled()

    def on_anc_mode_changed(self, mode: str) -> None:
        if mode not in ANC_MODES:
            return
        _check_button_silently(self._anc_buttons, mode)
        # Slider only useful in transparent mode.
        self.anc_transparent_slider.setEnabled(
            self._daemon is not None and mode == "transparent"
        )

    def on_anc_transparent_level_changed(self, level: int) -> None:
        clamped = max(1, min(10, int(level)))
        was_blocked = self.anc_transparent_slider.blockSignals(True)
        try:
            self.anc_transparent_slider.setValue(clamped)
        finally:
            self.anc_transparent_slider.blockSignals(was_blocked)
        self.anc_transparent_value.setText(f"{clamped} / 10")

    def on_wireless_mode_changed(self, mode: str) -> None:
        if mode in WIRELESS_MODES:
            _check_button_silently(self._wireless_buttons, mode)

    def on_mic_gain_changed(self, gain: str) -> None:
        if gain in MIC_GAINS:
            _check_button_silently(self._mic_gain_buttons, gain)

    def on_mic_volume_changed(self, level: int) -> None:
        clamped = max(1, min(10, int(level)))
        was_blocked = self.mic_volume_slider.blockSignals(True)
        try:
            self.mic_volume_slider.setValue(clamped)
        finally:
            self.mic_volume_slider.blockSignals(was_blocked)
        self.mic_volume_value.setText(f"{clamped} / 10")

    def on_mic_led_brightness_changed(self, level: int) -> None:
        clamped = max(1, min(10, int(level)))
        was_blocked = self.mic_led_slider.blockSignals(True)
        try:
            self.mic_led_slider.setValue(clamped)
        finally:
            self.mic_led_slider.blockSignals(was_blocked)
        self.mic_led_value.setText(f"{clamped} / 10")

    def on_pm_shutdown_changed(self, value: str) -> None:
        if value not in PM_SHUTDOWN_VALUES:
            return
        idx = PM_SHUTDOWN_VALUES.index(value)
        was_blocked = self.pm_shutdown_combo.blockSignals(True)
        try:
            self.pm_shutdown_combo.setCurrentIndex(idx)
        finally:
            self.pm_shutdown_combo.blockSignals(was_blocked)

    def on_deck_control_enabled_changed(self, enabled: bool) -> None:
        was_blocked = self.deck_control_toggle.blockSignals(True)
        try:
            self.deck_control_toggle.setChecked(bool(enabled))
        finally:
            self.deck_control_toggle.blockSignals(was_blocked)
        self._deck_control_enabled = bool(enabled)
        self._refresh_controls_enabled()

    # ------------------------------------------------------------ internals

    def _send_anc_mode(self, mode: str) -> None:
        if mode not in ANC_MODES:
            return
        self._send("set-anc-mode", mode=mode)
        # Local UI update — daemon will broadcast back, but we don't
        # want the slider's enabled state to lag the click.
        self.anc_transparent_slider.setEnabled(mode == "transparent")

    def _on_pm_shutdown_combo_changed(self, idx: int) -> None:
        if idx < 0 or idx >= len(PM_SHUTDOWN_VALUES):
            return
        self._send("set-pm-shutdown", value=PM_SHUTDOWN_VALUES[idx])

    def _on_deck_control_toggled(self, checked: bool) -> None:
        # Local UI update happens immediately; the daemon echo is
        # idempotent under the blockSignals guard in
        # on_deck_control_enabled_changed.
        self._deck_control_enabled = bool(checked)
        self._refresh_controls_enabled()
        self._send("set-deck-control-enabled", enabled=bool(checked))

    def _refresh_controls_enabled(self) -> None:
        # Both gates must be true for the knobs to fire daemon
        # commands. Setting the container un-enabled blocks all
        # children's signals as a side-effect.
        self._controls_container.setEnabled(
            self._oled_present and self._deck_control_enabled
        )

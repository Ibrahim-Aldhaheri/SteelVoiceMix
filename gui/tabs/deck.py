"""Deck tab — base-station + headset hardware controls (OLED + ANC today)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..widgets import NoWheelSlider, card

_ANC_MODES = ("off", "transparent", "on")
_WIRELESS_MODES = ("speed", "range")

# Stylesheet for the mode-picker buttons (ANC + Wireless). QPushButton's
# default checked state is visually identical to unchecked, so we
# colour the active mode with the app accent. Without this the user
# can't tell which mode is selected — especially confusing on first
# open.
_MODE_BUTTON_STYLE = """
QPushButton {
    padding: 6px 10px;
    border: 1px solid palette(mid);
    border-radius: 4px;
    background: palette(button);
}
QPushButton:hover {
    border-color: palette(highlight);
}
QPushButton:checked {
    background: palette(highlight);
    color: palette(highlighted-text);
    border-color: palette(highlight);
    font-weight: bold;
}
"""


class DeckTab(QWidget):
    def __init__(self, daemon_client=None, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(self._build_oled_card())
        layout.addWidget(self._build_anc_card())
        layout.addWidget(self._build_wireless_card())

        self._not_connected_hint = QLabel(
            self.tr(
                "Deck not detected. Plug in the base station's USB cable; "
                "your selection here will apply on connect."
            )
        )
        self._not_connected_hint.setWordWrap(True)
        self._not_connected_hint.setAlignment(Qt.AlignCenter)
        self._not_connected_hint.setStyleSheet(
            "color: palette(placeholder-text); font-size: 11px; padding: 8px;"
        )
        layout.addWidget(self._not_connected_hint)

        layout.addStretch(1)

        # Debounce slider drags — without this, every pixel of drag
        # sends a daemon command, which can reorder against the event
        # loop's once-per-iteration brightness apply.
        self._oled_send_timer = QTimer(self)
        self._oled_send_timer.setSingleShot(True)
        self._oled_send_timer.setInterval(120)
        self._oled_send_timer.timeout.connect(self._send_oled_brightness)
        self._oled_pending_level: int | None = None

        self._anc_send_timer = QTimer(self)
        self._anc_send_timer.setSingleShot(True)
        self._anc_send_timer.setInterval(120)
        self._anc_send_timer.timeout.connect(self._send_anc_transparent_level)
        self._anc_pending_level: int | None = None

    def _build_oled_card(self) -> QWidget:
        row = QHBoxLayout()
        icon = QLabel("💡")
        icon.setFixedWidth(36)
        icon.setStyleSheet("font-size: 18px;")

        self.oled_brightness_slider = NoWheelSlider(Qt.Horizontal)
        self.oled_brightness_slider.setRange(1, 10)
        self.oled_brightness_slider.setSingleStep(1)
        self.oled_brightness_slider.setPageStep(1)
        self.oled_brightness_slider.setTickInterval(1)
        self.oled_brightness_slider.setTickPosition(QSlider.TicksBelow)
        self.oled_brightness_slider.setValue(5)
        self.oled_brightness_slider.setEnabled(self._daemon is not None)
        self.oled_brightness_slider.valueChanged.connect(
            self._on_brightness_value_changed
        )

        self.oled_brightness_value = QLabel("5 / 10")
        self.oled_brightness_value.setFixedWidth(64)
        self.oled_brightness_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row.addWidget(icon)
        row.addWidget(self.oled_brightness_slider, 1)
        row.addWidget(self.oled_brightness_value)

        help_lbl = QLabel(
            self.tr(
                "Base-station screen brightness. Re-applied automatically "
                "on every reconnect — the deck does not remember this "
                "across power cycles."
            )
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        return card(self.tr("OLED Brightness"), row, help_lbl)

    def _build_anc_card(self) -> QWidget:
        # Mode picker — three exclusive buttons for off / transparent /
        # on. Maps 1:1 to the daemon's set-anc-mode command. The
        # headset's own hardware button cycles through the same three
        # states; the daemon's hardware-event listener pushes back into
        # this widget via on_anc_mode_changed.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self._anc_button_group = QButtonGroup(self)
        self._anc_button_group.setExclusive(True)
        self._anc_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("off", self.tr("Off")),
            ("transparent", self.tr("Transparent")),
            ("on", self.tr("ANC On")),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setEnabled(self._daemon is not None)
            btn.setStyleSheet(_MODE_BUTTON_STYLE)
            btn.clicked.connect(
                lambda _checked, m=mode: self._send_anc_mode(m)
            )
            self._anc_buttons[mode] = btn
            self._anc_button_group.addButton(btn)
            mode_row.addWidget(btn, 1)
        self._anc_buttons["off"].setChecked(True)

        # Transparent intensity slider — only audibly affects the
        # headset when mode == transparent, but the daemon accepts the
        # write regardless. Disabled visually outside transparent mode
        # to make the dependency obvious.
        slider_row = QHBoxLayout()
        slider_icon = QLabel("🎚")
        slider_icon.setFixedWidth(36)
        slider_icon.setStyleSheet("font-size: 16px;")
        self.anc_transparent_slider = NoWheelSlider(Qt.Horizontal)
        self.anc_transparent_slider.setRange(1, 10)
        self.anc_transparent_slider.setSingleStep(1)
        self.anc_transparent_slider.setPageStep(1)
        self.anc_transparent_slider.setTickInterval(1)
        self.anc_transparent_slider.setTickPosition(QSlider.TicksBelow)
        self.anc_transparent_slider.setValue(5)
        self.anc_transparent_slider.setEnabled(False)
        self.anc_transparent_slider.valueChanged.connect(
            self._on_anc_transparent_value_changed
        )
        self.anc_transparent_value = QLabel("5 / 10")
        self.anc_transparent_value.setFixedWidth(64)
        self.anc_transparent_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider_row.addWidget(slider_icon)
        slider_row.addWidget(self.anc_transparent_slider, 1)
        slider_row.addWidget(self.anc_transparent_value)

        help_lbl = QLabel(
            self.tr(
                "Active Noise Cancellation. The headset's hardware "
                "button cycles the same three modes; changes there "
                "reflect here automatically. Transparent intensity "
                "(1..10) only matters in Transparent mode."
            )
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        return card(
            self.tr("Headset ANC"), mode_row, slider_row, help_lbl,
        )

    def _build_wireless_card(self) -> QWidget:
        # Two-button picker: Speed (low latency, short range) vs Range
        # (long distance, slightly higher latency). Each switch briefly
        # drops the wireless link, so the daemon compares-and-skips
        # when the user clicks the already-active mode.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self._wireless_button_group = QButtonGroup(self)
        self._wireless_button_group.setExclusive(True)
        self._wireless_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("speed", self.tr("Speed (low latency)")),
            ("range", self.tr("Range (long distance)")),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setEnabled(self._daemon is not None)
            btn.setStyleSheet(_MODE_BUTTON_STYLE)
            btn.clicked.connect(
                lambda _checked, m=mode: self._send_wireless_mode(m)
            )
            self._wireless_buttons[mode] = btn
            self._wireless_button_group.addButton(btn)
            mode_row.addWidget(btn, 1)
        self._wireless_buttons["speed"].setChecked(True)

        help_lbl = QLabel(
            self.tr(
                "Switching modes briefly drops the wireless link. "
                "Bind a keyboard shortcut to "
                "<code>steelvoicemix-cli wireless-mode toggle</code> "
                "for a one-press flip when you walk to another room."
            )
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        return card(self.tr("Wireless Mode"), mode_row, help_lbl)

    def on_oled_brightness_changed(self, level: int) -> None:
        # Block signals so the daemon echo doesn't loop back as another
        # set-oled-brightness command.
        clamped = max(1, min(10, int(level)))
        was_blocked = self.oled_brightness_slider.blockSignals(True)
        try:
            self.oled_brightness_slider.setValue(clamped)
        finally:
            self.oled_brightness_slider.blockSignals(was_blocked)
        self.oled_brightness_value.setText(f"{clamped} / 10")

    def on_oled_presence_changed(self, present: bool) -> None:
        self._not_connected_hint.setVisible(not bool(present))

    def on_anc_mode_changed(self, mode: str) -> None:
        if mode not in _ANC_MODES:
            return
        btn = self._anc_buttons.get(mode)
        if btn is None:
            return
        # Block both the button's own signal and the group's so the
        # round-trip from daemon doesn't fire another set-anc-mode.
        was_blocked = btn.blockSignals(True)
        try:
            btn.setChecked(True)
        finally:
            btn.blockSignals(was_blocked)
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
        if mode not in _WIRELESS_MODES:
            return
        btn = self._wireless_buttons.get(mode)
        if btn is None:
            return
        was_blocked = btn.blockSignals(True)
        try:
            btn.setChecked(True)
        finally:
            btn.blockSignals(was_blocked)

    def _on_brightness_value_changed(self, value: int) -> None:
        self.oled_brightness_value.setText(f"{value} / 10")
        self._oled_pending_level = int(value)
        self._oled_send_timer.start()

    def _send_oled_brightness(self) -> None:
        if self._daemon is None or self._oled_pending_level is None:
            return
        self._daemon.send_command(
            "set-oled-brightness", level=self._oled_pending_level
        )
        self._oled_pending_level = None

    def _send_anc_mode(self, mode: str) -> None:
        if self._daemon is None or mode not in _ANC_MODES:
            return
        self._daemon.send_command("set-anc-mode", mode=mode)
        # Local UI update — the daemon will broadcast back, but we
        # don't want the slider's enabled state to lag the click.
        self.anc_transparent_slider.setEnabled(mode == "transparent")

    def _on_anc_transparent_value_changed(self, value: int) -> None:
        self.anc_transparent_value.setText(f"{value} / 10")
        self._anc_pending_level = int(value)
        self._anc_send_timer.start()

    def _send_anc_transparent_level(self) -> None:
        if self._daemon is None or self._anc_pending_level is None:
            return
        self._daemon.send_command(
            "set-anc-transparent-level", level=self._anc_pending_level
        )
        self._anc_pending_level = None

    def _send_wireless_mode(self, mode: str) -> None:
        if self._daemon is None or mode not in _WIRELESS_MODES:
            return
        self._daemon.send_command("set-wireless-mode", mode=mode)

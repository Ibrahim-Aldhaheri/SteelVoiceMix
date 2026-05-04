"""Deck tab — base-station hardware controls (OLED brightness today)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..widgets import NoWheelSlider, card


class DeckTab(QWidget):
    def __init__(self, daemon_client=None, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(self._build_oled_card())

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

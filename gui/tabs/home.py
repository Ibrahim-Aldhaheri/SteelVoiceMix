"""Home tab — live ChatMix balance bars + battery + hardware sidetone."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..widgets import card, make_bar


class HomeTab(QWidget):
    def __init__(self, daemon_client=None, parent=None):
        super().__init__(parent)
        # daemon_client is optional purely so existing call sites that
        # don't yet pass it (or tests that build the tab in isolation)
        # keep working — the sidetone slider just disables itself
        # without one.
        self._daemon = daemon_client

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # ChatMix card --------------------------------------------------
        game_row = QHBoxLayout()
        game_label = QLabel("🎮  Game")
        game_label.setFixedWidth(80)
        self.game_bar = make_bar("#4CAF50")
        game_row.addWidget(game_label)
        game_row.addWidget(self.game_bar)

        chat_row = QHBoxLayout()
        chat_label = QLabel("💬  Chat")
        chat_label.setFixedWidth(80)
        self.chat_bar = make_bar("#2196F3")
        chat_row.addWidget(chat_label)
        chat_row.addWidget(self.chat_bar)

        self.dial_label = QLabel("⚖️  Balanced")
        self.dial_label.setAlignment(Qt.AlignCenter)
        self.dial_label.setStyleSheet(
            "font-size: 12px; color: palette(placeholder-text);"
        )

        layout.addWidget(card("ChatMix", game_row, chat_row, self.dial_label))

        # Headset card — battery + hardware sidetone -------------------
        battery_row = QHBoxLayout()
        self.battery_label = QLabel("🔋  Battery")
        self.battery_label.setFixedWidth(90)
        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setValue(0)
        self.battery_bar.setTextVisible(True)
        self.battery_bar.setFormat("—")
        self._set_battery_chunk("#FF9800")
        battery_row.addWidget(self.battery_label)
        battery_row.addWidget(self.battery_bar)

        # Sidetone — hear yourself in the headset. The Arctis Nova Pro
        # Wireless takes 4 discrete levels (0/1/2/3); we expose 0..=128
        # to keep the UX consistent with HeadsetControl conventions.
        # Slider drags are debounced (250 ms idle + commit-on-release)
        # so each pixel of travel doesn't trigger a HID write +
        # save-state EEPROM flush.
        sidetone_row = QHBoxLayout()
        sidetone_lbl = QLabel("🎙️  Sidetone")
        sidetone_lbl.setFixedWidth(90)
        self.sidetone_slider = QSlider(Qt.Horizontal)
        self.sidetone_slider.setRange(0, 128)
        self.sidetone_slider.setValue(0)
        self.sidetone_slider.setEnabled(self._daemon is not None)
        self.sidetone_value = QLabel("0")
        self.sidetone_value.setFixedWidth(36)
        self.sidetone_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sidetone_slider.valueChanged.connect(self._on_sidetone_changed)
        self.sidetone_slider.sliderReleased.connect(self._on_sidetone_released)
        sidetone_row.addWidget(sidetone_lbl)
        sidetone_row.addWidget(self.sidetone_slider, 1)
        sidetone_row.addWidget(self.sidetone_value)

        sidetone_help = QLabel(
            "How loudly you hear your own mic in the headset. "
            "The Arctis Nova Pro Wireless has 4 internal levels — "
            "off / low / medium / high — and the slider quantises "
            "to whichever range it lands in."
        )
        sidetone_help.setWordWrap(True)
        sidetone_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(
            card("Headset", battery_row, sidetone_row, sidetone_help)
        )

        # Debounce timer for sidetone slider drags. Same pattern as the
        # EQ tab: while the user drags we just update the visible
        # label, and 250 ms after the last change we send the HID
        # command. Without this, every pixel writes to the headset's
        # EEPROM (save-state command runs after each HID write).
        self._sidetone_pending: int | None = None
        self._sidetone_commit_timer = QTimer(self)
        self._sidetone_commit_timer.setSingleShot(True)
        self._sidetone_commit_timer.setInterval(250)
        self._sidetone_commit_timer.timeout.connect(self._commit_sidetone)

        layout.addStretch(1)

    # ---------------------------------------------------- daemon-event hooks

    def on_chatmix(self, game_vol: int, chat_vol: int) -> None:
        self.game_bar.setValue(game_vol)
        self.chat_bar.setValue(chat_vol)

        diff = game_vol - chat_vol
        if abs(diff) < 10:
            label = "⚖️  Balanced"
        elif diff > 0:
            label = f"🎮  Game +{diff}"
        else:
            label = f"💬  Chat +{-diff}"
        self.dial_label.setText(label)

    def on_disconnected(self) -> None:
        self.game_bar.setValue(0)
        self.chat_bar.setValue(0)
        self.dial_label.setText("⚖️  —")

    def on_battery(self, level: int, status: str) -> None:
        self.battery_bar.setValue(level)
        if status == "charging":
            self.battery_bar.setFormat(f"⚡ {level}%")
            chunk = "#4CAF50"
        elif status == "offline":
            self.battery_bar.setFormat("Offline")
            self.battery_bar.setValue(0)
            chunk = "#FF9800"
        else:
            self.battery_bar.setFormat(f"{level}%")
            chunk = "#4CAF50" if level > 50 else "#FF9800" if level > 20 else "#f44336"
        self._set_battery_chunk(chunk)

    def on_sidetone_changed(self, level: int) -> None:
        """Daemon broadcast: the persisted sidetone level changed
        (e.g. status snapshot on connect, or another GUI client set
        it). Re-apply to the slider with signals blocked so the echo
        doesn't loop back as another set-sidetone command."""
        was_blocked = self.sidetone_slider.blockSignals(True)
        try:
            self.sidetone_slider.setValue(level)
        finally:
            self.sidetone_slider.blockSignals(was_blocked)
        self.sidetone_value.setText(str(level))

    # --------------------------------------------------------- handlers

    def _on_sidetone_changed(self, value: int) -> None:
        self.sidetone_value.setText(str(value))
        self._sidetone_pending = value
        self._sidetone_commit_timer.start()

    def _on_sidetone_released(self) -> None:
        self._sidetone_pending = self.sidetone_slider.value()
        self._sidetone_commit_timer.stop()
        self._commit_sidetone()

    def _commit_sidetone(self) -> None:
        if self._daemon is None or self._sidetone_pending is None:
            return
        level = int(self._sidetone_pending)
        self._sidetone_pending = None
        self._daemon.send_command("set-sidetone", level=level)

    def _set_battery_chunk(self, chunk: str) -> None:
        self.battery_bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 6px; "
            "height: 22px; text-align: center; background: palette(base); }"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 5px; }}"
        )

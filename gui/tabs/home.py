"""Home tab — live ChatMix balance bars + battery indicator."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from ..widgets import card, make_bar


class HomeTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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

        # Headset card --------------------------------------------------
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

        layout.addWidget(card("Headset", battery_row))

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

    def _set_battery_chunk(self, chunk: str) -> None:
        self.battery_bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 6px; "
            "height: 22px; text-align: center; background: palette(base); }"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 5px; }}"
        )

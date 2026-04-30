"""Home tab — status dashboard. ChatMix balance + battery + a
features-at-a-glance pill row showing what processing is live.

Layout uses a two-column grid on the top half so a maximised window
fills with content rather than a lonely strip of bars: ChatMix on
the left, headset / battery on the right, both balanced at the
same visual weight. The status row underneath is a single full-
width card with one pill per feature (EQ / Surround / Media /
HDMI / Mic-NR / Mic-Gate / Mic-AI). Pills colour green when active,
neutral when inactive, so the user can sanity-check what's running
without clicking through tabs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ..widgets import ACCENT, card, make_bar


class _StatusPill(QLabel):
    """Small rounded label that flips colour when `set_active(True)`.
    Used in the bottom row to show which features are currently on.
    Built on QLabel rather than QPushButton so it's clearly read-
    only; the user changes state on the relevant feature tab."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_active(False)

    def set_active(self, active: bool) -> None:
        if active:
            bg = ACCENT
            fg = "white"
            border = ACCENT
        else:
            bg = "transparent"
            fg = "palette(placeholder-text)"
            border = "palette(mid)"
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; "
            f"border: 1px solid {border}; "
            "border-radius: 10px; padding: 4px 12px; "
            "font-size: 10px; font-weight: bold;"
        )


class HomeTab(QWidget):
    def __init__(self, daemon_client=None, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Top row: ChatMix + Headset cards side-by-side. QGridLayout
        # gives each column equal weight (setColumnStretch) so the
        # cards expand together when the window grows. On a window
        # narrower than ~640 px Qt automatically squashes them — still
        # readable, just cosier.
        top_grid = QGridLayout()
        top_grid.setSpacing(12)
        top_grid.setColumnStretch(0, 1)
        top_grid.setColumnStretch(1, 1)
        top_grid.addWidget(self._build_chatmix_card(), 0, 0)
        top_grid.addWidget(self._build_headset_card(), 0, 1)
        layout.addLayout(top_grid)

        # Bottom row: full-width status pill grid.
        layout.addWidget(self._build_status_card())

        layout.addStretch(1)

    # --------------------------------------------------------- card builders

    def _build_chatmix_card(self) -> QWidget:
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
            "font-size: 14px; font-weight: bold; padding-top: 4px;"
        )
        return card("ChatMix", game_row, chat_row, self.dial_label)

    def _build_headset_card(self) -> QWidget:
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

        self.battery_status = QLabel("Headset disconnected")
        self.battery_status.setAlignment(Qt.AlignCenter)
        self.battery_status.setStyleSheet(
            "font-size: 14px; font-weight: bold; padding-top: 4px;"
        )
        return card("Headset", battery_row, self.battery_status)

    def _build_status_card(self) -> QWidget:
        # Active-features pill row — the user sees at a glance which
        # processing is live without clicking through Equalizer /
        # Surround / Microphone / Sinks. Pills are read-only; flip
        # them on the relevant tab.
        self._pills: dict[str, _StatusPill] = {
            "eq": _StatusPill("EQ"),
            "surround": _StatusPill("Surround"),
            "media": _StatusPill("Media sink"),
            "hdmi": _StatusPill("HDMI sink"),
            "mic_gate": _StatusPill("Mic Gate"),
            "mic_nr": _StatusPill("Mic NR"),
            "mic_ai": _StatusPill("Mic AI-NC"),
        }
        pill_row = QHBoxLayout()
        pill_row.setSpacing(8)
        for pill in self._pills.values():
            pill_row.addWidget(pill)
        pill_row.addStretch(1)

        return card("Active Features", pill_row)

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
            text = "Charging"
        elif status == "offline":
            self.battery_bar.setFormat("Offline")
            self.battery_bar.setValue(0)
            chunk = "#FF9800"
            text = "Headset offline"
        else:
            self.battery_bar.setFormat(f"{level}%")
            chunk = "#4CAF50" if level > 50 else "#FF9800" if level > 20 else "#f44336"
            text = (
                "Battery good" if level > 50
                else "Battery low" if level > 20
                else "Battery critical"
            )
        self._set_battery_chunk(chunk)
        self.battery_status.setText(text)

    # ------------------------------------------------------- pill bindings

    def on_eq_enabled(self, enabled: bool) -> None:
        self._pills["eq"].set_active(enabled)

    def on_surround_enabled(self, enabled: bool) -> None:
        self._pills["surround"].set_active(enabled)

    def on_media_enabled(self, enabled: bool) -> None:
        self._pills["media"].set_active(enabled)

    def on_hdmi_enabled(self, enabled: bool) -> None:
        self._pills["hdmi"].set_active(enabled)

    def on_mic_state(self, state: dict) -> None:
        gate = bool(((state.get("noise_gate") or {}).get("enabled")))
        nr = bool(((state.get("noise_reduction") or {}).get("enabled")))
        ai = bool(((state.get("ai_noise_cancellation") or {}).get("enabled")))
        self._pills["mic_gate"].set_active(gate)
        self._pills["mic_nr"].set_active(nr)
        self._pills["mic_ai"].set_active(ai)

    # --------------------------------------------------------- internals

    def _set_battery_chunk(self, chunk: str) -> None:
        self.battery_bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 6px; "
            "height: 22px; text-align: center; background: palette(base); }"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 5px; }}"
        )

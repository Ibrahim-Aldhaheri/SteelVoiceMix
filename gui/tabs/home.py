"""Home tab — the dashboard a non-technical user lands on.

It answers three questions at a glance, top to bottom:

  1. Is my headset working?   -> device hero card (name, status, battery)
  2. How is my mix set?       -> ChatMix balance
  3. What processing is on?   -> active-features row

...and offers one-click shortcuts to the things people do most.

Two "connected" states used to collide here: the window header shows
the background service link, while this page shows the headset itself,
and both said "Connected"/"disconnected" with equal weight. The hero
now owns the *headset* story in plain language ("Ready", "Headset is
off", "Not detected") and never borrows the service wording, so the two
indicators read as the distinct things they are.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..widgets import (
    ACCENT,
    ERROR,
    WARN,
    card,
    help_text,
    make_bar,
    quick_action,
    section_title,
    stat_readout,
)


class _StatusPill(QLabel):
    """Read-only rounded label that flips colour when active. Shows
    which processing is live; the user changes it on the feature tab."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._label = text
        self.setAlignment(Qt.AlignCenter)
        self.setTextFormat(Qt.PlainText)
        self.set_active(False)

    def set_active(self, active: bool, available: bool = True) -> None:
        self.setText(
            self.tr("{name}: On").format(name=self._label) if active and available
            else self.tr("{name}: Set up").format(name=self._label) if active
            else self.tr("{name}: Off").format(name=self._label)
        )
        self.setAccessibleName(self.text())
        if active and available:
            bg, fg, border = ACCENT, "#0E1A0F", ACCENT
        else:
            bg, fg, border = "transparent", "palette(placeholder-text)", "palette(mid)"
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {border}; "
            "border-radius: 10px; padding: 4px 12px; "
            "font-size: 11px; font-weight: bold;"
        )


class HomeTab(QWidget):
    # Emitted when a quick-action asks to jump to another page. The
    # window connects this to the sidebar so the dashboard doubles as
    # a launcher.
    navigate = Signal(str)

    def __init__(self, daemon_client=None, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._oled_present = False
        self._service_up = False
        self._hardware_available = False
        self._feature_states: dict[str, bool] = {}

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(self._build_device_hero())

        mid = QGridLayout()
        mid.setSpacing(12)
        mid.setColumnStretch(0, 3)
        mid.setColumnStretch(1, 2)
        mid.addWidget(self._build_chatmix_card(), 0, 0)
        mid.addWidget(self._build_status_card(), 0, 1)
        layout.addLayout(mid)

        layout.addWidget(self._build_quick_actions())
        layout.addStretch(1)

    # --------------------------------------------------------- hero

    def _build_device_hero(self) -> QWidget:
        """Full-width headset summary: name, plain-language status, and
        a big battery readout. This is the single source of truth for
        'is my headset working', so nothing else needs to repeat it."""
        row = QHBoxLayout()
        row.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {WARN}; font-size: 14px;")
        self.device_name = QLabel(self.tr("Arctis Nova Pro Wireless"))
        self.device_name.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.device_name.setTextFormat(Qt.PlainText)
        name_row.addWidget(self._dot)
        name_row.addWidget(self.device_name)
        name_row.addStretch(1)
        self.device_status = help_text(self.tr("Looking for your headset…"))
        left.addLayout(name_row)
        left.addWidget(self.device_status)

        self._battery_box, self.battery_value = stat_readout(
            "—", self.tr("battery")
        )

        row.addLayout(left, 1)
        row.addWidget(self._battery_box, 0, Qt.AlignVCenter)

        hero = card(None, row)
        return hero

    # --------------------------------------------------------- cards

    def _build_chatmix_card(self) -> QWidget:
        intro = help_text(
            self.tr(
                "ChatMix balances game audio against chat/voice using the "
                "dial on the base station."
            )
        )

        game_row = QHBoxLayout()
        game_label = QLabel(self.tr("Game"))
        game_label.setFixedWidth(64)
        self.game_bar = make_bar(ACCENT)
        game_row.addWidget(game_label)
        game_row.addWidget(self.game_bar)

        chat_row = QHBoxLayout()
        chat_label = QLabel(self.tr("Chat"))
        chat_label.setFixedWidth(64)
        self.chat_bar = make_bar("#2196F3")
        chat_row.addWidget(chat_label)
        chat_row.addWidget(self.chat_bar)

        self.dial_label = QLabel(self.tr("Balanced"))
        self.dial_label.setAlignment(Qt.AlignCenter)
        self.dial_label.setTextFormat(Qt.PlainText)
        self.dial_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; padding-top: 4px;"
        )
        return card(self.tr("ChatMix"), intro, game_row, chat_row, self.dial_label)

    def _build_status_card(self) -> QWidget:
        self._pills: dict[str, _StatusPill] = {
            "eq": _StatusPill(self.tr("EQ")),
            "surround": _StatusPill(self.tr("Surround")),
            "media": _StatusPill(self.tr("Media")),
            "hdmi": _StatusPill(self.tr("HDMI")),
            "mic_gate": _StatusPill(self.tr("Gate")),
            "mic_nr": _StatusPill(self.tr("Noise")),
            "mic_ai": _StatusPill(self.tr("AI-NC")),
        }
        grid = QGridLayout()
        grid.setSpacing(6)
        for i, pill in enumerate(self._pills.values()):
            grid.addWidget(pill, i // 2, i % 2)
        wrap = QWidget()
        wrap.setLayout(grid)
        return card(
            self.tr("Sound features"),
            help_text(self.tr(
                "On means available to use now. Set up means enabled, but the "
                "headset is currently unavailable."
            )),
            wrap,
        )

    def _build_quick_actions(self) -> QWidget:
        actions = [
            (("media-equalizer", "view-media-equalizer"),
             self.tr("Adjust EQ"),
             self.tr("Tune the sound per channel"), "equalizer"),
            (("audio-input-microphone", "microphone"),
             self.tr("Set up microphone"),
             self.tr("Noise removal, gate, levels"), "microphone"),
            (("audio-volume-high", "audio-speakers"),
             self.tr("Manage outputs"),
             self.tr("Virtual sinks and routing"), "sinks"),
            (("configure", "preferences-system"),
             self.tr("Settings"),
             self.tr("Appearance, startup, more"), "settings"),
        ]
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, (icons, title, sub, target) in enumerate(actions):
            btn = quick_action(icons, title, sub)
            btn.clicked.connect(lambda _=False, t=target: self.navigate.emit(t))
            grid.addWidget(btn, i // 2, i % 2)
            grid.setColumnStretch(i % 2, 1)
        wrap = QWidget()
        wrap.setLayout(grid)
        header = QVBoxLayout()
        header.setSpacing(8)
        header.addWidget(section_title(self.tr("Quick actions")))
        header.addWidget(wrap)
        holder = QWidget()
        holder.setLayout(header)
        return holder

    # ---------------------------------------------------- daemon-event hooks

    def on_service_connected(self, up: bool) -> None:
        """Window tells us whether the background service link is up.
        When it drops, everything the daemon reports is stale, so the
        hero shows the service problem rather than a frozen battery."""
        self._service_up = up
        if not up:
            self._set_hardware_available(False)
            self._set_hero(
                WARN,
                self.tr("Reconnecting to the background service…"),
                "—",
            )

    def on_chatmix(self, game_vol: int, chat_vol: int) -> None:
        self.game_bar.setValue(game_vol)
        self.chat_bar.setValue(chat_vol)
        diff = game_vol - chat_vol
        if abs(diff) < 10:
            self.dial_label.setText(self.tr("Balanced"))
        elif diff > 0:
            self.dial_label.setText(self.tr("Game +{}").format(diff))
        else:
            self.dial_label.setText(self.tr("Chat +{}").format(-diff))

    def on_disconnected(self) -> None:
        self.game_bar.setValue(0)
        self.chat_bar.setValue(0)
        self.dial_label.setText(self.tr("—"))

    def on_oled_presence_changed(self, present: bool) -> None:
        self._oled_present = bool(present)
        if not self._oled_present:
            self._set_hardware_available(False)
            self._set_hero(
                ERROR,
                self.tr(
                    "Not detected — check the base station is plugged in "
                    "over USB."
                ),
                "—",
            )

    def on_battery(self, level: int, status: str) -> None:
        # A real battery reading proves the headset is reachable, so it
        # supersedes any earlier "not detected" — don't gate on the
        # presence event having arrived first (event ordering isn't
        # guaranteed, and a dropped update used to strand the hero on
        # "Looking for your headset…").
        if status in ("charging", "discharging", "normal", ""):
            self._oled_present = True
        if status == "charging":
            self._set_hardware_available(True)
            self._set_hero(ACCENT, self.tr("Charging"), f"{level}%")
        elif status == "offline":
            self._set_hardware_available(False)
            self._set_hero(
                WARN, self.tr("Headset is off — turn it on to use it"), "—"
            )
        else:
            self._set_hardware_available(True)
            colour = ACCENT if level > 50 else WARN if level > 20 else ERROR
            note = (
                self.tr("Ready") if level > 50
                else self.tr("Battery low") if level > 20
                else self.tr("Battery critical — charge soon")
            )
            self._set_hero(colour, note, f"{level}%")

    # ------------------------------------------------------- pill bindings

    def on_eq_enabled(self, enabled: bool) -> None:
        self._set_feature("eq", enabled)

    def on_surround_enabled(self, enabled: bool) -> None:
        self._set_feature("surround", enabled)

    def on_media_enabled(self, enabled: bool) -> None:
        self._set_feature("media", enabled)

    def on_hdmi_enabled(self, enabled: bool) -> None:
        self._set_feature("hdmi", enabled)

    def on_mic_state(self, state: dict) -> None:
        gate = bool((state.get("noise_gate") or {}).get("enabled"))
        nr = bool((state.get("noise_reduction") or {}).get("enabled"))
        ai = bool((state.get("ai_noise_cancellation") or {}).get("enabled"))
        self._set_feature("mic_gate", gate)
        self._set_feature("mic_nr", nr)
        self._set_feature("mic_ai", ai)

    def _set_feature(self, key: str, enabled: bool) -> None:
        self._feature_states[key] = bool(enabled)
        self._pills[key].set_active(bool(enabled), self._hardware_available)

    def _set_hardware_available(self, available: bool) -> None:
        self._hardware_available = bool(available) and self._service_up
        for key, pill in self._pills.items():
            pill.set_active(self._feature_states.get(key, False), self._hardware_available)

    # --------------------------------------------------------- internals

    def _set_hero(self, dot_colour: str, status: str, battery: str) -> None:
        self._dot.setStyleSheet(f"color: {dot_colour}; font-size: 14px;")
        self.device_status.setText(status)
        self.battery_value.setText(battery)

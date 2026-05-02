"""Sinks tab — Media + HDMI virtual-sink toggles, browser auto-routing,
per-channel digital volume boost."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..widgets import (
    NoWheelSlider,
    ToggleSwitch,
    alpha_badge,
    card,
    labelled_toggle,
)


# Slider position above which a clipping warning is shown next to the
# row. Below this, mild headroom amplification is generally safe; above
# it the user is into territory where loud passages can clip. 150 lines
# up with the user's "warning if increasing it too much" requirement.
_BOOST_WARN_THRESHOLD = 150


class _ChannelBoostRow:
    """Per-channel boost row: emoji label + on/off toggle + slider +
    percent readout + warning lamp. Owns the four widgets that make up
    one channel's worth of boost UI and exposes a couple of helpers
    so SinksTab doesn't have to track each widget by name.

    Disabled state means: toggle off or the channel's sink isn't loaded
    (Media/HDMI). When disabled, the slider is dimmed and the warning
    is hidden regardless of where the slider sits."""

    def __init__(
        self,
        channel: str,
        emoji_label: str,
        daemon_client,
    ):
        self.channel = channel
        self._daemon = daemon_client
        self._available = True
        # Internal flag: when True, _on_toggle / _on_slider don't push to
        # the daemon (used while we're applying daemon-pushed state).
        self._suppress = False

        self.layout = QHBoxLayout()
        self.layout.setSpacing(8)

        self.label = QLabel(emoji_label)
        self.label.setFixedWidth(80)
        self.layout.addWidget(self.label)

        self.toggle = ToggleSwitch()
        self.toggle.setToolTip(
            f"Enable digital volume boost for the {channel.title()} channel"
        )
        self.toggle.toggled.connect(self._on_toggle)
        self.layout.addWidget(self.toggle, 0, Qt.AlignVCenter)

        self.slider = NoWheelSlider(Qt.Horizontal)
        self.slider.setRange(100, 200)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(10)
        self.slider.setValue(100)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider)
        self.layout.addWidget(self.slider, 1)

        self.readout = QLabel("100%")
        self.readout.setFixedWidth(48)
        self.readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.layout.addWidget(self.readout)

        # Warning indicator — only visible when slider is past the
        # threshold AND the boost is enabled.
        self.warn = QLabel("⚠ clipping risk")
        self.warn.setStyleSheet("color: #FF9800; font-size: 10px; font-weight: bold;")
        self.warn.setVisible(False)
        self.layout.addWidget(self.warn, 0, Qt.AlignVCenter)

    # ----- daemon-event-driven state apply --------------------------------

    def apply_state(self, enabled: bool, multiplier_pct: int) -> None:
        self._suppress = True
        self.toggle.setChecked(bool(enabled))
        pct = max(100, min(200, int(multiplier_pct)))
        self.slider.setValue(pct)
        self.readout.setText(f"{pct}%")
        self.slider.setEnabled(enabled and self._available)
        self._update_warning(enabled=enabled, pct=pct)
        self._suppress = False

    def set_available(self, available: bool) -> None:
        """Media/HDMI rows are only operable when their sink is loaded.
        When the sink is gone, the row stays visible but disabled to
        make the relationship obvious without dropping rows in/out."""
        if self._available == available:
            return
        self._available = available
        self.toggle.setEnabled(available)
        self.slider.setEnabled(available and self.toggle.isChecked())
        if not available:
            # Keep stored multiplier_pct as-is so the user's chosen
            # boost survives a sink toggle round-trip.
            self.warn.setVisible(False)
            tip = (
                f"Add the {self.channel.title()} sink first to use boost"
            )
            self.toggle.setToolTip(tip)
        else:
            self.toggle.setToolTip(
                f"Enable digital volume boost for the {self.channel.title()} channel"
            )

    # ----- input handlers --------------------------------------------------

    def _on_toggle(self, checked: bool) -> None:
        self.slider.setEnabled(bool(checked) and self._available)
        self._update_warning(enabled=checked, pct=self.slider.value())
        if self._suppress:
            return
        self._daemon.send_command(
            "set-channel-boost",
            channel=self.channel,
            enabled=bool(checked),
            multiplier_pct=int(self.slider.value()),
        )

    def _on_slider(self, value: int) -> None:
        self.readout.setText(f"{int(value)}%")
        self._update_warning(enabled=self.toggle.isChecked(), pct=int(value))
        if self._suppress:
            return
        # Only push to the daemon when the toggle is on — otherwise we'd
        # spam set-channel-boost with enabled=false and the daemon would
        # quietly accept multiplier-only changes. Storing the slider
        # locally and committing on toggle is cleaner.
        if self.toggle.isChecked():
            self._daemon.send_command(
                "set-channel-boost",
                channel=self.channel,
                enabled=True,
                multiplier_pct=int(value),
            )

    def _update_warning(self, *, enabled: bool, pct: int) -> None:
        self.warn.setVisible(bool(enabled) and pct > _BOOST_WARN_THRESHOLD)


class SinksTab(QWidget):
    def __init__(self, daemon_client, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._media_enabled = False
        self._hdmi_enabled = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Virtual sinks card -------------------------------------------
        media_row = QHBoxLayout()
        media_lbl = QLabel(self.tr("🎵  Media"))
        media_lbl.setFixedWidth(80)
        self.media_btn = QPushButton("Add Media")
        self.media_btn.clicked.connect(self._toggle_media)
        media_row.addWidget(media_lbl)
        media_row.addWidget(self.media_btn, 1)

        # HDMI is marked ALPHA — author hasn't actually run it through
        # a real TV/AVR yet. Functionality wires up; the badge tells
        # users to expect rough edges until it's hardware-verified.
        hdmi_row = QHBoxLayout()
        hdmi_lbl = QLabel(self.tr("📺  HDMI"))
        hdmi_lbl.setFixedWidth(80)
        hdmi_alpha = alpha_badge(
            tooltip=(
                "Alpha — not yet hardware-verified against a real "
                "HDMI sink (TV / AVR)."
            )
        )
        self.hdmi_btn = QPushButton("Add HDMI")
        self.hdmi_btn.clicked.connect(self._toggle_hdmi)
        hdmi_row.addWidget(hdmi_lbl)
        hdmi_row.addWidget(hdmi_alpha, 0)
        hdmi_row.addWidget(self.hdmi_btn, 1)

        sinks_help = QLabel(
            "Media and HDMI sinks bypass the ChatMix dial — useful for "
            "music, browsers, or routing audio to a TV/AVR independently "
            "of the headset."
        )
        sinks_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        sinks_help.setWordWrap(True)

        layout.addWidget(card(self.tr("Virtual Sinks"), media_row, hdmi_row, sinks_help))

        # Auto-routing card --------------------------------------------
        # Marked ALPHA — author hasn't pushed on it and treats it as
        # nice-to-have (per the user's flagged-as-experimental note).
        auto_row, self.auto_route_toggle = labelled_toggle(
            "Route browsers and media players to SteelMedia automatically",
            tooltip=(
                "Alpha — lightly tested. When enabled, the daemon moves "
                "new browser and media-player audio streams (Firefox, "
                "Chromium, mpv, VLC…) to the SteelMedia sink so they "
                "bypass the ChatMix dial. Manual moves stick — the "
                "daemon only acts on first-seen streams."
            ),
            badge="ALPHA",
        )
        self.auto_route_toggle.toggled.connect(self._toggle_auto_route)

        layout.addWidget(card(self.tr("Auto-Routing"), auto_row))

        # Volume boost card --------------------------------------------
        # One row per output channel. Game/Chat are always available
        # (they're the headset's own sinks). Media/HDMI follow the
        # corresponding sink's loaded state — see _refresh_boost_avail.
        self._boost_rows: dict[str, _ChannelBoostRow] = {
            "game": _ChannelBoostRow("game", self.tr("🎮  Game"), daemon_client),
            "chat": _ChannelBoostRow("chat", self.tr("💬  Chat"), daemon_client),
            "media": _ChannelBoostRow("media", self.tr("🎵  Media"), daemon_client),
            "hdmi": _ChannelBoostRow("hdmi", self.tr("📺  HDMI"), daemon_client),
        }
        boost_help = QLabel(
            "Digital amplification applied at the sink — use when an app "
            "is too quiet even at the system maximum. Headroom above 150% "
            "can introduce clipping; back off if loud passages distort."
        )
        boost_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        boost_help.setWordWrap(True)
        layout.addWidget(
            card(
                self.tr("Volume Boost"),
                self._boost_rows["game"].layout,
                self._boost_rows["chat"].layout,
                self._boost_rows["media"].layout,
                self._boost_rows["hdmi"].layout,
                boost_help,
            )
        )
        self._refresh_boost_avail()

        layout.addStretch(1)

    # ------------------------------------------------- public state queries

    @property
    def media_enabled(self) -> bool:
        return self._media_enabled

    @property
    def hdmi_enabled(self) -> bool:
        return self._hdmi_enabled

    # ---------------------------------------------------- daemon-event hooks

    def on_media_changed(self, enabled: bool) -> None:
        self._media_enabled = enabled
        self.media_btn.setText("Remove Media" if enabled else "Add Media")
        self.media_btn.setToolTip(
            "Destroy the SteelMedia virtual sink"
            if enabled
            else "Create a SteelMedia virtual sink that bypasses the ChatMix dial"
        )
        self._refresh_boost_avail()

    def on_hdmi_changed(self, enabled: bool) -> None:
        self._hdmi_enabled = enabled
        self.hdmi_btn.setText("Remove HDMI" if enabled else "Add HDMI")
        self.hdmi_btn.setToolTip(
            "Destroy the SteelHDMI virtual sink"
            if enabled
            else "Create a SteelHDMI virtual sink that loops to your HDMI output"
        )
        self._refresh_boost_avail()

    def on_auto_route_changed(self, enabled: bool) -> None:
        was_blocked = self.auto_route_toggle.blockSignals(True)
        self.auto_route_toggle.setChecked(enabled)
        self.auto_route_toggle.blockSignals(was_blocked)

    def on_channel_boost_changed(
        self, channel: str, enabled: bool, multiplier_pct: int
    ) -> None:
        row = self._boost_rows.get(channel)
        if row is not None:
            row.apply_state(enabled, multiplier_pct)

    def on_volume_boost_state(self, state: dict) -> None:
        for ch, row in self._boost_rows.items():
            data = state.get(ch)
            if isinstance(data, dict):
                row.apply_state(
                    bool(data.get("enabled", False)),
                    int(data.get("multiplier_pct", 100)),
                )

    # ---------------------------------------------------------- input handlers

    def _toggle_media(self) -> None:
        cmd = "remove-media-sink" if self._media_enabled else "add-media-sink"
        self._daemon.send_command(cmd)
        # Disable the button until the daemon confirms the change so quick
        # double-clicks don't queue conflicting commands.
        self.media_btn.setEnabled(False)
        QTimer.singleShot(600, lambda: self.media_btn.setEnabled(True))

    def _toggle_hdmi(self) -> None:
        cmd = "remove-hdmi-sink" if self._hdmi_enabled else "add-hdmi-sink"
        self._daemon.send_command(cmd)
        self.hdmi_btn.setEnabled(False)
        QTimer.singleShot(600, lambda: self.hdmi_btn.setEnabled(True))

    def _toggle_auto_route(self, checked: bool) -> None:
        self._daemon.send_command(
            "set-auto-route-browsers", enabled=bool(checked)
        )

    def _refresh_boost_avail(self) -> None:
        # Game + Chat are always available. Media / HDMI follow the
        # corresponding sink's loaded state — boost on a non-existent
        # sink would just be a no-op pactl call.
        self._boost_rows["game"].set_available(True)
        self._boost_rows["chat"].set_available(True)
        self._boost_rows["media"].set_available(self._media_enabled)
        self._boost_rows["hdmi"].set_available(self._hdmi_enabled)

    # ---------------------------------------------------- profile load helper

    def apply_profile(self, want_media: bool, want_hdmi: bool) -> None:
        """Profile loader: align the daemon's sink state with the profile."""
        if want_media != self._media_enabled:
            self._daemon.send_command(
                "add-media-sink" if want_media else "remove-media-sink"
            )
        if want_hdmi != self._hdmi_enabled:
            self._daemon.send_command(
                "add-hdmi-sink" if want_hdmi else "remove-hdmi-sink"
            )

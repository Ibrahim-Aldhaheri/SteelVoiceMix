"""Sinks tab — Media + HDMI virtual-sink toggles, browser auto-routing,
per-channel digital volume boost."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..settings import save as save_settings
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

        from PySide6.QtCore import QCoreApplication
        self.toggle = ToggleSwitch()
        self.toggle.setToolTip(
            QCoreApplication.translate(
                "SinksTab",
                "Enable digital volume boost for the {channel} channel",
            ).format(channel=channel.title())
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

        # Debounce slider drags so a continuous drag doesn't fire a
        # set-channel-boost (and a pactl subprocess) per pixel — that
        # made the slider feel laggy. 80ms is short enough to feel
        # responsive but long enough to coalesce a fast drag into one
        # tail-end command.
        self._commit_timer = QTimer()
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(80)
        self._commit_timer.timeout.connect(self._commit_slider)

        self.readout = QLabel("100%")
        self.readout.setFixedWidth(48)
        self.readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.layout.addWidget(self.readout)

        # Warning indicator — only visible when slider is past the
        # threshold AND the boost is enabled.
        from PySide6.QtCore import QCoreApplication
        self.warn = QLabel(
            QCoreApplication.translate("SinksTab", "⚠ clipping risk")
        )
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
        from PySide6.QtCore import QCoreApplication
        if not available:
            # Keep stored multiplier_pct as-is so the user's chosen
            # boost survives a sink toggle round-trip.
            self.warn.setVisible(False)
            self.toggle.setToolTip(
                QCoreApplication.translate(
                    "SinksTab",
                    "Add the {channel} sink first to use boost",
                ).format(channel=self.channel.title())
            )
        else:
            self.toggle.setToolTip(
                QCoreApplication.translate(
                    "SinksTab",
                    "Enable digital volume boost for the {channel} channel",
                ).format(channel=self.channel.title())
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
        # Only push when the toggle is on — otherwise we'd spam
        # set-channel-boost with enabled=false. Debounce so a drag
        # coalesces to one final pactl spawn instead of dozens.
        if self.toggle.isChecked():
            self._commit_timer.start()

    def _commit_slider(self) -> None:
        if not self.toggle.isChecked():
            return
        self._daemon.send_command(
            "set-channel-boost",
            channel=self.channel,
            enabled=True,
            multiplier_pct=int(self.slider.value()),
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
        self.media_btn = QPushButton(self.tr("Add Media"))
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
            tooltip=self.tr(
                "Alpha — not yet hardware-verified against a real "
                "HDMI sink (TV / AVR)."
            )
        )
        self.hdmi_btn = QPushButton(self.tr("Add HDMI"))
        self.hdmi_btn.clicked.connect(self._toggle_hdmi)
        hdmi_row.addWidget(hdmi_lbl)
        hdmi_row.addWidget(hdmi_alpha, 0)
        hdmi_row.addWidget(self.hdmi_btn, 1)

        sinks_help = QLabel(
            self.tr(
                "Media and HDMI sinks bypass the ChatMix dial — useful for "
                "music, browsers, or routing audio to a TV/AVR independently "
                "of the headset."
            )
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
            self.tr("Route browsers and media players to SteelMedia automatically"),
            tooltip=self.tr(
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
            self.tr(
                "Digital amplification applied at the sink — use when an app "
                "is too quiet even at the system maximum. Headroom above 150% "
                "can introduce clipping; back off if loud passages distort."
            )
        )
        boost_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        boost_help.setWordWrap(True)
        # Refresh the boost-row text via tr() — using class-level
        # gettext-style strings here so the section labels stay in
        # one .ts context. (The labels passed into _ChannelBoostRow
        # already came from self.tr() above.)
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

        # Auto-redirect card -------------------------------------------
        # Mirrors ASM's "redirect audio on connect/disconnect" feature.
        # When the headset comes online, optionally promote one of the
        # Steel sinks (or a chosen mic source) to system default. When
        # it goes offline, optionally redirect to a fallback device so
        # audio doesn't silently die. Targets are chosen from a live
        # `pactl list sinks/sources short` enumeration so the user
        # picks an actual device that exists on their system.
        layout.addWidget(self._build_redirect_card())

        layout.addStretch(1)

    # ----------------------------------------------------- redirect card

    def _build_redirect_card(self) -> QWidget:
        """Build the connect/disconnect redirect card. Each row is a
        toggle + a target dropdown; the target list is enumerated
        live from pactl so it always reflects what's actually
        plugged in. Saved as `redirect_{sink|source}_on_{connect|
        disconnect}_{enabled,target}` in settings.json."""
        s = self._settings if hasattr(self, "_settings") else None
        # Daemon-tab pattern: Sinks tab gets settings via the GUI
        # settings dict passed to the parent window. We don't have
        # that dependency here today; we fall back to reading via
        # gui.settings.load() each call.
        from ..settings import load as load_settings
        settings = load_settings()
        self._settings_cache = settings  # keep reference

        rows: list = []

        for kind, label in (
            ("sink_on_connect",
             self.tr("On headset connect, set default sink to")),
            ("sink_on_disconnect",
             self.tr("On headset disconnect, set default sink to")),
            ("source_on_connect",
             self.tr("On headset connect, set default source to")),
            ("source_on_disconnect",
             self.tr("On headset disconnect, set default source to")),
        ):
            row = QHBoxLayout()
            toggle = ToggleSwitch()
            toggle.setChecked(
                bool(settings.get(f"redirect_{kind}_enabled", False))
            )
            combo = QComboBox()
            combo.setMinimumWidth(280)
            combo.setEnabled(toggle.isChecked())
            self._populate_redirect_combo(combo, kind, settings)
            toggle.toggled.connect(
                lambda checked, k=kind, c=combo:
                self._on_redirect_toggled(k, checked, c)
            )
            combo.currentIndexChanged.connect(
                lambda _i, k=kind, c=combo:
                self._on_redirect_target_changed(k, c)
            )
            row.addWidget(QLabel(label), 1)
            row.addWidget(combo)
            row.addWidget(toggle, 0, Qt.AlignVCenter)
            rows.append(row)
            # Stash for later refreshes (devices change at runtime)
            setattr(self, f"_redirect_{kind}_combo", combo)
            setattr(self, f"_redirect_{kind}_toggle", toggle)

        help_lbl = QLabel(
            self.tr(
                "When the Arctis is detected (or unplugged), set the "
                "system's default sink/source as configured. Useful "
                "for auto-switching to your speakers when the headset "
                "comes off, or pinning a Steel sink as default while "
                "it's connected. Target lists update automatically "
                "when devices change."
            )
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        refresh_row = QHBoxLayout()
        refresh_btn = QPushButton(self.tr("🔄  Refresh device lists"))
        refresh_btn.setToolTip(
            self.tr("Re-enumerate available sinks and sources via pactl.")
        )
        refresh_btn.clicked.connect(self._refresh_all_redirect_combos)
        refresh_row.addWidget(refresh_btn)
        refresh_row.addStretch(1)

        return card(
            self.tr("Auto-Redirect on Connect / Disconnect"),
            *rows,
            refresh_row,
            help_lbl,
        )

    def _populate_redirect_combo(
        self, combo: QComboBox, kind: str, settings: dict,
    ) -> None:
        """Fill `combo` with available sinks or sources via pactl.
        First entry is always '(none)' = empty target = no redirect.
        Selects the persisted setting if it's still in the list,
        otherwise falls back to '(none)'."""
        import shutil
        import subprocess
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.tr("(none — keep current default)"), "")
        list_kind = "sinks" if "sink" in kind else "sources"
        if shutil.which("pactl"):
            try:
                r = subprocess.run(
                    ["pactl", "list", list_kind, "short"],
                    capture_output=True, text=True, timeout=3,
                )
                for line in r.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        combo.addItem(parts[1], parts[1])
            except Exception:
                pass
        target = settings.get(f"redirect_{kind}_target", "")
        idx = combo.findData(target)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_redirect_toggled(
        self, kind: str, checked: bool, combo: QComboBox,
    ) -> None:
        self._settings_cache[f"redirect_{kind}_enabled"] = bool(checked)
        save_settings(self._settings_cache)
        combo.setEnabled(checked)

    def _on_redirect_target_changed(
        self, kind: str, combo: QComboBox,
    ) -> None:
        target = combo.currentData() or ""
        self._settings_cache[f"redirect_{kind}_target"] = str(target)
        save_settings(self._settings_cache)

    def _refresh_all_redirect_combos(self) -> None:
        """Re-enumerate device lists for all four redirect combos.
        Useful when the user plugs/unplugs a device and wants the
        target dropdown to pick it up without restarting the GUI."""
        from ..settings import load as load_settings
        settings = load_settings()
        self._settings_cache = settings
        for kind in (
            "sink_on_connect", "sink_on_disconnect",
            "source_on_connect", "source_on_disconnect",
        ):
            combo = getattr(self, f"_redirect_{kind}_combo", None)
            if combo is not None:
                self._populate_redirect_combo(combo, kind, settings)

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
        self.media_btn.setText(
            self.tr("Remove Media") if enabled else self.tr("Add Media")
        )
        self.media_btn.setToolTip(
            "Destroy the SteelMedia virtual sink"
            if enabled
            else "Create a SteelMedia virtual sink that bypasses the ChatMix dial"
        )
        self._refresh_boost_avail()

    def on_hdmi_changed(self, enabled: bool) -> None:
        self._hdmi_enabled = enabled
        self.hdmi_btn.setText(
            self.tr("Remove HDMI") if enabled else self.tr("Add HDMI")
        )
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

"""Equalizer tab — 10-band parametric EQ with per-channel state.

This tab is going to grow significantly: a searchable preset library,
Custom-N preset auto-creation when sliders are modified, up to 5
favourites per channel, and Media + HDMI added to the channel selector.
Each of those features is a self-contained block of UI + handlers, so
the file is structured to make adding them straightforward — the slider
grid stays put, and new sections layer below it.
"""

from __future__ import annotations

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..searchable_select import SearchableSelect

from ..eq_presets import (
    delete_user_preset,
    find_preset,
    is_user_preset,
    list_presets,
    next_custom_name,
    rename_user_preset,
    save_user_preset,
)
from ..eq_test_audio import CHANNEL_TO_SINK, TEST_AUDIO_CATALOGUE
from ..settings import (
    MAX_FAVOURITES_PER_CHANNEL,
    add_favourite,
    get_favourites,
    is_favourite,
    remove_favourite,
    rename_favourite,
    save as save_settings,
)
from ..widgets import NoWheelComboBox, NoWheelSlider, card, labelled_toggle


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
    def __init__(
        self,
        daemon_client,
        settings: dict,
        game_eq_manager=None,
        voice_test=None,
        parent=None,
    ):
        super().__init__(parent)
        self._daemon = daemon_client
        # Settings dict is shared with the rest of the GUI so favourite
        # changes persist alongside overlay/profile prefs.
        self._settings = settings
        # GameProfileManager — used by the Auto Game-EQ card at the
        # bottom of the page. We hold it so the binding-add dropdown
        # can read latest_seen() and the "currently detected" line
        # can subscribe to detected_changed.
        self._game_eq_manager = game_eq_manager
        # Shared VoiceTestService — drives the Hear Yourself button
        # that replaces the Test Audio card when the user is on the
        # Mic channel. Owned by MixerGUI so the Microphone tab and
        # this tab stay in sync.
        self._voice_test = voice_test
        self._eq_enabled = False
        # Set when Auto Game-EQ has applied a preset to the Game
        # channel. While truthy, the Game-channel sliders + preset
        # combo are locked — any user change would just get clobbered
        # by the next watcher tick. Cleared when the manager emits
        # applied_changed("") (game closed → snapshot restored).
        self._auto_applied_preset: str | None = None

        # Per-channel band data. Each channel carries its own
        # {freq, q, gain, type, enabled} list. Defaults match the Rust
        # daemon's default_channel_bands() so the GUI shows a sane shape
        # before the first status snapshot.
        self._bands_by_channel: dict[str, list[dict]] = {
            "game": _default_channel_bands(),
            "chat": _default_channel_bands(),
            "media": _default_channel_bands(),
            "hdmi": _default_channel_bands(),
            "mic": _default_channel_bands(),
        }
        self._current_channel: str = "game"
        # Media and HDMI channels are only present in the channel combo
        # while the corresponding null-sink is loaded — the daemon
        # broadcasts media-sink-changed / hdmi-sink-changed and the EQ
        # tab listens, refreshing the combo when those flip.
        self._media_sink_enabled: bool = False
        self._hdmi_sink_enabled: bool = False

        # Track which preset is "active" per channel — the one currently
        # selected in the preset combo, or empty if the user has been
        # tweaking sliders without loading anything. Drives the
        # auto-fork-to-Custom-N behaviour: if the user starts editing
        # while a built-in preset is active, we fork to a fresh Custom N
        # so the built-in stays clean.
        self._active_preset_by_channel: dict[str, str] = {
            "game": "",
            "chat": "",
            "media": "",
            "hdmi": "",
            "mic": "",
        }

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

        # Test-audio playback runs as a managed QProcess so the Stop
        # button can kill it cleanly mid-clip. Lazily created on first
        # Play; we never spawn until the user asks.
        self._test_process: QProcess | None = None

        self._build_ui()
        self._render_sliders_for_channel(self._current_channel)

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Enable + channel selector card -------------------------------
        enable_row, self.eq_toggle = labelled_toggle(
            f"Enable {NUM_EQ_BANDS}-band parametric EQ",
            tooltip=(
                "Inserts a PipeWire filter chain between every loaded "
                "virtual sink (Game / Chat / Media / HDMI) and the "
                "downstream target. The user-facing sinks stay put across "
                "toggles, so Discord and other apps don't lose their "
                "connection."
            ),
        )
        self.eq_toggle.toggled.connect(self._toggle_enabled)

        # Per-channel selector: tune [Game] and [Chat] independently.
        # Sliders display the selected channel's bands; switching the
        # combo loads that channel's stored values. Emoji icons match
        # the Home-tab convention (🎮 / 💬).
        ch_row = QHBoxLayout()
        ch_row.addWidget(QLabel("Channel"))
        self.channel_combo = NoWheelComboBox()
        self.channel_combo.setMinimumWidth(140)
        self.channel_combo.currentTextChanged.connect(self._on_channel_changed)
        ch_row.addWidget(self.channel_combo, 1)
        # Populate channel combo for the initial sink state (Game + Chat
        # always; Media/HDMI added if their sinks are enabled). Sink
        # toggles fire on_media_sink_changed / on_hdmi_sink_changed and
        # we re-run this populate to keep the combo in sync.
        self._refresh_channel_combo()

        # Banner shown when Auto Game-EQ has overridden the Game
        # channel. While this is visible, the Game-channel sliders +
        # preset combo are read-only (any change would be clobbered
        # by the next watcher tick anyway). The banner lives inside
        # the Equalizer card so it stays put as the user scrolls
        # through preset / bands / favourites cards below.
        self.auto_lock_banner = QLabel("")
        self.auto_lock_banner.setStyleSheet(
            "background: #FF9800; color: white; "
            "padding: 6px 10px; border-radius: 6px; "
            "font-size: 11px; font-weight: bold;"
        )
        self.auto_lock_banner.setWordWrap(True)
        self.auto_lock_banner.hide()

        layout.addWidget(card(
            "Equalizer", enable_row, ch_row, self.auto_lock_banner,
        ))

        # Preset row: searchable dropdown filtered by the current channel,
        # plus Load / Save / Delete actions. The combo is editable so the
        # user can type to filter (the QCompleter does substring match
        # against built-in + user preset names).
        preset_picker_row = QHBoxLayout()
        # Use the custom SearchableSelect — QComboBox-with-completer
        # has a pile of issues we don't want here: existing selection
        # blocks search until manually cleared, scroll-wheel cycles
        # through entries (catastrophic with auto-apply), the popup
        # is separate from the dropdown, and long ASM names get
        # truncated. SearchableSelect bakes in: search field at the
        # top of the popup, instant substring filter, keyboard nav,
        # wheel events ignored.
        self.preset_combo = SearchableSelect()
        self.preset_combo.setMinimumWidth(220)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_index_changed)
        # `activated` fires only when the user actually picks an item
        # from the popup — programmatic setCurrentIndex doesn't
        # trigger it, so internal repopulates don't kick off
        # spurious loads.
        self.preset_combo.activated.connect(self._on_preset_activated)
        preset_picker_row.addWidget(self.preset_combo, 1)
        # Star toggle. Outline = not favourited, filled = favourited.
        # Limited to MAX_FAVOURITES_PER_CHANNEL on each channel; trying
        # to add a sixth pops a message asking the user to clear one.
        self.preset_fav_btn = QPushButton("☆")
        self.preset_fav_btn.setFixedWidth(36)
        self.preset_fav_btn.setToolTip(
            f"Favourite this preset (up to {MAX_FAVOURITES_PER_CHANNEL} per channel)"
        )
        self.preset_fav_btn.clicked.connect(self._on_preset_favourite_toggled)
        preset_picker_row.addWidget(self.preset_fav_btn)

        # Action row — Load is gone (selecting from the combo auto-
        # applies). The remaining buttons handle the actions that DO
        # require explicit confirmation: saving the current state,
        # renaming a user preset, deleting a user preset.
        preset_btn_row = QHBoxLayout()
        self.preset_save_btn = QPushButton("Save…")
        self.preset_save_btn.clicked.connect(self._on_preset_save)
        self.preset_rename_btn = QPushButton("Rename…")
        self.preset_rename_btn.clicked.connect(self._on_preset_rename)
        self.preset_rename_btn.setEnabled(False)
        self.preset_delete_btn = QPushButton("Delete")
        self.preset_delete_btn.clicked.connect(self._on_preset_delete)
        self.preset_delete_btn.setEnabled(False)
        preset_btn_row.addWidget(self.preset_save_btn)
        preset_btn_row.addWidget(self.preset_rename_btn)
        preset_btn_row.addWidget(self.preset_delete_btn)
        preset_btn_row.addStretch(1)

        layout.addWidget(card("Preset", preset_picker_row, preset_btn_row))
        # Populate the combo for the initial channel before any signals fire.
        self._refresh_preset_combo()

        # Favourites quick-bar — up to MAX_FAVOURITES_PER_CHANNEL
        # buttons, one per favourited preset. Click any to load
        # immediately. Built fresh by `_refresh_favourites_card` on
        # channel switch, favourite toggle, rename, delete.
        self.favourites_card_layout = QVBoxLayout()
        self.favourites_card_layout.setSpacing(6)
        self.favourites_buttons_row = QHBoxLayout()
        self.favourites_buttons_row.setSpacing(6)
        self.favourites_empty_hint = QLabel(
            "No favourites yet — tap ★ next to a preset to pin it here."
        )
        self.favourites_empty_hint.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        self.favourites_card_layout.addLayout(self.favourites_buttons_row)
        self.favourites_card_layout.addWidget(self.favourites_empty_hint)
        layout.addWidget(card("Favourites", self.favourites_card_layout))
        self._refresh_favourites_card()

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

            # NoWheelSlider — stock QSlider's wheel-scroll changed band
            # gain by accident every time the user scrolled the EQ tab,
            # forking the active preset to a fresh Custom-N. Subclass
            # ignores the wheel event entirely; arrow keys + drag still
            # work for keyboard / pointer users.
            slider = NoWheelSlider(Qt.Vertical)
            # Slider unit = 0.1 dB. Range: -120 to 120 → -12.0 to +12.0 dB.
            slider.setRange(-120, 120)
            slider.setValue(0)
            slider.setTickPosition(QSlider.TicksRight)
            slider.setTickInterval(60)
            # Use a fixed height (not minimum) so the slider column never
            # collapses when the parent layout is under pressure — the
            # earlier minimum-height was getting overridden when the EQ
            # tab couldn't fit at the old window size.
            slider.setFixedHeight(260)
            slider.setFixedWidth(34)
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

        eq_help = QLabel(
            "Drag a slider to boost or cut a frequency band by up to "
            "±12 dB. Each release respawns the filter chain with the new "
            "gains (~100 ms audio glitch per change)."
        )
        eq_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        eq_help.setWordWrap(True)
        layout.addWidget(card("Bands", bands_row, eq_help))

        # Test-audio card ----------------------------------------------
        test_row = QHBoxLayout()
        self.test_audio_combo = NoWheelComboBox()
        for label, _factory in TEST_AUDIO_CATALOGUE:
            self.test_audio_combo.addItem(label)
        self.test_audio_combo.setMinimumWidth(200)
        test_row.addWidget(self.test_audio_combo, 1)
        self.test_play_btn = QPushButton("▶ Play")
        self.test_play_btn.clicked.connect(self._on_test_play)
        self.test_stop_btn = QPushButton("⏹")
        self.test_stop_btn.setFixedWidth(36)
        self.test_stop_btn.clicked.connect(self._on_test_stop)
        self.test_stop_btn.setEnabled(False)
        test_row.addWidget(self.test_play_btn)
        test_row.addWidget(self.test_stop_btn)
        test_warn = QLabel(
            "⚠ Drop system volume to ~10–20% BEFORE pressing Play. "
            "These clips are intentionally whisper-quiet to protect "
            "your hearing, but if your headset gain or system volume "
            "is high they can still be uncomfortable. Hit Stop "
            "immediately if anything feels too loud."
        )
        test_warn.setWordWrap(True)
        test_warn.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #FF9800;"
        )
        test_help = QLabel(
            "Reference signals for ear-checking the EQ — pink noise "
            "is the recommended starting point. Each clip ramps in over "
            "200 ms so even the onset is gentle."
        )
        test_help.setWordWrap(True)
        test_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        # Test Audio card — for ear-checking output-channel EQ. We
        # cache the widget so _on_channel_changed can hide it when
        # the user switches to Mic (where Test Audio doesn't make
        # sense — a noise generator wouldn't go through the mic).
        self.test_audio_card = card("Test Audio", test_warn, test_row, test_help)
        layout.addWidget(self.test_audio_card)

        # Hear Yourself card — only visible on the Mic channel.
        # Shares the VoiceTestService with the Microphone tab so
        # toggling either button toggles both.
        self.voice_test_card = self._build_voice_test_card()
        layout.addWidget(self.voice_test_card)

        # Auto Game-EQ card — lives on the EQ page (rather than
        # buried in Settings) so users find it where they manage
        # their EQ.
        layout.addWidget(self._build_auto_game_card())

        # Apply initial channel-dependent visibility now that all
        # the channel-conditional cards are constructed.
        self._update_channel_specific_cards(self._current_channel)

        layout.addStretch(1)

    # ---------------------------------------------- Hear Yourself card

    def _build_voice_test_card(self) -> QWidget:
        btn_row = QHBoxLayout()
        self.voice_test_btn = QPushButton("🎧  Hear yourself (test mic)")
        self.voice_test_btn.setCheckable(True)
        self.voice_test_btn.setMaximumWidth(280)
        self.voice_test_btn.toggled.connect(self._on_voice_test_toggled)
        btn_row.addWidget(self.voice_test_btn)
        btn_row.addStretch(1)

        help_lbl = QLabel(
            "Loops the processed SteelMic back through your headset "
            "so you can A/B the EQ live. Same control as the "
            "Microphone tab — toggling either button drives both."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        if self._voice_test is not None:
            self._voice_test.state_changed.connect(
                self._on_voice_test_state_changed
            )

        return card("Listen", btn_row, help_lbl)

    def _on_voice_test_toggled(self, checked: bool) -> None:
        if self._voice_test is None:
            return
        if checked:
            ok, err = self._voice_test.start()
            if not ok:
                QMessageBox.warning(self, "Voice-test failed", err)
                self.voice_test_btn.setChecked(False)
        else:
            self._voice_test.stop()

    def _on_voice_test_state_changed(self, running: bool) -> None:
        was_blocked = self.voice_test_btn.blockSignals(True)
        try:
            self.voice_test_btn.setChecked(running)
        finally:
            self.voice_test_btn.blockSignals(was_blocked)
        self.voice_test_btn.setText(
            "🛑  Stop voice test" if running else "🎧  Hear yourself (test mic)"
        )

    def _update_channel_specific_cards(self, channel: str) -> None:
        """Test Audio is for output-channel EQ tuning — hides on
        Mic. Hear Yourself is only meaningful on Mic — hides
        elsewhere."""
        is_mic = (channel == "mic")
        if hasattr(self, "test_audio_card"):
            self.test_audio_card.setVisible(not is_mic)
        if hasattr(self, "voice_test_card"):
            self.voice_test_card.setVisible(is_mic)

    # --------------------------------------------------- auto game-EQ card

    def _build_auto_game_card(self) -> QWidget:
        """Toggle (with ALPHA badge), live 'Currently detected' status
        line, manual bindings table, and Add/Remove buttons. Talks
        directly to the GameProfileManager for runtime state and to
        settings.json for persistence."""
        auto_row, self.auto_game_toggle = labelled_toggle(
            "Auto-switch EQ when a known game launches",
            badge="ALPHA",
        )
        self.auto_game_toggle.setChecked(
            bool(self._settings.get("auto_game_eq_enabled", False))
        )
        self.auto_game_toggle.toggled.connect(self._toggle_auto_game)

        self.detected_label = QLabel("Currently detected: none")
        self.detected_label.setWordWrap(True)
        self.detected_label.setStyleSheet(
            "font-size: 10px; padding: 4px 0; "
            "color: palette(placeholder-text);"
        )
        if self._game_eq_manager is not None:
            self._game_eq_manager.detected_changed.connect(
                self._on_detected_changed
            )

        self.bindings_table = QTableWidget(0, 3)
        self.bindings_table.setHorizontalHeaderLabels(
            ["#", "Game name", "EQ preset"]
        )
        header = self.bindings_table.horizontalHeader()
        # Priority column stays narrow; the other two share the rest.
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.bindings_table.verticalHeader().setVisible(False)
        self.bindings_table.setMinimumHeight(140)
        # Drag-drop reorder: row-level selection, internal-move so
        # users can drag a binding up to raise its priority. The
        # rowsMoved signal on the model fires after each successful
        # drop; we persist the new order at that point.
        self.bindings_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.bindings_table.setDragEnabled(True)
        self.bindings_table.setAcceptDrops(True)
        self.bindings_table.setDropIndicatorShown(True)
        self.bindings_table.setDragDropMode(QAbstractItemView.InternalMove)
        self.bindings_table.setDragDropOverwriteMode(False)
        self.bindings_table.model().rowsMoved.connect(self._on_rows_moved)
        self._refresh_bindings_table()

        bindings_btns = QHBoxLayout()
        add_btn = QPushButton("Add binding…")
        add_btn.clicked.connect(self._add_binding)
        del_btn = QPushButton("Remove selected")
        del_btn.clicked.connect(self._remove_binding)
        bindings_btns.addWidget(add_btn)
        bindings_btns.addWidget(del_btn)
        bindings_btns.addStretch(1)

        help_lbl = QLabel(
            "When on, the app watches PipeWire for active audio "
            "clients and applies a matching ASM preset to the Game "
            "channel. The table below overrides the auto-match — bind "
            "the same preset to several games to share one tuning. "
            "Closing the game restores whatever EQ you had before. "
            "Note: the EQ only takes effect when the game's audio is "
            "routed to SteelGame; if the status above says 'not on "
            "SteelGame', move the stream via your system audio "
            "settings or pavucontrol."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        return card(
            "Auto Game-EQ",
            auto_row,
            self.detected_label,
            self.bindings_table,
            bindings_btns,
            help_lbl,
        )

    def _toggle_auto_game(self, checked: bool) -> None:
        self._settings["auto_game_eq_enabled"] = checked
        save_settings(self._settings)
        # Reconcile immediately rather than waiting up to 2 s for
        # the next watcher tick. If a game is already running and
        # the user just turned the feature on, this is what triggers
        # the preset apply. If they turned it off mid-game, this
        # restores their pre-game EQ.
        if self._game_eq_manager is not None:
            self._game_eq_manager.reconcile()

    def _on_auto_bands_load(self, bands: list) -> None:
        """GameProfileManager handed us the bands to display
        immediately — either the auto-loaded preset on enter/switch
        or the saved snapshot on exit. We don't wait for the
        daemon's eq-bands-changed broadcast to roundtrip; the
        broadcast still arrives and confirms the same data, but
        the user's eyes get instant feedback. Mirrors what
        _apply_preset does for manual loads."""
        if not isinstance(bands, list) or not bands:
            return
        self._bands_by_channel["game"] = [dict(b) for b in bands]
        if self._current_channel == "game":
            self._render_sliders_for_channel("game")

    def _on_auto_applied(self, preset_name: str) -> None:
        """GameProfileManager signal: Auto Game-EQ has applied a
        preset (preset_name truthy) or restored the user snapshot
        (empty string). Update the status banner, sync the preset
        combo to the auto-loaded preset (or the user's pre-game
        preset on restore), and force a slider re-render."""
        was_engaged = self._auto_applied_preset is not None
        self._auto_applied_preset = preset_name or None
        if preset_name:
            # Engaging — remember the user's pre-game preset name
            # so we can put the combo back when the game closes.
            if not was_engaged:
                self._pre_auto_preset_game = self._active_preset_by_channel.get(
                    "game", ""
                )
            self.auto_lock_banner.setText(
                f"🎮 Auto Game-EQ active — preset: {preset_name}. "
                "Sliders are still editable; your tweaks stay until "
                "the next game change or until you close the game "
                "(at which point your pre-game EQ is restored)."
            )
            self.auto_lock_banner.show()
            self._active_preset_by_channel["game"] = preset_name
            if self._current_channel == "game":
                idx = self._index_for_preset_name(preset_name)
                if idx >= 0:
                    was_blocked = self.preset_combo.blockSignals(True)
                    try:
                        self.preset_combo.setCurrentIndex(idx)
                    finally:
                        self.preset_combo.blockSignals(was_blocked)
                self._render_sliders_for_channel("game")
        else:
            # Disengaging — restore both the banner and the preset
            # combo to whatever the user had selected pre-game.
            self.auto_lock_banner.hide()
            previous = getattr(self, "_pre_auto_preset_game", "") or ""
            self._active_preset_by_channel["game"] = previous
            if self._current_channel == "game":
                idx = self._index_for_preset_name(previous) if previous else -1
                was_blocked = self.preset_combo.blockSignals(True)
                try:
                    if idx >= 0:
                        self.preset_combo.setCurrentIndex(idx)
                    else:
                        # No prior preset — leave combo at first item
                        # (or wherever it lands); the snapshot bands
                        # supplied via _on_auto_bands_load already
                        # painted the sliders.
                        pass
                finally:
                    self.preset_combo.blockSignals(was_blocked)
                self._render_sliders_for_channel("game")
            self._pre_auto_preset_game = ""

    def _on_detected_changed(
        self, name: str, preset, on_steel_game: bool
    ) -> None:
        if not name:
            self.detected_label.setText("Currently detected: none")
            self.detected_label.setStyleSheet(
                "font-size: 10px; padding: 4px 0; "
                "color: palette(placeholder-text);"
            )
            return
        preset_part = f" → {preset}" if preset else " (no preset match)"
        if on_steel_game:
            # We confirmed the sink-input is on SteelGame — EQ will
            # be audible.
            self.detected_label.setText(
                f"Currently detected: {name}{preset_part}"
            )
            self.detected_label.setStyleSheet(
                "font-size: 10px; padding: 4px 0; color: #4CAF50;"
            )
        else:
            # The EQ command IS sent regardless — the manager doesn't
            # gate on this flag — but if PipeWire didn't report a
            # SteelGame routing for this stream, the EQ change might
            # not be audible. Two cases: (a) the user really has the
            # game on a different sink, (b) PipeWire's metadata-based
            # routing hides the SteelGame target from pactl. Reword
            # so the user knows it's a "maybe" not a "definitely
            # won't work".
            self.detected_label.setText(
                f"Currently detected: {name}{preset_part} "
                "— EQ applied; if you don't hear a change, route the "
                "game to SteelGame in your system audio settings"
            )
            self.detected_label.setStyleSheet(
                "font-size: 10px; padding: 4px 0; color: #FF9800;"
            )

    def _bindings_list(self) -> list[dict]:
        """Read the bindings list from settings, normalising the
        legacy dict shape if a stale settings.json predates the
        migration. Returns a NEW list — caller may mutate freely."""
        raw = self._settings.get("game_eq_bindings") or []
        if isinstance(raw, dict):
            return [{"game": k, "preset": v} for k, v in sorted(raw.items())]
        out: list[dict] = []
        for entry in raw:
            if isinstance(entry, dict) and entry.get("game") and entry.get("preset"):
                out.append({"game": entry["game"], "preset": entry["preset"]})
        return out

    def _refresh_bindings_table(self) -> None:
        """Repopulate the table from the ordered bindings list. The
        priority column shows 1-based index so users see at a glance
        which binding wins on conflict."""
        bindings = self._bindings_list()
        self.bindings_table.setRowCount(0)
        for entry in bindings:
            row = self.bindings_table.rowCount()
            self.bindings_table.insertRow(row)
            prio_item = QTableWidgetItem(str(row + 1))
            prio_item.setTextAlignment(Qt.AlignCenter)
            prio_item.setFlags(prio_item.flags() & ~Qt.ItemIsEditable)
            self.bindings_table.setItem(row, 0, prio_item)
            game_item = QTableWidgetItem(entry["game"])
            game_item.setFlags(game_item.flags() & ~Qt.ItemIsEditable)
            preset_item = QTableWidgetItem(entry["preset"])
            preset_item.setFlags(preset_item.flags() & ~Qt.ItemIsEditable)
            self.bindings_table.setItem(row, 1, game_item)
            self.bindings_table.setItem(row, 2, preset_item)

    def _on_rows_moved(self, _parent, _start, _end, _dest, _row) -> None:
        """User drag-drop reordered the rows. Read the new order back
        from the table widget and persist as the canonical list. We
        ignore the indices Qt hands us because re-reading the table
        is simpler and authoritative."""
        new_order: list[dict] = []
        for r in range(self.bindings_table.rowCount()):
            game_item = self.bindings_table.item(r, 1)
            preset_item = self.bindings_table.item(r, 2)
            if game_item and preset_item:
                new_order.append({
                    "game": game_item.text(),
                    "preset": preset_item.text(),
                })
        self._settings["game_eq_bindings"] = new_order
        save_settings(self._settings)
        # Re-render to refresh the priority column numbers.
        self._refresh_bindings_table()

    def _collect_binding_candidates(self) -> list[str]:
        """Build a deduped, ordered list of binding suggestions.
        Audio clients first (proven to match PipeWire's
        application.name), then windowed apps via wmctrl. Free-text
        input is still allowed — the combo is editable."""
        seen: set[str] = set()
        out: list[str] = []
        # Active audio clients from the watcher — reuse what's
        # already in cache rather than re-running pactl.
        if self._game_eq_manager is not None:
            for name in sorted(self._game_eq_manager.latest_seen().keys()):
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        # Windowed applications via wmctrl. Optional — if wmctrl
        # isn't installed we just skip this source (no error,
        # the audio-clients list is enough for most users).
        import shutil
        import subprocess
        if shutil.which("wmctrl"):
            try:
                r = subprocess.run(
                    ["wmctrl", "-l"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        # wmctrl -l format: "<id> <desktop> <host> <title>"
                        parts = line.split(None, 3)
                        if len(parts) < 4:
                            continue
                        title = parts[3].strip()
                        # Filter out empty titles, our own GUI, and
                        # very long titles (likely web-page tabs).
                        if (
                            title
                            and title not in seen
                            and "SteelVoiceMix" not in title
                            and len(title) <= 80
                        ):
                            seen.add(title)
                            out.append(title)
            except Exception:
                pass
        return out

    def _add_binding(self) -> None:
        # Combine three sources so the user has one dropdown to pick
        # from regardless of whether the target app is currently
        # making audio:
        #   1. Audio clients PipeWire is reporting right now (best —
        #      proven names that match what the daemon will see).
        #   2. Open windowed applications via wmctrl, if available
        #      (catches Steam Big Picture, launchers, native games
        #      that opened their window before producing audio).
        #   3. Free-text input — the combo is editable so users can
        #      pre-bind anything they expect to run later, even
        #      something not currently visible at all.
        active = self._collect_binding_candidates()
        prompt = (
            "Pick a running app or game from the list — or type a "
            "custom name. Bindings match against PipeWire's "
            "application.name when audio starts."
        )
        if not active:
            prompt = (
                "No running apps detected. Type a custom name below — "
                "it'll match when the app produces audio later."
            )
        name, ok = QInputDialog.getItem(
            self,
            "Bind app to preset",
            prompt,
            active,
            0,
            True,  # editable: user can type anything
        )
        if not ok or not name or not name.strip():
            return
        name = name.strip()
        preset_names = [p["name"] for p in list_presets("game")]
        if not preset_names:
            QMessageBox.warning(
                self,
                "No presets available",
                "There are no Game-channel presets to bind. Save one "
                "from this tab first.",
            )
            return
        preset, ok = QInputDialog.getItem(
            self,
            f"Preset for '{name}'",
            "EQ preset:",
            preset_names,
            0,
            False,
        )
        if not ok or not preset:
            return
        # Append to the ordered list — new entries land at the bottom
        # (lowest priority); the user drags up if they want it to win.
        # Duplicates are allowed: same game + different preset is the
        # whole point of "first match wins" priority ordering.
        bindings = self._bindings_list()
        bindings.append({"game": name, "preset": preset})
        self._settings["game_eq_bindings"] = bindings
        save_settings(self._settings)
        self._refresh_bindings_table()

    def _remove_binding(self) -> None:
        rows = sorted(
            {idx.row() for idx in self.bindings_table.selectedIndexes()},
            reverse=True,
        )
        if not rows:
            return
        bindings = self._bindings_list()
        for r in rows:
            if 0 <= r < len(bindings):
                bindings.pop(r)
        self._settings["game_eq_bindings"] = bindings
        save_settings(self._settings)
        self._refresh_bindings_table()

    # ---------------------------------------------------- daemon-event hooks

    def on_enabled_changed(self, enabled: bool) -> None:
        self._eq_enabled = enabled
        was_blocked = self.eq_toggle.blockSignals(True)
        self.eq_toggle.setChecked(enabled)
        self.eq_toggle.blockSignals(was_blocked)

    def on_bands_changed(self, channel: str, bands: list) -> None:
        """Daemon broadcast: bands for `channel` changed (perhaps because
        we just sent the change, perhaps from another client or a preset
        load). Update the local cache; if it's the channel currently on
        screen, refresh sliders + labels too."""
        if channel not in self._bands_by_channel:
            return
        self._bands_by_channel[channel] = list(bands)
        # Reconcile the active-preset cache so the combo doesn't keep
        # showing the previous selection when bands actually changed
        # (e.g. auto-game-EQ load, profile load, or daemon-side reset).
        self._reconcile_active_preset(channel)
        if channel == self._current_channel:
            self._render_sliders_for_channel(channel)
            # Sync the combo too — _refresh_preset_combo reads the
            # active-preset cache we just updated.
            self._refresh_preset_combo()

    def on_full_state(self, state: dict) -> None:
        """Initial Status snapshot delivered every channel's band data
        at once (Game / Chat / Media / HDMI / Mic). Cache them all and
        reconcile the active-preset name from the loaded bands so the
        preset combo doesn't show 'Flat' while the sliders sit on a
        non-flat preset (the bug we hit on first restart after upgrade
        from a stable that didn't persist active_preset)."""
        for ch in ("game", "chat", "media", "hdmi", "mic"):
            if ch in state:
                self._bands_by_channel[ch] = list(state[ch])
                self._reconcile_active_preset(ch)
        self._render_sliders_for_channel(self._current_channel)
        self._refresh_preset_combo()

    def _reconcile_active_preset(self, channel: str) -> None:
        """Look at the currently-loaded bands for `channel` and set
        `_active_preset_by_channel[channel]` to the name of whichever
        bundled / built-in / user preset matches them, or '' if no
        preset matches.

        We do this because the daemon persists the BANDS (which is
        the source of truth for the audio chain) but NOT which
        preset name the bands came from — that's GUI-side metadata.
        Without this reconcile, after a daemon restart the user sees
        their non-flat bands but the combo still says 'Flat'."""
        bands = self._bands_by_channel.get(channel)
        if not bands:
            return
        matched = self._find_preset_name_for_bands(channel, bands)
        # Empty string is fine — means 'bands don't match any known
        # preset' which is the legitimate state when the user has
        # been hand-editing.
        self._active_preset_by_channel[channel] = matched

    def _find_preset_name_for_bands(
        self, channel: str, bands: list[dict],
    ) -> str:
        """Find the preset whose bands match `bands` exactly (within
        float tolerance). Returns the preset name, or '' if none
        matches. Iterates list_presets(channel) which already merges
        built-in + ASM-bundled + user presets."""
        for preset in list_presets(channel):
            preset_bands = preset.get("bands") or []
            if self._bands_equal(bands, preset_bands):
                return str(preset.get("name", ""))
        return ""

    @staticmethod
    def _bands_equal(a: list[dict], b: list[dict]) -> bool:
        """Field-by-field band comparison with float tolerance for
        freq / q / gain. Type + enabled compare exactly."""
        if len(a) != len(b):
            return False
        for ba, bb in zip(a, b):
            if not isinstance(ba, dict) or not isinstance(bb, dict):
                return False
            if abs(float(ba.get("freq", 0)) - float(bb.get("freq", 0))) > 0.5:
                return False
            if abs(float(ba.get("q", 0)) - float(bb.get("q", 0))) > 0.001:
                return False
            if abs(float(ba.get("gain", 0)) - float(bb.get("gain", 0))) > 0.05:
                return False
            if str(ba.get("type", "")) != str(bb.get("type", "")):
                return False
            if bool(ba.get("enabled", True)) != bool(bb.get("enabled", True)):
                return False
        return True

    def on_media_sink_changed(self, enabled: bool) -> None:
        """Sink toggled in another tab → make Media available (or not)
        in the channel combo here."""
        if self._media_sink_enabled == enabled:
            return
        self._media_sink_enabled = enabled
        self._refresh_channel_combo()

    def on_hdmi_sink_changed(self, enabled: bool) -> None:
        if self._hdmi_sink_enabled == enabled:
            return
        self._hdmi_sink_enabled = enabled
        self._refresh_channel_combo()

    def _refresh_channel_combo(self) -> None:
        """Rebuild the channel-combo entries based on which sinks are
        currently loaded. Game + Chat always show; Media + HDMI show
        only when their sinks are enabled. Mic is always present —
        the mic chain runs whenever any mic feature OR EQ band is
        active, and the daemon spawns it on demand. UserData carries
        the bare channel key ('game', 'chat', 'media', 'hdmi', 'mic')
        so internal lookups don't have to parse the emoji prefix."""
        labels = [("game", "🎮 Game"), ("chat", "💬 Chat")]
        if self._media_sink_enabled:
            labels.append(("media", "🎵 Media"))
        if self._hdmi_sink_enabled:
            labels.append(("hdmi", "📺 HDMI"))
        labels.append(("mic", "🎙 Microphone"))

        was_blocked = self.channel_combo.blockSignals(True)
        try:
            self.channel_combo.clear()
            for key, label in labels:
                self.channel_combo.addItem(label, userData=key)
            # Restore the current channel if it's still available;
            # otherwise fall back to Game (always present) so the EQ tab
            # never lands on a non-existent channel.
            keys = [k for k, _ in labels]
            if self._current_channel not in keys:
                self._current_channel = "game"
            for i in range(self.channel_combo.count()):
                if self.channel_combo.itemData(i) == self._current_channel:
                    self.channel_combo.setCurrentIndex(i)
                    break
        finally:
            self.channel_combo.blockSignals(was_blocked)

    # ---------------------------------------------------------- input handlers

    def _toggle_enabled(self, checked: bool) -> None:
        self._daemon.send_command("set-eq-enabled", enabled=bool(checked))

    def _on_slider_changed(self, band: int, value_tenths: int, label: QLabel) -> None:
        """User moved a slider. Update the live value label, store the
        new value in the current channel's bands array, and queue a
        debounced daemon commit. If the user is editing on top of a
        built-in or no preset, fork to a fresh Custom N — once — before
        any commits go out, so the original preset stays untouched."""
        gain_db = value_tenths / 10.0
        sign = "+" if gain_db > 0 else ""
        label.setText(f"{sign}{gain_db:.1f}")
        bands = self._bands_by_channel[self._current_channel]
        if 1 <= band <= len(bands):
            bands[band - 1]["gain"] = gain_db
        self._maybe_fork_to_custom()
        self._pending_band_value[band] = value_tenths
        self._commit_timer.start()

    def _on_slider_released(self, band: int, slider: QSlider) -> None:
        """Slider released — commit *now* without waiting for the debounce."""
        self._pending_band_value[band] = slider.value()
        self._commit_timer.stop()
        self._commit_pending_changes()

    def _commit_pending_changes(self) -> None:
        """Flush queued band-gain changes to the daemon for the currently
        selected channel. If we've forked to a Custom preset, persist the
        updated bands so the saved file stays in sync with the sliders."""
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
        # Auto-save: if a user preset is currently active, write the
        # latest band state to its file so the dropdown stays a faithful
        # snapshot of what's playing. Built-ins are read-only, so skip.
        active = self._active_preset_by_channel.get(channel, "")
        if active and is_user_preset(active, channel):
            try:
                save_user_preset(
                    active, channel, self._bands_by_channel[channel]
                )
            except ValueError:
                # Sanitisation rejected the name, somehow. Don't surface
                # a dialog mid-drag — log and move on.
                pass

    def _maybe_fork_to_custom(self) -> None:
        """If the user just started editing while a built-in (or nothing)
        is selected, fork the current bands into a new Custom N user
        preset and select it. Subsequent edits then update Custom N in
        place via the auto-save in `_commit_pending_changes`. Idempotent:
        if a Custom N is already active, no-op."""
        channel = self._current_channel
        active = self._active_preset_by_channel.get(channel, "")
        if active and is_user_preset(active, channel):
            return
        new_name = next_custom_name(channel)
        try:
            save_user_preset(
                new_name, channel, self._bands_by_channel[channel]
            )
        except ValueError:
            return
        self._active_preset_by_channel[channel] = new_name
        # _refresh_preset_combo handles the star-prefix + favourites
        # sorting and re-selects the active preset for us.
        self._refresh_preset_combo()

    def _on_channel_changed(self, _text: str) -> None:
        """Combo box changed — read the selected row's userData (the
        bare channel key like 'game' / 'media') and load that channel's
        stored bands into the sliders. The visible label has an emoji
        prefix; we ignore it and trust the userData."""
        key = self.channel_combo.currentData()
        if not isinstance(key, str) or key not in self._bands_by_channel:
            return
        self._current_channel = key
        # Cancel any pending commit from the previous channel.
        self._commit_timer.stop()
        self._pending_band_value.clear()
        # Test audio is bound to the previous channel's sink — kill it
        # so the user doesn't keep hearing the old chain after switching.
        self._on_test_stop()
        self._render_sliders_for_channel(key)
        # Preset list is channel-scoped — repopulate the dropdown so the
        # user only sees presets for the channel they're looking at.
        self._refresh_preset_combo()
        # Show/hide the Test Audio + Hear Yourself cards based on
        # the new channel.
        self._update_channel_specific_cards(key)

    # ---------------------------------------------------------------- presets

    def _refresh_preset_combo(self) -> None:
        """Rebuild the preset combo for the current channel. Favourites
        pin to the top with a star prefix, separated from the rest of
        the list. Block signals while we mutate the model so the active
        selection change doesn't fire spurious 'load this preset'
        edits."""
        ch = self._current_channel
        favs = get_favourites(self._settings, ch)
        all_presets = list_presets(ch)
        all_names = [p["name"] for p in all_presets]
        # Favourites first (in user-defined order), then everything else
        # in the natural built-in-then-alphabetical order from
        # list_presets. Names that were favourited but no longer exist
        # (e.g. the underlying preset was deleted from disk) are skipped
        # so the dropdown never shows a dead entry.
        fav_present = [n for n in favs if n in all_names]
        non_fav = [n for n in all_names if n not in fav_present]

        was_blocked = self.preset_combo.blockSignals(True)
        try:
            self.preset_combo.clear()
            for n in fav_present:
                self.preset_combo.addItem(f"★ {n}", userData=n)
            if fav_present and non_fav:
                self.preset_combo.insertSeparator(self.preset_combo.count())
            for n in non_fav:
                self.preset_combo.addItem(n, userData=n)

            active = self._active_preset_by_channel.get(ch, "")
            if active:
                idx = self._index_for_preset_name(active)
                if idx >= 0:
                    self.preset_combo.setCurrentIndex(idx)
        finally:
            self.preset_combo.blockSignals(was_blocked)
        self._update_action_buttons()
        # The favourites quick-bar reads the same data, so any combo
        # refresh implies a favourites refresh too. Guard against
        # __init__ ordering — first call happens before the bar is
        # built — by hasattr-checking.
        if hasattr(self, "favourites_buttons_row"):
            self._refresh_favourites_card()

    def _index_for_preset_name(self, name: str) -> int:
        """Find the combo row whose userData (the underlying preset
        name) matches `name`. Necessary because favourited rows show as
        '★ Foo' but their userData is plain 'Foo'."""
        for i in range(self.preset_combo.count()):
            if self.preset_combo.itemData(i) == name:
                return i
        return -1

    def _selected_preset_name(self) -> str:
        """Return the underlying preset name for the currently selected
        combo row — or whatever the user typed if they edited the line
        and haven't picked an item yet. Strips a leading star prefix as
        a safety net for typed input."""
        data = self.preset_combo.currentData()
        if isinstance(data, str) and data:
            return data
        text = self.preset_combo.currentText().strip()
        if text.startswith("★ "):
            return text[2:].strip()
        return text

    def _on_preset_index_changed(self, _idx: int) -> None:
        # Rename + Delete + favourite-star are only meaningful for the
        # currently-selected preset's identity — refresh them when the
        # selection changes (programmatic OR user-driven).
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        name = self._selected_preset_name()
        editable = bool(name) and is_user_preset(name, self._current_channel)
        self.preset_delete_btn.setEnabled(editable)
        self.preset_rename_btn.setEnabled(editable)
        if name and is_favourite(self._settings, self._current_channel, name):
            self.preset_fav_btn.setText("★")
            self.preset_fav_btn.setToolTip(
                "Remove this preset from favourites"
            )
        else:
            self.preset_fav_btn.setText("☆")
            self.preset_fav_btn.setToolTip(
                f"Favourite this preset (up to {MAX_FAVOURITES_PER_CHANNEL} per channel)"
            )
        self.preset_fav_btn.setEnabled(bool(name))

    def _on_preset_favourite_toggled(self) -> None:
        name = self._selected_preset_name()
        if not name:
            return
        ch = self._current_channel
        if is_favourite(self._settings, ch, name):
            remove_favourite(self._settings, ch, name)
        else:
            ok = add_favourite(self._settings, ch, name)
            if not ok:
                QMessageBox.information(
                    self,
                    "Favourites full",
                    f"You can only have {MAX_FAVOURITES_PER_CHANNEL} "
                    f"favourites per channel. Remove one first, then "
                    f"favourite '{name}'.",
                )
                return
        self._refresh_preset_combo()

    def _refresh_favourites_card(self) -> None:
        """Rebuild the row of favourite quick-buttons for the current
        channel. Each button is the underlying preset name (no '★ '
        prefix — the entire bar is favourites). Click any to apply."""
        # Drop any existing buttons. QHBoxLayout.removeItem() doesn't
        # take ownership, so we have to deleteLater the widget too.
        while self.favourites_buttons_row.count():
            item = self.favourites_buttons_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        names = get_favourites(self._settings, self._current_channel)
        # Drop any favourites that no longer correspond to an existing
        # preset (built-in / bundled / user). Don't surface a warning
        # — it just means the user deleted a preset that was
        # favourited; we silently clean up.
        all_names = {p["name"] for p in list_presets(self._current_channel)}
        names = [n for n in names if n in all_names]

        if not names:
            self.favourites_empty_hint.show()
            return
        self.favourites_empty_hint.hide()

        for name in names:
            btn = QPushButton(name)
            btn.setToolTip(f"Load '{name}' on the {self._current_channel} channel")
            btn.setCursor(Qt.PointingHandCursor)
            # Fixed width keeps the row tidy even with long preset
            # names — `[ASM] Some Long Game Title.json` would otherwise
            # blow out the layout. ElideRight in QSS would be nicer
            # but QPushButton doesn't support it natively, so we cap.
            btn.setMinimumWidth(110)
            btn.setMaximumWidth(170)
            btn.clicked.connect(lambda _checked, n=name: self._apply_preset(n))
            self.favourites_buttons_row.addWidget(btn)
        self.favourites_buttons_row.addStretch(1)

    def _on_preset_activated(self, _idx: int) -> None:
        """User picked an item from the preset dropdown — apply it
        immediately. The Load button is gone; selection IS the action.
        Doesn't fire when we programmatically refresh the combo (that
        path uses currentTextChanged / setCurrentIndex), so internal
        repopulates don't trigger spurious loads."""
        self._apply_preset(self._selected_preset_name())

    def _apply_preset(self, name: str) -> None:
        """Shared code path for loading a preset by name. Called by
        the dropdown's `activated` signal, by the Favourites quick-
        bar buttons, and by anywhere else that wants to apply a
        preset without going through the user-driven combo selection.
        Updates the local band cache, re-renders the sliders, sends
        the atomic set-eq-channel to the daemon, and marks the preset
        active so subsequent slider edits know whether to fork."""
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
        self._bands_by_channel[self._current_channel] = [
            dict(b) for b in preset["bands"]
        ]
        self._render_sliders_for_channel(self._current_channel)
        self._daemon.send_command(
            "set-eq-channel",
            channel=self._current_channel,
            bands=preset["bands"],
        )
        self._active_preset_by_channel[self._current_channel] = name
        # Make sure the combo reflects what's now active — useful
        # when this was triggered by the favourites bar (combo had
        # something else selected).
        idx = self._index_for_preset_name(name)
        if idx >= 0 and self.preset_combo.currentIndex() != idx:
            was_blocked = self.preset_combo.blockSignals(True)
            try:
                self.preset_combo.setCurrentIndex(idx)
            finally:
                self.preset_combo.blockSignals(was_blocked)
        self._update_action_buttons()

    def _on_preset_save(self) -> None:
        suggested = self._selected_preset_name() or "My Preset"
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
        self._active_preset_by_channel[self._current_channel] = name.strip()
        self._refresh_preset_combo()

    def _on_preset_rename(self) -> None:
        old_name = self._selected_preset_name()
        if not old_name or not is_user_preset(old_name, self._current_channel):
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename preset",
            "New name:",
            text=old_name,
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        try:
            rename_user_preset(old_name, new_name.strip(), self._current_channel)
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return
        # Keep the favourites list in sync with the new name so the star
        # row at the top of the dropdown stays consistent.
        rename_favourite(
            self._settings, self._current_channel, old_name, new_name.strip()
        )
        self._active_preset_by_channel[self._current_channel] = new_name.strip()
        self._refresh_preset_combo()

    # ----------------------------------------------------------- test audio

    def _on_test_play(self) -> None:
        """Synthesise the currently-selected clip and stream it via
        pw-cat into the active channel's null-sink. Stops any prior
        playback first so back-to-back Play presses don't pile up."""
        self._on_test_stop()
        idx = self.test_audio_combo.currentIndex()
        if idx < 0 or idx >= len(TEST_AUDIO_CATALOGUE):
            return
        label, factory = TEST_AUDIO_CATALOGUE[idx]
        try:
            wav_path = factory()
        except Exception as e:
            QMessageBox.warning(
                self, "Test audio failed", f"Could not generate {label}: {e}"
            )
            return

        sink = CHANNEL_TO_SINK.get(self._current_channel)
        if not sink:
            return

        proc = QProcess(self)
        proc.setProgram("pw-cat")
        proc.setArguments(["-p", "--target", sink, str(wav_path)])
        proc.finished.connect(self._on_test_finished)
        # If the binary's missing the GUI shouldn't blow up — surface
        # a friendly error instead of a backtrace.
        proc.errorOccurred.connect(self._on_test_error)
        self._test_process = proc
        self.test_play_btn.setEnabled(False)
        self.test_stop_btn.setEnabled(True)
        proc.start()

    def _on_test_stop(self) -> None:
        proc = self._test_process
        if proc is None:
            return
        if proc.state() != QProcess.NotRunning:
            proc.kill()
            proc.waitForFinished(500)
        self._test_process = None
        self.test_play_btn.setEnabled(True)
        self.test_stop_btn.setEnabled(False)

    def _on_test_finished(self, _exit_code: int, _exit_status) -> None:
        # Natural end of clip — reset the buttons. Don't kill the
        # process here; QProcess auto-cleans on finished.
        self._test_process = None
        self.test_play_btn.setEnabled(True)
        self.test_stop_btn.setEnabled(False)

    def _on_test_error(self, error) -> None:
        # FailedToStart is the only one we care to surface — the user
        # is missing pw-cat (i.e. pipewire-utils isn't installed).
        # Other errors (Crashed, Timedout, …) flow through finished
        # too, so handle them once there.
        if error == QProcess.FailedToStart:
            QMessageBox.warning(
                self,
                "pw-cat missing",
                "Could not run pw-cat — install pipewire-utils to use "
                "test audio (dnf install pipewire-utils on Fedora).",
            )
        self._test_process = None
        self.test_play_btn.setEnabled(True)
        self.test_stop_btn.setEnabled(False)

    def _on_preset_delete(self) -> None:
        name = self._selected_preset_name()
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
        # Drop the deleted preset from favourites too — otherwise a
        # ghost entry would persist in settings.json.
        remove_favourite(self._settings, self._current_channel, name)
        if self._active_preset_by_channel.get(self._current_channel) == name:
            self._active_preset_by_channel[self._current_channel] = ""
        self._refresh_preset_combo()

    def _render_sliders_for_channel(self, channel: str) -> None:
        """Push the stored bands for `channel` into the slider widgets
        AND refresh the per-band name + frequency labels. Preset loads
        change frequencies, so the labels can't be static."""
        bands = self._bands_by_channel.get(channel) or _default_channel_bands()
        # Compute names up front: musical naming (Sub Bass / Bass / Mids
        # / etc.) only makes sense when frequencies don't repeat the
        # same musical band. Parametric ASM presets often place 3 or
        # 4 filters inside a single band range — labelling them all
        # 'Brilliance' or 'Bass' looks broken. When the names would
        # collide, fall back to 'Band 1..10' so every slider has a
        # unique identifier.
        n = len(self.band_sliders)
        freqs = [
            float((bands[idx] if idx < len(bands) else _default_eq_band(idx)).get("freq", 1000.0))
            for idx in range(n)
        ]
        musical = [_band_name_for(f) for f in freqs]
        if len(set(musical)) == len(musical):
            names = musical
        else:
            names = [f"Band {i + 1}" for i in range(n)]

        for idx in range(n):
            band = bands[idx] if idx < len(bands) else _default_eq_band(idx)
            gain_db = float(band.get("gain", 0.0))
            freq = freqs[idx]

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
            name_lbl.setText(names[idx])
            freq_lbl.setText(_format_freq(freq))

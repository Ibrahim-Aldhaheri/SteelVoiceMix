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
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..eq_graph_widget import EqGraphWidget
from ..searchable_select import SearchableSelect

from ..eq_presets import (
    delete_user_override,
    delete_user_preset,
    find_preset,
    has_user_override,
    is_user_preset,
    list_presets,
    load_user_override,
    next_custom_name,
    rename_user_preset,
    save_user_override,
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


# Macro zone splits — bands with freq < BASS_VOICE_HZ get the bass
# trim, < VOICE_TREBLE_HZ get the voice trim, otherwise the treble
# trim. Boundaries chosen to match common DAW bass/mid/treble
# splits; tweakable here in one place.
MACRO_BASS_VOICE_HZ = 250.0
MACRO_VOICE_TREBLE_HZ = 4000.0


def _macro_trim_for_freq(freq_hz: float, macros: dict[str, float]) -> float:
    """Pick which macro slider applies to a band of frequency
    `freq_hz`, return the offset in dB."""
    if freq_hz < MACRO_BASS_VOICE_HZ:
        return float(macros.get("bass", 0.0))
    if freq_hz < MACRO_VOICE_TREBLE_HZ:
        return float(macros.get("voice", 0.0))
    return float(macros.get("treble", 0.0))


def _apply_macros_to_bands(
    bands: list[dict], macros: dict[str, float],
) -> list[dict]:
    """Return a copy of `bands` with each band's gain offset by the
    macro for its zone. Used at send time so the daemon receives
    the effective gains while the stored band values stay untouched."""
    out: list[dict] = []
    for b in bands:
        adj = dict(b)
        trim = _macro_trim_for_freq(float(b.get("freq", 1000.0)), macros)
        if abs(trim) > 1e-4:
            adj["gain"] = max(-24.0, min(24.0, float(b.get("gain", 0.0)) + trim))
        out.append(adj)
    return out


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
        # Hydrate from settings so a fresh GUI launch can restore
        # which preset name was last active per channel — needed by
        # the Auto Game-EQ orchestrator to remember the user's
        # pre-game preset for the exit notification.
        persisted_active = self._settings.get("eq_active_preset_by_channel") or {}
        self._active_preset_by_channel: dict[str, str] = {
            ch: str(persisted_active.get(ch, "") or "")
            for ch in ("game", "chat", "media", "hdmi", "mic")
        }

        # Bass / Voice / Treble macro values per channel. Each macro
        # is a single ±12 dB offset that tilts a slice of the
        # spectrum — bass for <250 Hz bands, voice for 250 Hz–4 kHz,
        # treble for >4 kHz. Stored separately from band gains and
        # applied at send time so the user can dial them up/down
        # without permanently shifting the underlying band values.
        # Persisted alongside band overrides in the preset's
        # override file.
        self._macros_by_channel: dict[str, dict[str, float]] = {
            ch: {"bass": 0.0, "voice": 0.0, "treble": 0.0}
            for ch in ("game", "chat", "media", "hdmi", "mic")
        }

        # Original preset bands captured at load time. Reset button
        # restores from here. For bundled / built-in presets these
        # are the package defaults; for user presets they're
        # whatever was on disk at load. Refreshed on every preset
        # load.
        self._preset_defaults_by_channel: dict[str, list[dict]] = {
            ch: [] for ch in ("game", "chat", "media", "hdmi", "mic")
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
            self.tr("Enable {n}-band parametric EQ").format(n=NUM_EQ_BANDS),
            tooltip=self.tr(
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
        ch_row.addWidget(QLabel(self.tr("Channel")))
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
            self.tr("Equalizer"), enable_row, ch_row, self.auto_lock_banner,
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
            self.tr("Favourite this preset (up to {n} per channel)").format(
                n=MAX_FAVOURITES_PER_CHANNEL,
            )
        )
        self.preset_fav_btn.clicked.connect(self._on_preset_favourite_toggled)
        preset_picker_row.addWidget(self.preset_fav_btn)

        # Action row — Load is gone (selecting from the combo auto-
        # applies). The remaining buttons handle the actions that DO
        # require explicit confirmation: saving the current state,
        # renaming a user preset, deleting a user preset.
        preset_btn_row = QHBoxLayout()
        # Reset button — restores the preset's defaults (bundled
        # values for bundled / built-in presets, on-disk values for
        # user presets) and clears the Bass / Voice / Treble macros.
        # Enabled only when an override exists or macros are non-zero.
        self.preset_reset_btn = QPushButton(self.tr("Reset"))
        self.preset_reset_btn.setToolTip(self.tr(
            "Restore this preset's defaults and clear the "
            "Bass / Voice / Treble macros."
        ))
        self.preset_reset_btn.clicked.connect(self._on_preset_reset)
        self.preset_reset_btn.setEnabled(False)
        self.preset_save_btn = QPushButton(self.tr("Save as…"))
        self.preset_save_btn.setToolTip(self.tr(
            "Save the current bands as a new user preset."
        ))
        self.preset_save_btn.clicked.connect(self._on_preset_save)
        self.preset_rename_btn = QPushButton(self.tr("Rename…"))
        self.preset_rename_btn.clicked.connect(self._on_preset_rename)
        self.preset_rename_btn.setEnabled(False)
        self.preset_delete_btn = QPushButton(self.tr("Delete"))
        self.preset_delete_btn.clicked.connect(self._on_preset_delete)
        self.preset_delete_btn.setEnabled(False)
        preset_btn_row.addWidget(self.preset_reset_btn)
        preset_btn_row.addWidget(self.preset_save_btn)
        preset_btn_row.addWidget(self.preset_rename_btn)
        preset_btn_row.addWidget(self.preset_delete_btn)
        preset_btn_row.addStretch(1)

        layout.addWidget(card(self.tr("Preset"), preset_picker_row, preset_btn_row))
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
            self.tr("No favourites yet — tap ★ next to a preset to pin it here.")
        )
        self.favourites_empty_hint.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        self.favourites_card_layout.addLayout(self.favourites_buttons_row)
        self.favourites_card_layout.addWidget(self.favourites_empty_hint)
        layout.addWidget(card(self.tr("Favourites"), self.favourites_card_layout))
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

        # Help text — phrased to cover both views. The graph view's
        # discovery hint ("Click anywhere to add a point") lives inside
        # the empty graph itself; this label covers the generic side.
        eq_help = QLabel(
            self.tr(
                "Boost or cut a frequency band by up to ±12 dB. "
                "Sliders view: drag a slider for that band. "
                "Graph view: click empty space to drop a point, "
                "drag points to shape the curve, right-click a "
                "point to remove it. Each commit respawns the filter "
                "chain (~100 ms audio glitch per change)."
            )
        )
        eq_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        eq_help.setWordWrap(True)

        # View-mode toggle: Sliders (existing) vs Graph (new Sonar-style
        # dot-on-curve). Both views read/write the same _bands_by_channel
        # dict so switching mid-session is lossless. Default = Sliders to
        # avoid surprising existing users; persists across launches.
        view_row = QHBoxLayout()
        view_row.setSpacing(6)
        self.view_sliders_btn = QPushButton(self.tr("Sliders"))
        self.view_sliders_btn.setCheckable(True)
        self.view_graph_btn = QPushButton(self.tr("Graph"))
        self.view_graph_btn.setCheckable(True)
        self._view_button_group = QButtonGroup(self)
        self._view_button_group.setExclusive(True)
        self._view_button_group.addButton(self.view_sliders_btn, 0)
        self._view_button_group.addButton(self.view_graph_btn, 1)
        view_row.addWidget(QLabel(self.tr("View:")))
        view_row.addWidget(self.view_sliders_btn)
        view_row.addWidget(self.view_graph_btn)
        view_row.addStretch(1)

        # Slider grid wrapped so QStackedWidget can swap it as a unit.
        sliders_page = QWidget()
        sliders_page.setLayout(bands_row)

        # Graph view — emits bandChanged on drag, bandReleased on
        # mouse-up. We mirror those into the same daemon-commit path
        # the sliders use so behaviour stays identical.
        self.eq_graph = EqGraphWidget()
        self.eq_graph.bandChanged.connect(self._on_graph_band_changed)
        self.eq_graph.bandReleased.connect(self._on_graph_band_released)
        self.eq_graph.bandQChanged.connect(self._on_graph_band_q_changed)
        self.eq_graph.selectionChanged.connect(self._on_graph_selection_changed)
        # Inspector edits commit the full band atomically — same code
        # path the drag-release uses, just triggered by a spinner /
        # combo edit instead of a mouse release.
        self.eq_graph.band_inspector.band_edited.connect(
            self._on_graph_band_released
        )

        # Graph page wraps the graph widget plus a row of three
        # macro sliders (Bass / Voice / Treble). The sliders tilt
        # the curve in their frequency zones — bass <250 Hz, voice
        # 250 Hz–4 kHz, treble >4 kHz — and the daemon receives the
        # effective bands (stored gain + macro trim) so what you
        # hear matches the drawn curve.
        graph_page = QWidget()
        graph_page_layout = QVBoxLayout(graph_page)
        graph_page_layout.setContentsMargins(0, 0, 0, 0)
        graph_page_layout.setSpacing(8)
        graph_page_layout.addWidget(self.eq_graph, 1)
        macros_row = QHBoxLayout()
        macros_row.setSpacing(12)
        self.macro_sliders: dict[str, NoWheelSlider] = {}
        self.macro_value_labels: dict[str, QLabel] = {}
        for key, label_text in (
            ("bass",   self.tr("Bass")),
            ("voice",  self.tr("Voice")),
            ("treble", self.tr("Treble")),
        ):
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignHCenter)
            value_lbl = QLabel("0.0")
            value_lbl.setAlignment(Qt.AlignCenter)
            value_lbl.setStyleSheet("font-size: 10px; font-weight: bold; min-width: 36px;")
            col.addWidget(value_lbl)
            slider = NoWheelSlider(Qt.Horizontal)
            slider.setRange(-120, 120)   # 0.1 dB units, ±12 dB
            slider.setValue(0)
            slider.setTickPosition(QSlider.NoTicks)
            slider.setFixedHeight(20)
            slider.setMinimumWidth(120)
            slider.valueChanged.connect(
                lambda v, k=key, lbl=value_lbl: self._on_macro_changed(k, v, lbl)
            )
            slider.sliderReleased.connect(
                lambda k=key, s=slider: self._on_macro_released(k, s)
            )
            self.macro_sliders[key] = slider
            self.macro_value_labels[key] = value_lbl
            col.addWidget(slider)
            name_lbl = QLabel(label_text)
            name_lbl.setAlignment(Qt.AlignCenter)
            name_lbl.setStyleSheet("font-size: 10px; font-weight: bold;")
            col.addWidget(name_lbl)
            macros_row.addLayout(col, 1)
        graph_page_layout.addLayout(macros_row)

        self.eq_view_stack = QStackedWidget()
        self.eq_view_stack.addWidget(sliders_page)
        self.eq_view_stack.addWidget(graph_page)

        # Restore persisted view mode (defaults to sliders).
        initial_view = str(self._settings.get("eq_view_mode") or "sliders")
        if initial_view == "graph":
            self.view_graph_btn.setChecked(True)
            self.eq_view_stack.setCurrentIndex(1)
        else:
            self.view_sliders_btn.setChecked(True)
            self.eq_view_stack.setCurrentIndex(0)
        self._view_button_group.idClicked.connect(self._on_view_mode_changed)

        layout.addWidget(card(self.tr("Bands"), view_row, self.eq_view_stack, eq_help))

        # Test-audio card ----------------------------------------------
        test_row = QHBoxLayout()
        self.test_audio_combo = NoWheelComboBox()
        for label, _factory in TEST_AUDIO_CATALOGUE:
            self.test_audio_combo.addItem(self.tr(label))
        self.test_audio_combo.setMinimumWidth(200)
        test_row.addWidget(self.test_audio_combo, 1)
        self.test_play_btn = QPushButton(self.tr("▶ Play"))
        self.test_play_btn.clicked.connect(self._on_test_play)
        self.test_stop_btn = QPushButton("⏹")
        self.test_stop_btn.setFixedWidth(36)
        self.test_stop_btn.clicked.connect(self._on_test_stop)
        self.test_stop_btn.setEnabled(False)
        test_row.addWidget(self.test_play_btn)
        test_row.addWidget(self.test_stop_btn)
        test_warn = QLabel(
            self.tr(
                "⚠ Drop system volume to ~10–20% BEFORE pressing Play. "
                "These clips are intentionally whisper-quiet to protect "
                "your hearing, but if your headset gain or system volume "
                "is high they can still be uncomfortable. Hit Stop "
                "immediately if anything feels too loud."
            )
        )
        test_warn.setWordWrap(True)
        test_warn.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #FF9800;"
        )
        test_help = QLabel(
            self.tr(
                "Reference signals for ear-checking the EQ — pink noise "
                "is the recommended starting point. Each clip ramps in over "
                "200 ms so even the onset is gentle."
            )
        )
        test_help.setWordWrap(True)
        test_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )
        # Test Audio card — for ear-checking output-channel EQ. We
        # cache the widget so _on_channel_changed can hide it when
        # the user switches to Mic (where Test Audio doesn't make
        # sense — a noise generator wouldn't go through the mic).
        self.test_audio_card = card(self.tr("Test Audio"), test_warn, test_row, test_help)
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
        self.voice_test_btn = QPushButton(self.tr("🎧  Hear yourself (test mic)"))
        self.voice_test_btn.setCheckable(True)
        self.voice_test_btn.setMaximumWidth(280)
        self.voice_test_btn.toggled.connect(self._on_voice_test_toggled)
        btn_row.addWidget(self.voice_test_btn)
        btn_row.addStretch(1)

        help_lbl = QLabel(
            self.tr(
                "Loops the processed SteelMic back through your headset "
                "so you can A/B the EQ live. Same control as the "
                "Microphone tab — toggling either button drives both."
            )
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        if self._voice_test is not None:
            self._voice_test.state_changed.connect(
                self._on_voice_test_state_changed
            )

        return card(self.tr("Listen"), btn_row, help_lbl)

    def _on_voice_test_toggled(self, checked: bool) -> None:
        if self._voice_test is None:
            return
        if checked:
            ok, err = self._voice_test.start()
            if not ok:
                QMessageBox.warning(self, self.tr("Voice-test failed"), err)
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
            self.tr("Auto-switch EQ when a known game launches"),
            badge="ALPHA",
        )
        self.auto_game_toggle.setChecked(
            bool(self._settings.get("auto_game_eq_enabled", False))
        )
        self.auto_game_toggle.toggled.connect(self._toggle_auto_game)

        self.detected_label = QLabel(self.tr("Currently detected: none"))
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
        add_btn = QPushButton(self.tr("Add binding…"))
        add_btn.clicked.connect(self._add_binding)
        del_btn = QPushButton(self.tr("Remove selected"))
        del_btn.clicked.connect(self._remove_binding)
        bindings_btns.addWidget(add_btn)
        bindings_btns.addWidget(del_btn)
        bindings_btns.addStretch(1)

        help_lbl = QLabel(
            self.tr(
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
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        return card(
            self.tr("Auto Game-EQ"),
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
                self.tr(
                    "🎮 Auto Game-EQ active — preset: {preset}. Sliders "
                    "are still editable; your tweaks stay until the next "
                    "game change or until you close the game (at which "
                    "point your pre-game EQ is restored)."
                ).format(preset=preset_name)
            )
            self.auto_lock_banner.show()
            self._active_preset_by_channel["game"] = preset_name
            self._persist_active_presets()
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
            self._persist_active_presets()
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
            self.detected_label.setText(self.tr("Currently detected: none"))
            self.detected_label.setStyleSheet(
                "font-size: 10px; padding: 4px 0; "
                "color: palette(placeholder-text);"
            )
            return
        preset_part = (
            f" → {preset}" if preset else self.tr(" (no preset match)")
        )
        if on_steel_game:
            # We confirmed the sink-input is on SteelGame — EQ will
            # be audible.
            self.detected_label.setText(
                self.tr("Currently detected: {name}{suffix}").format(
                    name=name, suffix=preset_part,
                )
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
                self.tr(
                    "Currently detected: {name}{suffix} — EQ applied; "
                    "if you don't hear a change, route the game to "
                    "SteelGame in your system audio settings"
                ).format(name=name, suffix=preset_part)
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

    def _collect_binding_candidates(self) -> tuple[list[str], bool]:
        """Build a deduped, ordered list of binding suggestions plus a
        flag for whether wmctrl was available. Audio clients first
        (proven to match PipeWire's application.name), then windowed
        apps via wmctrl. Free-text input is still allowed — the combo
        is editable.

        Returns (candidates, wmctrl_available). The dialog uses the
        flag to surface a warning when the windowed-apps suggestion
        source is unavailable, so the user knows to type a name
        manually instead of expecting their open Steam/game window
        to appear."""
        seen: set[str] = set()
        out: list[str] = []
        # Active audio clients from the watcher — reuse what's
        # already in cache rather than re-running pactl.
        if self._game_eq_manager is not None:
            for name in sorted(self._game_eq_manager.latest_seen().keys()):
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        # Windowed applications via wmctrl. If wmctrl isn't installed
        # the dialog still works (audio-clients + free-text), but the
        # user gets a warning so they understand why their open game
        # window isn't in the dropdown.
        import shutil
        import subprocess
        wmctrl_available = bool(shutil.which("wmctrl"))
        if wmctrl_available:
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
        return out, wmctrl_available

    def _add_binding(self) -> None:
        # Custom dialog (not QInputDialog.getItem) — the editable=True
        # variant of getItem hides the dropdown arrow on some Qt
        # themes and the field reads as a plain text-edit, which the
        # user reported as 'no dropdown'. Building one ourselves lets
        # us show a real combo with explicit dropdown UI.
        active, wmctrl_available = self._collect_binding_candidates()
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import (
            QComboBox as _QComboBox,
            QDialog as _QDialog,
            QDialogButtonBox as _QDialogButtonBox,
            QLabel as _QLabel,
            QVBoxLayout as _QVBoxLayout,
        )
        dlg = _QDialog(self)
        dlg.setWindowTitle(self.tr("Bind app to preset"))
        from PySide6.QtWidgets import QApplication as _QApp
        app = _QApp.instance()
        if app is not None:
            dlg.setLayoutDirection(app.layoutDirection())
        dlg.setMinimumWidth(420)
        dlg_layout = _QVBoxLayout(dlg)
        prompt_lbl = _QLabel(
            self.tr(
                "Pick a running app or type a custom name (the field is "
                "editable). Bindings match against PipeWire's "
                "application.name when audio starts."
            )
            if active else
            self.tr(
                "No running apps detected — type a custom name below. "
                "It'll match when the app eventually produces audio."
            )
        )
        prompt_lbl.setWordWrap(True)
        dlg_layout.addWidget(prompt_lbl)

        # If wmctrl is missing the dropdown only shows audio clients
        # (no open-windows source). Surface that explicitly so the
        # user knows their open game-launcher window won't appear in
        # the list — they have to type the name manually.
        if not wmctrl_available:
            warn_lbl = _QLabel(self.tr(
                "⚠ <code>wmctrl</code> not installed — open windows are "
                "not in the dropdown. Install <code>wmctrl</code> "
                "(<code>dnf install wmctrl</code>) to get window-title "
                "suggestions, or type the app name manually."
            ))
            warn_lbl.setWordWrap(True)
            warn_lbl.setTextFormat(_Qt.RichText)
            warn_lbl.setStyleSheet(
                "background: rgba(255, 152, 0, 0.18);"
                "border: 1px solid rgba(255, 152, 0, 0.6);"
                "border-radius: 4px;"
                "color: palette(text);"
                "font-size: 11px;"
                "padding: 6px;"
            )
            dlg_layout.addWidget(warn_lbl)

        combo = _QComboBox()
        combo.setEditable(True)
        combo.addItems(active)
        # Setting the line-edit's placeholder makes the empty-list
        # case obviously a free-text entry.
        if not active and combo.lineEdit() is not None:
            combo.lineEdit().setPlaceholderText(self.tr("e.g. Hunt: Showdown"))
        dlg_layout.addWidget(combo)
        buttons = _QDialogButtonBox(
            _QDialogButtonBox.Ok | _QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)
        if dlg.exec() != _QDialog.Accepted:
            return
        name = combo.currentText().strip()
        if not name:
            return
        preset_names = [p["name"] for p in list_presets("game")]
        if not preset_names:
            QMessageBox.warning(
                self,
                self.tr("No presets available"),
                self.tr(
                    "There are no Game-channel presets to bind. Save one "
                    "from this tab first."
                ),
            )
            return
        preset, ok = QInputDialog.getItem(
            self,
            self.tr("Preset for '{name}'").format(name=name),
            self.tr("EQ preset:"),
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
        """Daemon broadcast: bands for `channel` changed.

        Daemon receives effective bands (stored gain + macro trim
        per zone) so the chain matches the drawn curve. The echo
        carries those effective gains. To keep the stored bands
        free of macro double-application on subsequent edits, we
        reverse the macro fold before caching."""
        if channel not in self._bands_by_channel:
            return
        macros = self._macros_by_channel.get(channel, {})
        if any(abs(v) > 1e-4 for v in macros.values()):
            stored = []
            for b in bands:
                adj = dict(b)
                trim = _macro_trim_for_freq(
                    float(b.get("freq", 1000.0)), macros,
                )
                adj["gain"] = float(b.get("gain", 0.0)) - trim
                stored.append(adj)
            self._bands_by_channel[channel] = stored
        else:
            self._bands_by_channel[channel] = list(bands)
        if channel == self._current_channel:
            self._render_sliders_for_channel(channel)

    def on_full_state(self, state: dict) -> None:
        """Initial Status snapshot delivered every channel's band data
        at once (Game / Chat / Media / HDMI / Mic). Cache them all and
        reconcile the active-preset name from the loaded bands so the
        preset combo doesn't show 'Flat' while the sliders sit on a
        non-flat preset (the bug we hit on first restart after upgrade
        from a stable that didn't persist active_preset).

        If a per-channel active preset has a stored override, load
        the override's bands + macros instead of the daemon's
        snapshot — the override is what the user expects to see
        after a restart."""
        for ch in ("game", "chat", "media", "hdmi", "mic"):
            if ch not in state:
                continue
            active = self._active_preset_by_channel.get(ch, "")
            override = load_user_override(active, ch) if active else None
            if override is not None:
                self._bands_by_channel[ch] = [dict(b) for b in override["bands"]]
                self._macros_by_channel[ch] = dict(override["macros"])
                preset = find_preset(active, ch)
                if preset is not None:
                    self._preset_defaults_by_channel[ch] = [
                        dict(b) for b in preset["bands"]
                    ]
            else:
                self._bands_by_channel[ch] = list(state[ch])
                self._macros_by_channel[ch] = {"bass": 0.0, "voice": 0.0, "treble": 0.0}
            self._reconcile_active_preset(ch)
        # Full-state snapshots win unconditionally — drop any
        # in-flight local-authority window.
        self.eq_graph.reset_local_authority()
        self._sync_macros_to_widgets(self._current_channel)
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
        self._persist_active_presets()

    def _persist_active_presets(self) -> None:
        """Mirror the per-channel active preset names to settings.json
        so the Auto Game-EQ orchestrator can read what was selected
        pre-game. Called from every site that mutates
        _active_preset_by_channel — it's a small JSON write and the
        sites are all user-initiated (preset pick / save / rename
        / delete), so the cost per operation is negligible."""
        self._settings["eq_active_preset_by_channel"] = dict(
            self._active_preset_by_channel
        )
        try:
            save_settings(self._settings)
        except Exception:
            pass

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
        labels = [
            ("game", self.tr("🎮 Game")),
            ("chat", self.tr("💬 Chat")),
        ]
        if self._media_sink_enabled:
            labels.append(("media", self.tr("🎵 Media")))
        if self._hdmi_sink_enabled:
            labels.append(("hdmi", self.tr("📺 HDMI")))
        labels.append(("mic", self.tr("🎙 Microphone")))

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
        selected channel.

        The daemon receives gain with the active channel's macro for
        that band's zone folded in — same math as set-eq-channel /
        set-eq-band paths — so the audible response stays consistent
        with the drawn curve. Persistence: see _persist_active_mods.
        """
        channel = self._current_channel
        macros = self._macros_by_channel[channel]
        bands = self._bands_by_channel[channel]
        for band_num, value_tenths in self._pending_band_value.items():
            gain_db = value_tenths / 10.0
            # Apply the macro offset for this band's frequency zone.
            slot = band_num - 1
            if 0 <= slot < len(bands):
                trim = _macro_trim_for_freq(
                    float(bands[slot].get("freq", 1000.0)), macros,
                )
                gain_db = max(-24.0, min(24.0, gain_db + trim))
            self._daemon.send_command(
                "set-eq-band-gain",
                channel=channel,
                band=band_num,
                gain_db=gain_db,
            )
        self._pending_band_value.clear()
        self._persist_active_mods(channel)
        self._update_action_buttons()

    def _persist_active_mods(self, channel: str) -> None:
        """Write the current bands + macros to wherever modifications
        for the active preset should land. Sonar-style: edits to a
        bundled / built-in preset write a user override file, edits
        to a user preset save in place. With no active preset there's
        nowhere to save — modifications stay in-memory until the user
        picks Save As."""
        active = self._active_preset_by_channel.get(channel, "")
        if not active:
            return
        bands = self._bands_by_channel[channel]
        macros = self._macros_by_channel[channel]
        try:
            if is_user_preset(active, channel):
                # User presets persist their macros via the same
                # override path so a single Reset round-trips both.
                save_user_preset(active, channel, bands)
                if any(abs(v) > 1e-4 for v in macros.values()):
                    save_user_override(active, channel, bands, macros)
                else:
                    delete_user_override(active, channel)
            else:
                # Bundled / built-in: override layer captures the mods
                # so the source preset stays canonical for Reset.
                save_user_override(active, channel, bands, macros)
        except ValueError:
            # Sanitisation rejected the name — log and skip; this
            # is auto-save, not user-driven, so a dialog would be
            # noise.
            pass

    def _maybe_fork_to_custom(self) -> None:
        """Deprecated no-op — kept so any remaining call sites don't
        break. Sonar-style edits write to an override file for the
        active preset instead of forking to Custom N. The
        `next_custom_name` path is still alive for explicit "Save
        As" via the preset Save button."""

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
        # Drop the graph's local-authority window — it applied to the
        # previous channel; the new channel's bands must paint
        # immediately even if the user was rapidly editing on the
        # outgoing channel.
        self.eq_graph.reset_local_authority()
        self._sync_macros_to_widgets(key)
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
        channel = self._current_channel
        editable = bool(name) and is_user_preset(name, channel)
        self.preset_delete_btn.setEnabled(editable)
        self.preset_rename_btn.setEnabled(editable)
        # Reset is meaningful when the loaded preset has been modified
        # — either an override file exists, or any macro is non-zero.
        active = self._active_preset_by_channel.get(channel, "")
        macros_active = any(
            abs(v) > 1e-4 for v in self._macros_by_channel.get(channel, {}).values()
        )
        has_override = bool(active) and has_user_override(active, channel)
        self.preset_reset_btn.setEnabled(bool(active) and (has_override or macros_active))
        if name and is_favourite(self._settings, channel, name):
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

        Sonar-style behaviour: if the user has previously modified
        this preset (saved as an override), the modified bands +
        macros load; otherwise the preset's defaults load with
        zero macros. The defaults are captured in
        `_preset_defaults_by_channel` so the Reset button can
        restore them. The daemon receives the effective bands
        (stored + macros applied) so what you hear matches the
        drawn curve."""
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
        channel = self._current_channel
        # Snapshot the factory defaults for Reset BEFORE we look at
        # overrides. `preset["bands"]` is the bundled/built-in/user-
        # saved file value, untouched by any user mods.
        self._preset_defaults_by_channel[channel] = [
            dict(b) for b in preset["bands"]
        ]
        override = load_user_override(name, channel)
        if override is not None:
            bands = [dict(b) for b in override["bands"]]
            self._macros_by_channel[channel] = dict(override["macros"])
        else:
            bands = [dict(b) for b in preset["bands"]]
            self._macros_by_channel[channel] = {"bass": 0.0, "voice": 0.0, "treble": 0.0}
        self._bands_by_channel[channel] = bands
        # Preset load is a deliberate full-state replacement — drop
        # the graph's local-authority window so the new bands paint
        # immediately, not after the rolling timeout.
        self.eq_graph.reset_local_authority()
        self._render_sliders_for_channel(channel)
        self._sync_macros_to_widgets(channel)
        self._daemon.send_command(
            "set-eq-channel",
            channel=channel,
            bands=_apply_macros_to_bands(bands, self._macros_by_channel[channel]),
        )
        self._active_preset_by_channel[channel] = name
        self._persist_active_presets()
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

    def _on_preset_reset(self) -> None:
        """Restore the active preset's defaults and clear macros.
        For bundled / built-in presets, deletes the user override
        file. For user presets, reloads from disk (whatever the
        saved values are). Sends set-eq-channel atomic with the
        defaults so the daemon snaps cleanly."""
        channel = self._current_channel
        active = self._active_preset_by_channel.get(channel, "")
        if not active:
            return
        # Drop any override layer so the next find_preset sees the
        # bundled / built-in values. For user presets there's no
        # override — delete_user_override is a no-op.
        delete_user_override(active, channel)
        defaults = self._preset_defaults_by_channel.get(channel)
        if not defaults:
            # Fallback: re-resolve via find_preset (defaults dict
            # may be empty on first load after upgrade from a
            # version without the snapshot).
            preset = find_preset(active, channel)
            if preset is None:
                return
            defaults = preset["bands"]
        self._bands_by_channel[channel] = [dict(b) for b in defaults]
        self._macros_by_channel[channel] = {"bass": 0.0, "voice": 0.0, "treble": 0.0}
        self.eq_graph.reset_local_authority()
        self._sync_macros_to_widgets(channel)
        self._render_sliders_for_channel(channel)
        self._daemon.send_command(
            "set-eq-channel",
            channel=channel,
            bands=self._bands_by_channel[channel],
        )
        self._update_action_buttons()

    def _on_macro_changed(
        self, key: str, value_tenths: int, label: QLabel,
    ) -> None:
        """Macro slider drag tick. Updates the displayed value and
        the in-memory macro state; the actual daemon commit fires on
        release so we don't respawn the filter chain on every pixel
        of slider travel."""
        gain_db = value_tenths / 10.0
        sign = "+" if gain_db > 0 else ""
        label.setText(f"{sign}{gain_db:.1f}")
        channel = self._current_channel
        self._macros_by_channel[channel][key] = gain_db
        # Live preview: graph curve recomputes via set_macros even
        # before the daemon hears about it, so the user sees the
        # tilt immediately.
        self.eq_graph.set_macros(**self._macros_by_channel[channel])

    def _on_macro_released(self, _key: str, _slider) -> None:
        """Macro slider released — flush the channel's effective
        bands to the daemon (a single set-eq-channel keeps the chain
        respawn atomic) and persist the modification as an override.
        Cheaper than per-band updates since macro touches up to 10
        bands at once."""
        channel = self._current_channel
        bands = self._bands_by_channel[channel]
        macros = self._macros_by_channel[channel]
        self._daemon.send_command(
            "set-eq-channel",
            channel=channel,
            bands=_apply_macros_to_bands(bands, macros),
        )
        self._persist_active_mods(channel)
        self._update_action_buttons()

    def _sync_macros_to_widgets(self, channel: str) -> None:
        """Push the channel's macro values into the slider widgets
        (blocked signals so we don't echo a set-eq-channel from a
        programmatic slider update) and into the graph for the curve."""
        macros = self._macros_by_channel[channel]
        for key, slider in self.macro_sliders.items():
            v = float(macros.get(key, 0.0))
            value_tenths = int(round(v * 10))
            was_blocked = slider.blockSignals(True)
            try:
                slider.setValue(value_tenths)
            finally:
                slider.blockSignals(was_blocked)
            lbl = self.macro_value_labels[key]
            sign = "+" if v > 0 else ""
            lbl.setText(f"{sign}{v:.1f}")
        self.eq_graph.set_macros(**macros)

    def _on_preset_save(self) -> None:
        suggested = self._selected_preset_name() or self.tr("My Preset")
        name, ok = QInputDialog.getText(
            self,
            self.tr("Save preset"),
            self.tr("Save current {channel} EQ as:").format(
                channel=self._current_channel,
            ),
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
            QMessageBox.warning(self, self.tr("Could not save preset"), str(e))
            return
        self._active_preset_by_channel[self._current_channel] = name.strip()
        self._persist_active_presets()
        self._refresh_preset_combo()

    def _on_preset_rename(self) -> None:
        old_name = self._selected_preset_name()
        if not old_name or not is_user_preset(old_name, self._current_channel):
            return
        new_name, ok = QInputDialog.getText(
            self,
            self.tr("Rename preset"),
            self.tr("New name:"),
            text=old_name,
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        try:
            rename_user_preset(old_name, new_name.strip(), self._current_channel)
        except ValueError as e:
            QMessageBox.warning(self, self.tr("Could not rename"), str(e))
            return
        # Keep the favourites list in sync with the new name so the star
        # row at the top of the dropdown stays consistent.
        rename_favourite(
            self._settings, self._current_channel, old_name, new_name.strip()
        )
        self._active_preset_by_channel[self._current_channel] = new_name.strip()
        self._persist_active_presets()
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
            self._persist_active_presets()
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

        # Keep the graph view in sync with the slider grid. Both views
        # render from _bands_by_channel, but the graph caches its own
        # copy so it stays consistent across paint events — push fresh
        # bands now that we've decided what this channel looks like.
        if hasattr(self, "eq_graph"):
            self.eq_graph.set_bands(bands)

    def _on_view_mode_changed(self, btn_id: int) -> None:
        """Switch between Sliders (0) and Graph (1) views and persist
        the choice. Both views read/write the same _bands_by_channel
        dict so no data migration is needed across the switch."""
        self.eq_view_stack.setCurrentIndex(btn_id)
        self._settings["eq_view_mode"] = "graph" if btn_id == 1 else "sliders"
        save_settings(self._settings)

    def _on_graph_band_changed(
        self, band_idx: int, freq_hz: float, gain_db: float,
    ) -> None:
        """Graph dot drag tick. Mirrors _on_slider_changed: update the
        local bands dict, fork to Custom-N if needed, queue a debounced
        commit of the full band (since freq can change too — the slider
        path only does gain, but graph drags move freq + gain together)."""
        channel = self._current_channel
        bands = self._bands_by_channel[channel]
        if not (0 <= band_idx < len(bands)):
            return
        band = bands[band_idx]
        band["freq"] = float(freq_hz)
        band["gain"] = float(gain_db)
        self._maybe_fork_to_custom()
        # Sync the slider grid so view-switching during an unreleased
        # drag shows the in-flight values. Freq dragged peaking bands
        # also need their freq label refreshed — gain only would let
        # the slider lag the dot.
        if 0 <= band_idx < len(self.band_sliders):
            slider = self.band_sliders[band_idx]
            value_lbl = self.band_value_labels[band_idx]
            freq_lbl = self.band_freq_labels[band_idx]
            value_tenths = int(round(float(gain_db) * 10))
            was_blocked = slider.blockSignals(True)
            slider.setValue(value_tenths)
            slider.blockSignals(was_blocked)
            sign = "+" if gain_db > 0 else ""
            value_lbl.setText(f"{sign}{float(gain_db):.1f}")
            freq_lbl.setText(_format_freq(float(freq_hz)))
        # Debounced commit — same pattern as the slider drag.
        self._pending_band_value[band_idx + 1] = int(round(float(gain_db) * 10))
        self._commit_timer.start()

    def _on_graph_band_q_changed(self, band_idx: int, q: float) -> None:
        """Scroll-wheel Q tick from the graph. Update local state;
        the bandReleased that follows will flush a single set-eq-band
        atomic write covering the new Q value (the slider path has
        no Q control so we can't reuse its gain-only debounce)."""
        channel = self._current_channel
        bands = self._bands_by_channel[channel]
        if 0 <= band_idx < len(bands):
            bands[band_idx]["q"] = float(q)
            self._maybe_fork_to_custom()

    def _on_graph_selection_changed(self, _band_idx: int) -> None:
        """Inspector visibility is managed inside the graph widget
        itself; this slot exists so external listeners (status bar
        readouts, band-name labels elsewhere) can hook in later
        without re-routing the graph's signal."""

    def _on_graph_band_released(self, band_idx: int) -> None:
        """Drag or inspector edit finished — push the full band
        (freq + gain + q + type) atomically via set-eq-band, since
        freq/q/type may have changed. The slider path's
        set-eq-band-gain only handles gain so it can't cover this
        case. Cancel any pending debounce so we don't fire a stale
        gain-only commit afterward.

        Sync from `eq_graph._bands` BEFORE building the commit:
        inspector edits (filter type, gain spinbox, freq spinbox, Q
        spinbox) and scroll-wheel Q updates mutate only the graph's
        local copy. Without this sync, the daemon would receive the
        parent's stale values and echo them right back, snapping
        the dot to its pre-edit position."""
        channel = self._current_channel
        bands = self._bands_by_channel[channel]
        if not (0 <= band_idx < len(bands)):
            return
        if 0 <= band_idx < len(self.eq_graph._bands):
            graph_band = self.eq_graph._bands[band_idx]
            for key in ("freq", "gain", "q", "type", "enabled"):
                if key in graph_band:
                    bands[band_idx][key] = graph_band[key]
        # The dragged band may share its band-number slot with a
        # pending-gain entry from the same drag — drop it, the
        # set-eq-band below supersedes a gain-only update.
        self._pending_band_value.pop(band_idx + 1, None)
        self._commit_timer.stop()
        # Flush any other bands that had pending gain changes too.
        if self._pending_band_value:
            self._commit_pending_changes()
        band = bands[band_idx]
        macros = self._macros_by_channel[channel]
        # Macro trim folded into gain so the daemon's filter chain
        # reflects the slider tilt. Q / freq / type aren't affected.
        effective_gain = max(-24.0, min(24.0, float(band.get("gain", 0.0)) +
            _macro_trim_for_freq(float(band.get("freq", 1000.0)), macros)))
        self._daemon.send_command(
            "set-eq-band",
            channel=channel,
            band=band_idx + 1,
            params={
                "freq": float(band.get("freq", 1000.0)),
                "q": float(band.get("q", 1.0)),
                "gain": effective_gain,
                "type": str(band.get("type", "peaking")),
                "enabled": bool(band.get("enabled", True)),
            },
        )
        self._persist_active_mods(channel)
        self._update_action_buttons()

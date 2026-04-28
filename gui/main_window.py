"""Main SteelVoiceMix window — status dashboard, settings, and tray integration."""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .about import make_about_dialog
from .daemon_client import DaemonClient, DaemonSignals
from .overlay import DialOverlay
from .settings import (
    APP_NAME,
    DISPLAY_NAME,
    OVERLAY_ORIENTATIONS,
    OVERLAY_POSITIONS,
    delete_profile,
    list_profiles,
    load as load_settings,
    load_profile,
    normalize_orientation,
    normalize_position,
    save as save_settings,
    save_profile,
)
from .update_checker import UpdateChecker

APP_ICON = "steelvoicemix"
APP_ICON_FALLBACK = "audio-headset"

log = logging.getLogger(__name__)


# Global stylesheet — gives the window a more cohesive look without
# overriding the user's system theme too aggressively. Most of these
# rules just tighten spacing, give buttons consistent padding, and
# soften borders. The progress bars keep their explicit per-bar styles
# (chunk colours) — those override these defaults where needed.
_GLOBAL_QSS = """
QMainWindow {
    background-color: palette(window);
}
QTabWidget::pane {
    border: 1px solid palette(mid);
    border-radius: 6px;
    background: palette(base);
    top: -1px;
}
QTabBar::tab {
    background: palette(window);
    border: 1px solid palette(mid);
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 14px;
    min-width: 60px;
    color: palette(text);
}
QTabBar::tab:selected {
    background: palette(base);
    font-weight: bold;
}
QTabBar::tab:!selected:hover {
    background: palette(midlight);
}
QPushButton {
    padding: 5px 12px;
    border-radius: 4px;
    border: 1px solid palette(mid);
    background: palette(button);
    min-height: 22px;
}
QPushButton:hover {
    background: palette(midlight);
}
QPushButton:pressed {
    background: palette(mid);
}
QPushButton:disabled {
    color: palette(placeholder-text);
}
QPushButton:flat {
    border: none;
    background: transparent;
}
QComboBox {
    padding: 4px 8px;
    border: 1px solid palette(mid);
    border-radius: 4px;
    min-height: 22px;
}
QCheckBox {
    spacing: 8px;
}
QLabel#section-title {
    font-weight: bold;
    font-size: 11px;
    color: palette(placeholder-text);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
}
QFrame[divider="true"] {
    background: palette(mid);
    max-height: 1px;
    min-height: 1px;
    margin: 4px 0;
}
"""


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section-title")
    return label


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setProperty("divider", True)
    return line


# Canonical settings key → exact display string used in the position combo.
# Avoid using .replace("-", " ").title() to derive this — the items in the
# combo keep the dash, so a space-separated lookup never matches and the
# selected index doesn't update on profile load (or on startup if the
# user's saved position isn't the default).
_POSITION_DISPLAY: dict[str, str] = {
    "top-right": "Top-right",
    "top-left": "Top-left",
    "bottom-right": "Bottom-right",
    "bottom-left": "Bottom-left",
    "center": "Center",
}


# Sonar's parametric EQ exposes 10 bands. Matching that count means a Sonar
# preset's filter1..filter10 maps slot-for-slot into our state.
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


def _app_icon() -> QIcon:
    """Return our installed icon, falling back to the generic theme icon
    when running from a source checkout that hasn't been installed yet."""
    return QIcon.fromTheme(APP_ICON, QIcon.fromTheme(APP_ICON_FALLBACK))


class MixerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(DISPLAY_NAME)
        self.setFixedSize(440, 560)
        self.setWindowIcon(_app_icon())
        self.setStyleSheet(_GLOBAL_QSS)

        self.signals = DaemonSignals()
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.chatmix_changed.connect(self._on_chatmix)
        self.signals.status_message.connect(self._on_status)
        self.signals.battery_updated.connect(self._on_battery)
        self.signals.media_sink_changed.connect(self._on_media_sink_changed)
        self.signals.hdmi_sink_changed.connect(self._on_hdmi_sink_changed)
        self.signals.auto_route_browsers_changed.connect(
            self._on_auto_route_browsers_changed
        )
        self.signals.eq_enabled_changed.connect(self._on_eq_enabled_changed)
        self.signals.eq_bands_changed.connect(self._on_eq_bands_changed)
        self.signals.eq_full_state.connect(self._on_eq_full_state)
        # Track the daemon's reported sink-toggle states so the buttons
        # render correctly. Daemon defaults are "off until the user opts in"
        # so we start with False; the first status event corrects them.
        self._media_sink_enabled = False
        self._hdmi_sink_enabled = False
        self._auto_route_browsers = False
        self._eq_enabled = False
        # Sonar-style per-channel EQ: separate band arrays for Game and
        # Chat. Each band carries its full {freq, q, gain, type, enabled}
        # — sliders bind to `gain`, labels read `freq` and derive a
        # musical name from it. The sliders display whichever channel is
        # currently selected via the channel combo box; switching the
        # combo re-renders sliders + labels from that channel's bands.
        # Defaults match the Rust daemon's default_channel_bands() so
        # the GUI shows a sane shape before the first status snapshot.
        self._eq_bands_by_channel: dict[str, list[dict]] = {
            "game": _default_channel_bands(),
            "chat": _default_channel_bands(),
        }
        self._eq_current_channel: str = "game"
        # Slider + label widgets, populated in _build_eq_tab. Cached so
        # channel-switch and daemon-broadcast handlers can update them
        # without triggering signal storms. Name + freq labels are kept
        # so preset loads (which can change frequencies) refresh them.
        self.eq_band_sliders: list[QSlider] = []
        self.eq_band_value_labels: list[QLabel] = []
        self.eq_band_name_labels: list[QLabel] = []
        self.eq_band_freq_labels: list[QLabel] = []

        # EQ slider commits are debounced. While the user drags, we just
        # update the visible label — sending a daemon command per pixel
        # of slider travel queues hundreds of chain respawns and stalls
        # the GUI for minutes. _eq_pending_band_value collects the most
        # recent value per band; the timer fires 250 ms after the last
        # change and flushes everything to the daemon in one shot.
        self._eq_pending_band_value: dict[int, int] = {}
        from PySide6.QtCore import QTimer as _QTimer
        self._eq_commit_timer = _QTimer(self)
        self._eq_commit_timer.setSingleShot(True)
        self._eq_commit_timer.setInterval(250)
        self._eq_commit_timer.timeout.connect(self._commit_pending_eq_changes)

        self.settings = load_settings()
        self.overlay = DialOverlay()
        self.overlay.set_orientation(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            )
        )

        # Cross-DE: some sessions (GNOME without extensions, minimal WMs)
        # have no status-notifier. Detect that once and skip hide-to-tray.
        self.has_tray = QSystemTrayIcon.isSystemTrayAvailable()
        if not self.has_tray:
            log.warning(
                "System tray not available — closing the window will quit "
                "instead of hiding to tray."
            )

        # Detect Wayland so we can warn about overlay stacking order when
        # the user has forced QT_QPA_PLATFORM=wayland (the launcher defaults
        # to xcb, but the override env var is respected for advanced users).
        self._wayland = (
            os.environ.get("XDG_SESSION_TYPE") == "wayland"
            and os.environ.get("QT_QPA_PLATFORM", "").startswith("wayland")
        )

        self._build_ui()
        if self.has_tray:
            self._build_tray()
        self._start_daemon_client()
        self._update_checker = None
        self._start_update_check()

    # ---------------------------------------------------------------- layout

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # Persistent header — connection status always visible above the tabs.
        self.status_label = QLabel("🔍 Connecting to daemon...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold; padding: 4px;")
        root.addWidget(self.status_label)

        tabs = QTabWidget()
        tabs.addTab(self._build_home_tab(), "Home")
        tabs.addTab(self._build_sinks_tab(), "Sinks")
        tabs.addTab(self._build_eq_tab(), "Sonar")
        tabs.addTab(self._build_settings_tab(), "Settings")
        root.addWidget(tabs, 1)

        # Persistent footer — update status + check-now + about, always visible.
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.update_label = QLabel("Up to date")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")
        update_btn = QPushButton("Check for updates")
        update_btn.setFlat(True)
        update_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        update_btn.clicked.connect(self._force_update_check)
        self.about_btn = QPushButton("About…")
        self.about_btn.setFlat(True)
        self.about_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        self.about_btn.clicked.connect(self._show_about)
        footer.addWidget(self.update_label, 1)
        footer.addWidget(update_btn)
        footer.addWidget(self.about_btn)
        root.addLayout(footer)

        if self._wayland:
            # Launcher forces xcb; this only fires if the user has overridden
            # that, usually knowingly. Keep the hint terse.
            hint = QLabel(
                "⚠ Wayland session detected. The overlay may appear below "
                "fullscreen windows. Unset QT_QPA_PLATFORM or re-run the "
                "installer to restore XCB."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #FF9800; font-size: 10px;")
            root.addWidget(hint)

    # ---------------------------------------------------------------- tabs

    def _build_home_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title("ChatMix"))

        game_row = QHBoxLayout()
        game_label = QLabel("🎮 Game")
        game_label.setFixedWidth(70)
        self.game_bar = self._make_bar("#4CAF50")
        game_row.addWidget(game_label)
        game_row.addWidget(self.game_bar)
        layout.addLayout(game_row)

        chat_row = QHBoxLayout()
        chat_label = QLabel("💬 Chat")
        chat_label.setFixedWidth(70)
        self.chat_bar = self._make_bar("#2196F3")
        chat_row.addWidget(chat_label)
        chat_row.addWidget(self.chat_bar)
        layout.addLayout(chat_row)

        self.dial_label = QLabel("⚖️ Balanced")
        self.dial_label.setAlignment(Qt.AlignCenter)
        self.dial_label.setStyleSheet("font-size: 11px; color: palette(placeholder-text);")
        layout.addWidget(self.dial_label)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Headset"))

        battery_row = QHBoxLayout()
        self.battery_label = QLabel("🔋 Battery")
        self.battery_label.setFixedWidth(90)
        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setValue(0)
        self.battery_bar.setTextVisible(True)
        self.battery_bar.setFormat("—")
        self.battery_bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; height: 22px; }"
            "QProgressBar::chunk { background: #FF9800; border-radius: 3px; }"
        )
        battery_row.addWidget(self.battery_label)
        battery_row.addWidget(self.battery_bar)
        layout.addLayout(battery_row)

        layout.addStretch(1)
        return page

    def _build_sinks_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title("Virtual Sinks"))

        media_row = QHBoxLayout()
        media_lbl = QLabel("Media")
        media_lbl.setFixedWidth(70)
        self.media_btn = QPushButton("Add Media")
        self.media_btn.clicked.connect(self._toggle_media_sink)
        media_row.addWidget(media_lbl)
        media_row.addWidget(self.media_btn, 1)
        layout.addLayout(media_row)

        hdmi_row = QHBoxLayout()
        hdmi_lbl = QLabel("HDMI")
        hdmi_lbl.setFixedWidth(70)
        self.hdmi_btn = QPushButton("Add HDMI")
        self.hdmi_btn.clicked.connect(self._toggle_hdmi_sink)
        hdmi_row.addWidget(hdmi_lbl)
        hdmi_row.addWidget(self.hdmi_btn, 1)
        layout.addLayout(hdmi_row)

        sinks_help = QLabel(
            "Media and HDMI sinks bypass the ChatMix dial — useful for "
            "music, browsers, or routing audio to a TV/AVR independently "
            "of the headset."
        )
        sinks_help.setStyleSheet("font-size: 10px; color: palette(placeholder-text); padding-top: 4px;")
        sinks_help.setWordWrap(True)
        layout.addWidget(sinks_help)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Auto-Routing"))

        self.auto_route_check = QCheckBox(
            "Route browsers and media players to SteelMedia automatically"
        )
        self.auto_route_check.setToolTip(
            "When enabled, the daemon moves new browser and media-player "
            "audio streams (Firefox, Chromium, mpv, VLC…) to the SteelMedia "
            "sink so they bypass the ChatMix dial. Manual moves stick — "
            "the daemon only acts on first-seen streams."
        )
        self.auto_route_check.toggled.connect(self._toggle_auto_route_browsers)
        layout.addWidget(self.auto_route_check)

        layout.addStretch(1)
        return page

    def _build_eq_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title(f"Sonar — {NUM_EQ_BANDS}-band parametric EQ"))

        self.eq_check = QCheckBox("Enable Sonar EQ (🎮 Game + 💬 Chat)")
        self.eq_check.setToolTip(
            "Inserts a PipeWire filter chain between the SteelGame and "
            "SteelChat sinks and the headset. The user-facing sinks stay "
            "put across toggles, so Discord and other apps don't lose "
            "their connection."
        )
        self.eq_check.toggled.connect(self._toggle_eq_enabled)
        layout.addWidget(self.eq_check)

        # Sonar-style per-channel selector: tune [Game] and [Chat]
        # independently. Sliders display the selected channel's gains;
        # switching the combo loads that channel's stored values. Emoji
        # icons match the Home-tab convention (🎮 / 💬).
        ch_row = QHBoxLayout()
        ch_row.addWidget(QLabel("Channel:"))
        self.eq_channel_combo = QComboBox()
        self.eq_channel_combo.addItems(["🎮 Game", "💬 Chat"])
        self.eq_channel_combo.setMinimumWidth(140)
        self.eq_channel_combo.currentTextChanged.connect(
            self._on_eq_channel_changed
        )
        ch_row.addWidget(self.eq_channel_combo, 1)
        layout.addLayout(ch_row)

        # 10 vertical sliders, one per band. The musical name + frequency
        # labels are populated dynamically from the current channel's
        # band data — preset loads can move bands around without us
        # having to relabel manually. Slimmer columns than the prior 6-
        # band layout to keep all 10 visible without horizontal scroll.
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
            self.eq_band_value_labels.append(value_lbl)
            band_col.addWidget(value_lbl)

            slider = QSlider(Qt.Vertical)
            # Slider unit = 0.1 dB. Range: -120 to 120 → -12.0 to +12.0 dB.
            slider.setRange(-120, 120)
            slider.setValue(0)
            slider.setTickPosition(QSlider.TicksRight)
            slider.setTickInterval(60)
            slider.setMinimumHeight(200)
            slider.setFixedWidth(28)
            band_num = idx + 1
            # valueChanged updates the visible label and queues a debounced
            # commit. sliderReleased commits immediately on drag end so the
            # user gets fast feedback when they let go (no 250 ms delay).
            slider.valueChanged.connect(
                lambda v, b=band_num, lbl=value_lbl: self._on_eq_slider_changed(b, v, lbl)
            )
            slider.sliderReleased.connect(
                lambda b=band_num, s=slider: self._on_eq_slider_released(b, s)
            )
            self.eq_band_sliders.append(slider)
            band_col.addWidget(slider, 0, alignment=Qt.AlignHCenter)

            name_lbl = QLabel("")
            name_lbl.setAlignment(Qt.AlignCenter)
            name_lbl.setStyleSheet("font-size: 9px; font-weight: bold;")
            name_lbl.setWordWrap(True)
            self.eq_band_name_labels.append(name_lbl)
            band_col.addWidget(name_lbl)

            freq_lbl = QLabel("")
            freq_lbl.setAlignment(Qt.AlignCenter)
            freq_lbl.setStyleSheet(
                "font-size: 9px; color: palette(placeholder-text);"
            )
            self.eq_band_freq_labels.append(freq_lbl)
            band_col.addWidget(freq_lbl)

            bands_row.addLayout(band_col)
        layout.addLayout(bands_row)
        # Render initial labels from the default band shape so the EQ tab
        # has populated frequency / name labels even before the first
        # daemon status arrives.
        self._render_sliders_for_channel(self._eq_current_channel)

        eq_help = QLabel(
            "Drag a slider to boost or cut a frequency band by up to "
            "±12 dB. Each release respawns the filter chain with the new "
            "gains (~100 ms audio glitch per change). Live param updates "
            "without respawn are planned for a follow-up."
        )
        eq_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-top: 4px;"
        )
        eq_help.setWordWrap(True)
        layout.addWidget(eq_help)

        layout.addStretch(1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(_section_title("Overlay"))

        self.overlay_check = QCheckBox("Show overlay when dial is turned")
        self.overlay_check.setChecked(self.settings.get("overlay", True))
        self.overlay_check.toggled.connect(self._toggle_overlay)
        layout.addWidget(self.overlay_check)

        position_row = QHBoxLayout()
        pos_lbl = QLabel("Position")
        pos_lbl.setFixedWidth(70)
        self.position_combo = QComboBox()
        self.position_combo.addItems(list(_POSITION_DISPLAY.values()))
        current_pos = normalize_position(self.settings.get("overlay_position", "top-right"))
        idx = self.position_combo.findText(_POSITION_DISPLAY[current_pos])
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        self.position_combo.currentTextChanged.connect(self._change_position)
        position_row.addWidget(pos_lbl)
        position_row.addWidget(self.position_combo, 1)
        layout.addLayout(position_row)

        orient_row = QHBoxLayout()
        ori_lbl = QLabel("Style")
        ori_lbl.setFixedWidth(70)
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Horizontal", "Vertical"])
        idx = self.orient_combo.findText(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
        if idx >= 0:
            self.orient_combo.setCurrentIndex(idx)
        self.orient_combo.currentTextChanged.connect(self._change_orientation)
        orient_row.addWidget(ori_lbl)
        orient_row.addWidget(self.orient_combo, 1)
        layout.addLayout(orient_row)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Startup"))

        self.autostart_check = QCheckBox("Start with system")
        self.autostart_check.setChecked(self.settings.get("autostart", True))
        self.autostart_check.toggled.connect(self._toggle_autostart)
        layout.addWidget(self.autostart_check)

        layout.addWidget(_divider())
        layout.addWidget(_section_title("Audio Profiles"))

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Saved:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(140)
        self._refresh_profile_combo()
        profile_row.addWidget(self.profile_combo, 1)
        layout.addLayout(profile_row)

        profile_btns = QHBoxLayout()
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load_selected_profile)
        save_btn = QPushButton("Save…")
        save_btn.clicked.connect(self._save_new_profile)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_selected_profile)
        profile_btns.addWidget(load_btn)
        profile_btns.addWidget(save_btn)
        profile_btns.addWidget(del_btn)
        layout.addLayout(profile_btns)

        profile_help = QLabel(
            "A profile snapshots overlay options + Media/HDMI sink toggles.\n"
            "Save the current setup, switch quickly, restore in one click."
        )
        profile_help.setStyleSheet("font-size: 10px; color: palette(placeholder-text); padding-top: 4px;")
        profile_help.setWordWrap(True)
        layout.addWidget(profile_help)

        layout.addStretch(1)
        return page

    def _make_bar(self, chunk_color: str) -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(100)
        bar.setTextVisible(True)
        bar.setFormat("%v%")
        bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; "
            "height: 22px; text-align: center; }"
            f"QProgressBar::chunk {{ background: {chunk_color}; border-radius: 3px; }}"
        )
        return bar

    def _build_tray(self):
        self.tray = QSystemTrayIcon(_app_icon(), self)
        self.tray.setToolTip(DISPLAY_NAME)

        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    # -------------------------------------------------------- event handlers

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show_window()

    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if not self.has_tray:
            event.accept()
            self._quit()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            DISPLAY_NAME,
            "Minimized to tray",
            QSystemTrayIcon.Information,
            2000,
        )

    def _quit(self):
        self.daemon_client.stop()
        QApplication.quit()

    def _start_daemon_client(self):
        self.daemon_client = DaemonClient(self.signals)
        self.daemon_thread = threading.Thread(
            target=self.daemon_client.run, daemon=True
        )
        self.daemon_thread.start()

    def _on_connected(self):
        self.status_label.setText("🟢 Connected — ChatMix Active")
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #4CAF50;"
        )

    def _on_disconnected(self):
        self.status_label.setText("🔴 Disconnected — Reconnecting...")
        self.status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #f44336;"
        )
        self.game_bar.setValue(0)
        self.chat_bar.setValue(0)
        self.dial_label.setText("⚖️ —")

    def _on_chatmix(self, game_vol, chat_vol):
        self.game_bar.setValue(game_vol)
        self.chat_bar.setValue(chat_vol)

        diff = game_vol - chat_vol
        if abs(diff) < 10:
            label = "⚖️ Balanced"
        elif diff > 0:
            label = f"🎮 Game +{diff}"
        else:
            label = f"💬 Chat +{-diff}"
        self.dial_label.setText(label)

        if self.settings.get("overlay", True):
            pos = normalize_position(
                self.settings.get("overlay_position", "top-right")
            )
            self.overlay.show_volumes(game_vol, chat_vol, pos)

    def _on_battery(self, level, status):
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

        self.battery_bar.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; "
            "height: 22px; text-align: center; }"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}"
        )
        if self.has_tray:
            self.tray.setToolTip(f"{DISPLAY_NAME} — 🔋 {level}% ({status})")

    def _toggle_overlay(self, checked):
        self.settings["overlay"] = checked
        save_settings(self.settings)

    def _change_position(self, text):
        key = text.lower().replace(" ", "-")
        if key not in OVERLAY_POSITIONS:
            return
        self.settings["overlay_position"] = key
        save_settings(self.settings)
        self.overlay.show_volumes(self.game_bar.value(), self.chat_bar.value(), key)

    def _change_orientation(self, text):
        key = text.lower()
        if key not in OVERLAY_ORIENTATIONS:
            return
        self.settings["overlay_orientation"] = key
        save_settings(self.settings)
        self.overlay.set_orientation(key)
        self.overlay.show_volumes(
            self.game_bar.value(),
            self.chat_bar.value(),
            normalize_position(self.settings.get("overlay_position", "top-right")),
        )

    def _toggle_autostart(self, checked):
        self.settings["autostart"] = checked
        save_settings(self.settings)
        verb = "enable" if checked else "disable"
        # Best-effort: the setting is always persisted above; systemd-less
        # environments simply won't toggle autostart and that's fine.
        for unit in (APP_NAME, f"{APP_NAME}-gui"):
            try:
                subprocess.run(
                    ["systemctl", "--user", verb, unit],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass

    def _on_status(self, msg):
        self.status_label.setText(msg)

    def _show_about(self):
        dialog = make_about_dialog(self)
        dialog.exec()

    def _on_media_sink_changed(self, enabled: bool):
        self._media_sink_enabled = enabled
        self.media_btn.setText("Remove Media" if enabled else "Add Media")
        self.media_btn.setToolTip(
            "Destroy the SteelMedia virtual sink"
            if enabled
            else "Create a SteelMedia virtual sink that bypasses the ChatMix dial"
        )

    def _toggle_media_sink(self):
        cmd = "remove-media-sink" if self._media_sink_enabled else "add-media-sink"
        self.daemon_client.send_command(cmd)
        # Disable the button until the daemon confirms the change so quick
        # double-clicks don't queue conflicting commands.
        self.media_btn.setEnabled(False)
        self._media_btn_reenable_timer()

    def _media_btn_reenable_timer(self):
        from PySide6.QtCore import QTimer

        def reenable():
            self.media_btn.setEnabled(True)

        QTimer.singleShot(600, reenable)

    def _on_hdmi_sink_changed(self, enabled: bool):
        self._hdmi_sink_enabled = enabled
        self.hdmi_btn.setText("Remove HDMI" if enabled else "Add HDMI")
        self.hdmi_btn.setToolTip(
            "Destroy the SteelHDMI virtual sink"
            if enabled
            else "Create a SteelHDMI virtual sink that loops to your HDMI output"
        )

    def _toggle_hdmi_sink(self):
        cmd = "remove-hdmi-sink" if self._hdmi_sink_enabled else "add-hdmi-sink"
        self.daemon_client.send_command(cmd)
        self.hdmi_btn.setEnabled(False)
        self._hdmi_btn_reenable_timer()

    def _hdmi_btn_reenable_timer(self):
        from PySide6.QtCore import QTimer

        def reenable():
            self.hdmi_btn.setEnabled(True)

        QTimer.singleShot(600, reenable)

    def _on_auto_route_browsers_changed(self, enabled: bool):
        self._auto_route_browsers = enabled
        # Block the toggled signal so this echo doesn't re-send to the daemon.
        was_blocked = self.auto_route_check.blockSignals(True)
        self.auto_route_check.setChecked(enabled)
        self.auto_route_check.blockSignals(was_blocked)

    def _toggle_auto_route_browsers(self, checked: bool):
        self.daemon_client.send_command(
            "set-auto-route-browsers", enabled=bool(checked)
        )

    def _on_eq_enabled_changed(self, enabled: bool):
        self._eq_enabled = enabled
        was_blocked = self.eq_check.blockSignals(True)
        self.eq_check.setChecked(enabled)
        self.eq_check.blockSignals(was_blocked)

    def _toggle_eq_enabled(self, checked: bool):
        self.daemon_client.send_command("set-eq-enabled", enabled=bool(checked))

    def _on_eq_slider_changed(self, band: int, value_tenths: int, label: QLabel):
        """User moved a slider. Update the live value label, store the
        new value in the current channel's bands array, and queue a
        debounced daemon commit. While the user is actively dragging,
        no commands go to the daemon — that's what was producing
        minute-long lag (one chain respawn queued per pixel of travel).
        The 250 ms timer collapses rapid changes into a single command
        per band per pause."""
        gain_db = value_tenths / 10.0
        sign = "+" if gain_db > 0 else ""
        label.setText(f"{sign}{gain_db:.1f}")
        # Update the LOCAL view of the current channel's bands so a
        # channel switch + switch-back doesn't lose the in-progress edit.
        bands = self._eq_bands_by_channel[self._eq_current_channel]
        if 1 <= band <= len(bands):
            bands[band - 1]["gain"] = gain_db
        self._eq_pending_band_value[band] = value_tenths
        self._eq_commit_timer.start()

    def _on_eq_slider_released(self, band: int, slider: QSlider):
        """Slider released — commit *now* without waiting for the debounce."""
        self._eq_pending_band_value[band] = slider.value()
        self._eq_commit_timer.stop()
        self._commit_pending_eq_changes()

    def _commit_pending_eq_changes(self):
        """Flush queued band-gain changes to the daemon for the currently
        selected channel."""
        channel = self._eq_current_channel
        for band, value_tenths in self._eq_pending_band_value.items():
            gain_db = value_tenths / 10.0
            self.daemon_client.send_command(
                "set-eq-band-gain",
                channel=channel,
                band=band,
                gain_db=gain_db,
            )
        self._eq_pending_band_value.clear()

    def _on_eq_channel_changed(self, text: str):
        """Combo box changed — load the selected channel's stored bands
        into the sliders. The combo items carry emoji prefixes
        ('🎮 Game' / '💬 Chat') for visual continuity with the Home tab,
        so we extract the trailing word to map back to the daemon's
        channel keys ('game' / 'chat')."""
        last_word = text.strip().split()[-1].lower() if text.strip() else ""
        if last_word not in self._eq_bands_by_channel:
            return
        self._eq_current_channel = last_word
        # Cancel any pending commit from the previous channel.
        self._eq_commit_timer.stop()
        self._eq_pending_band_value.clear()
        self._render_sliders_for_channel(last_word)

    def _render_sliders_for_channel(self, channel: str):
        """Push the stored bands for `channel` into the slider widgets
        AND refresh the per-band name + frequency labels. Preset loads
        change frequencies, so the labels can't be static."""
        bands = self._eq_bands_by_channel.get(channel) or _default_channel_bands()
        for idx in range(len(self.eq_band_sliders)):
            band = bands[idx] if idx < len(bands) else _default_eq_band(idx)
            gain_db = float(band.get("gain", 0.0))
            freq = float(band.get("freq", 1000.0))

            slider = self.eq_band_sliders[idx]
            value_lbl = self.eq_band_value_labels[idx]
            name_lbl = self.eq_band_name_labels[idx]
            freq_lbl = self.eq_band_freq_labels[idx]

            value_tenths = int(round(gain_db * 10))
            was_blocked = slider.blockSignals(True)
            slider.setValue(value_tenths)
            slider.blockSignals(was_blocked)
            sign = "+" if gain_db > 0 else ""
            value_lbl.setText(f"{sign}{gain_db:.1f}")
            name_lbl.setText(_band_name_for(freq))
            freq_lbl.setText(_format_freq(freq))

    def _on_eq_bands_changed(self, channel: str, bands: list):
        """Daemon broadcast: the bands for `channel` changed (perhaps
        because we just sent the change, perhaps from another client or
        a preset load). Always update the local cache; if it's the
        channel currently on screen, refresh sliders + labels too."""
        if channel not in self._eq_bands_by_channel:
            return
        self._eq_bands_by_channel[channel] = list(bands)
        if channel == self._eq_current_channel:
            self._render_sliders_for_channel(channel)

    def _on_eq_full_state(self, state: dict):
        """Initial Status snapshot delivered both channels' band data at
        once (Game and Chat). Cache both and refresh the visible sliders
        for whichever channel is currently selected."""
        for ch in ("game", "chat"):
            if ch in state:
                self._eq_bands_by_channel[ch] = list(state[ch])
        self._render_sliders_for_channel(self._eq_current_channel)

    # -------------------------------------------------------------- profiles

    def _refresh_profile_combo(self):
        names = list_profiles(self.settings)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
        else:
            self.profile_combo.addItem("(no saved profiles)")
            self.profile_combo.setEnabled(False)
            return
        self.profile_combo.setEnabled(True)

    def _save_new_profile(self):
        name, ok = QInputDialog.getText(
            self, "Save profile", "Profile name:"
        )
        if not ok or not name.strip():
            return
        try:
            save_profile(
                self.settings,
                name.strip(),
                media_enabled=self._media_sink_enabled,
                hdmi_enabled=self._hdmi_sink_enabled,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid profile name", str(e))
            return
        self._refresh_profile_combo()
        idx = self.profile_combo.findText(name.strip())
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)

    def _load_selected_profile(self):
        name = self.profile_combo.currentText()
        if not name or name.startswith("("):
            return
        profile = load_profile(self.settings, name)
        if profile is None:
            return
        # Re-render the GUI controls from the (now updated) settings dict.
        self.overlay_check.setChecked(self.settings.get("overlay", True))
        current_pos = normalize_position(self.settings.get("overlay_position", "top-right"))
        idx = self.position_combo.findText(_POSITION_DISPLAY[current_pos])
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)
        idx = self.orient_combo.findText(
            normalize_orientation(
                self.settings.get("overlay_orientation", "horizontal")
            ).capitalize()
        )
        if idx >= 0:
            self.orient_combo.setCurrentIndex(idx)
        self.overlay.set_orientation(
            normalize_orientation(self.settings.get("overlay_orientation", "horizontal"))
        )

        # Apply daemon-side sink toggles via the existing socket commands so
        # the daemon's persisted state stays consistent with the profile.
        sinks = profile.get("sinks", {}) if isinstance(profile, dict) else {}
        want_media = bool(sinks.get("media", False))
        want_hdmi = bool(sinks.get("hdmi", False))
        if want_media != self._media_sink_enabled:
            self.daemon_client.send_command(
                "add-media-sink" if want_media else "remove-media-sink"
            )
        if want_hdmi != self._hdmi_sink_enabled:
            self.daemon_client.send_command(
                "add-hdmi-sink" if want_hdmi else "remove-hdmi-sink"
            )

    def _delete_selected_profile(self):
        name = self.profile_combo.currentText()
        if not name or name.startswith("("):
            return
        ok = QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        delete_profile(self.settings, name)
        self._refresh_profile_combo()

    # -------------------------------------------------------- update checker

    def _start_update_check(self):
        """Spawn the background update check on first show."""
        if getattr(self, "_update_checker", None) is not None:
            return
        self._update_checker = UpdateChecker(self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.no_update.connect(self._on_no_update)
        self._update_checker.no_release_found.connect(self._on_no_release_found)
        self._update_checker.failed.connect(self._on_update_failed)
        self.update_label.setText("Checking for updates…")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")
        self._update_checker.start()

    def _force_update_check(self):
        """Forced re-check from the user-visible button."""
        self._update_checker = None
        self.update_label.setText("Checking…")
        self._start_update_check()

    def _on_update_available(self, latest_tag: str, current_version: str):
        self.update_label.setText(
            f"Update available: {latest_tag} (you have {current_version})"
        )
        self.update_label.setStyleSheet("color: #FF9800; font-size: 10px; font-weight: bold;")

    def _on_no_update(self):
        self.update_label.setText("Up to date")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")

    def _on_no_release_found(self):
        # Reachable upstream but no version tag found — typical for repos
        # that haven't cut a release yet, or for forks. Different from
        # offline; don't blame the network.
        self.update_label.setText("No published release found")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")

    def _on_update_failed(self):
        self.update_label.setText("Update check failed (offline?)")
        self.update_label.setStyleSheet("color: palette(placeholder-text); font-size: 10px;")

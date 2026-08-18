"""Surround tab — plain-language on/off, sound profile, and an
interactive stage editor for per-channel levels.

Honesty (see STATUS.md): the stage shows every channel at its FIXED
direction (set by the HRIR sound profile). Dragging a speaker changes
its LEVEL only, never its direction; azimuth/elevation aren't ours to
change with a static convolver. Per-channel level maps to a real gain
node in the surround chain, committed on release (one respawn per
adjustment, like the EQ tab).
"""

from __future__ import annotations

import logging

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..hrir_default import bundled_default_path, has_default
from ..surround_model import (
    GAIN_MAX_DB,
    GAIN_MIN_DB,
    PRESET_LABELS,
    apply_preset,
    clamp_db,
    default_channels,
    deserialize,
    serialize,
)
from ..surround_stage import SurroundStage
from ..widgets import (
    ACCENT,
    NoWheelComboBox,
    card,
    collapsible_help,
    help_text,
    labelled_toggle,
    notice,
    section_title,
    themed_icon,
)

# Settings key holding per-device level profiles:
# { "<device-variant>": { "<channel>": {gain_db, muted} } }
log = logging.getLogger(__name__)

_PROFILES_KEY = "surround_channel_profiles"


class SurroundTab(QWidget):
    def __init__(self, daemon_client, settings: dict | None = None, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        self._settings = settings if settings is not None else {}
        self._enabled: bool = False
        self._hrir_path: str = ""
        self._device_variant: str = "wireless"
        self._undo_stack: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # --- What it does + on/off ---
        intro = help_text(
            self.tr(
                "Makes 5.1 and 7.1 games and movies sound like they come "
                "from around you, on ordinary headphones. Stereo music and "
                "calls are left unchanged."
            )
        )
        tech_row, tech_body = collapsible_help(
            self.tr(
                "Technically: apps see a SteelSurround 7.1 output; PipeWire "
                "convolves each channel with an HRIR (head-related impulse "
                "response) file and mixes the result to binaural stereo. "
                "The direction of each channel is fixed by that file — the "
                "stage below adjusts each channel's level, not its "
                "direction. HeSuVi-format WAVs work; personalise with "
                "Impulcifer for tighter positioning."
            ),
            label=self.tr("Virtual Surround"),
        )
        toggle_row, self.enable_toggle = labelled_toggle(
            self.tr("Turn on virtual surround"),
        )
        self.enable_toggle.setEnabled(False)
        self.enable_toggle.toggled.connect(self._on_toggled)
        self.status_label = help_text(self.tr("Choose a sound profile below first."))
        layout.addWidget(card(
            None, tech_row, tech_body, intro, toggle_row, self.status_label,
        ))

        self.setup_intro = notice(self.tr(
            "Set up surround in three short steps: 1) choose a sound profile, "
            "2) turn on virtual surround, 3) send a 5.1 or 7.1 app to "
            "SteelSurround. Speaker directions come from the profile and stay fixed."
        ))
        layout.addWidget(self.setup_intro)

        # --- Sound profile ---
        profile_help = help_text(
            self.tr(
                "Surround needs a sound profile that sets how audio reaches "
                "your ears — and fixes each channel's direction. Use the "
                "built-in one to get started, or load your own."
            )
        )
        path_row = QHBoxLayout()
        path_label = QLabel(self.tr("Selected profile"))
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(self.tr("No profile selected"))
        self.path_edit.setMinimumWidth(200)
        path_label.setBuddy(self.path_edit)
        path_row.addWidget(path_label)
        path_row.addWidget(self.path_edit, 1)
        self.default_btn = QPushButton(self.tr("Use built-in profile"))
        self.default_btn.setProperty("cta", True)
        self.default_btn.clicked.connect(self._on_use_default)
        path_row.addWidget(self.default_btn)
        self.browse_btn = QPushButton(self.tr("Choose file…"))
        self.browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(self.browse_btn)
        self.clear_btn = QPushButton(self.tr("Remove"))
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setEnabled(False)
        path_row.addWidget(self.clear_btn)
        layout.addWidget(card(self.tr("Sound profile"), profile_help, path_row))

        # --- Stage editor ---
        self._stage_card = self._build_stage_card()
        layout.addWidget(self._stage_card)
        self._stage_card.setVisible(False)

        layout.addStretch(1)

        self._load_profile_for_device()

    # ----------------------------------------------------------- stage UI

    def _build_stage_card(self) -> QWidget:
        self.stage = SurroundStage()
        self.stage.gain_changing.connect(self._on_gain_changing)
        self.stage.gain_committed.connect(self._on_gain_committed)
        self.stage.selection_changed.connect(self._on_selection_changed)
        # Undo must snapshot BEFORE the drag/keys mutate the level, so
        # the stage tells us when an adjustment begins.
        self.stage.drag_started.connect(lambda _key: self._push_undo())
        # Keyboard M / S / T forward to the same handlers as the buttons.
        self.stage.mute_requested.connect(
            lambda _k: self.mute_btn.toggle()
        )
        self.stage.solo_requested.connect(
            lambda _k: self.solo_btn.toggle()
        )
        # Use the key the stage sends rather than re-reading the
        # selection: they agree today, but a stage that ever fires this
        # for a speaker other than the selected one should test THAT
        # speaker, not silently test a different one.
        self.stage.test_requested.connect(self._on_test)

        # Right-side control panel for the selected speaker.
        panel = QVBoxLayout()
        panel.setSpacing(8)
        panel.addWidget(section_title(self.tr("Selected speaker")))
        self.sel_name = QLabel(self.tr("Center"))
        self.sel_name.setStyleSheet("font-size: 14px; font-weight: bold;")
        panel.addWidget(self.sel_name)

        level_row = QHBoxLayout()
        level_row.addWidget(QLabel(self.tr("Level")))
        self.level_spin = QDoubleSpinBox()
        self.level_spin.setRange(GAIN_MIN_DB, GAIN_MAX_DB)
        self.level_spin.setSingleStep(0.5)
        self.level_spin.setSuffix(self.tr(" dB"))
        self.level_spin.setDecimals(1)
        self.level_spin.valueChanged.connect(self._on_spin_changed)
        level_row.addWidget(self.level_spin, 1)
        panel.addLayout(level_row)

        btn_row = QHBoxLayout()
        self.mute_btn = QPushButton(self.tr("Mute"))
        self.mute_btn.setCheckable(True)
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        self.solo_btn = QPushButton(self.tr("Solo"))
        self.solo_btn.setCheckable(True)
        self.solo_btn.toggled.connect(self._on_solo_toggled)
        self.test_btn = QPushButton(self.tr("Test"))
        self.test_btn.setIcon(themed_icon("media-playback-start"))
        self.test_btn.clicked.connect(self._on_test)
        btn_row.addWidget(self.mute_btn)
        btn_row.addWidget(self.solo_btn)
        btn_row.addWidget(self.test_btn)
        panel.addLayout(btn_row)

        self.solo_btn.setToolTip(self.tr("Hear only this speaker"))
        self.solo_status = help_text("")
        self.solo_status.setStyleSheet(
            f"color: {ACCENT}; font-size: 11px; font-weight: bold;"
        )
        self.solo_status.setVisible(False)
        panel.addWidget(self.solo_status)

        # Shown only when the non-directional LFE (bass) channel is
        # selected, since it can't be dragged like the others.
        self.lfe_hint = help_text(
            self.tr("Bass has no direction — set its level with the box above.")
        )
        self.lfe_hint.setVisible(False)
        panel.addWidget(self.lfe_hint)

        panel.addWidget(help_text(
            self.tr(
                "Drag a speaker toward you to make it louder, away to make "
                "it quieter. Direction is set by the sound profile and "
                "can't be moved here."
            )
        ))
        panel.addStretch(1)

        # Presets + reset/undo.
        tools = QHBoxLayout()
        tools.addWidget(QLabel(self.tr("Preset")))
        self.preset_combo = NoWheelComboBox()
        # Labels live in a Qt-free model; translate at display time so
        # the model stays importable without Qt and the strings still
        # localise (Arabic).
        for key, label in PRESET_LABELS.items():
            self.preset_combo.addItem(self.tr(label), key)
        tools.addWidget(self.preset_combo, 1)
        self.apply_preset_btn = QPushButton(self.tr("Apply"))
        self.apply_preset_btn.clicked.connect(self._on_apply_preset)
        tools.addWidget(self.apply_preset_btn)
        self.undo_btn = QPushButton(self.tr("Undo"))
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._on_undo)
        tools.addWidget(self.undo_btn)
        self.reset_btn = QPushButton(self.tr("Reset levels"))
        self.reset_btn.clicked.connect(self._on_reset_levels)
        tools.addWidget(self.reset_btn)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.addWidget(self.stage, 0, 0)
        panel_w = QWidget(); panel_w.setLayout(panel)
        panel_w.setMinimumWidth(210)
        grid.addWidget(panel_w, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid_w = QWidget(); grid_w.setLayout(grid)

        # Shown when a profile is set but surround is off, so the user
        # knows their edits are saved but not yet audible.
        self.off_notice = notice(
            self.tr(
                "Virtual surround is off — your changes are saved, but turn "
                "it on above to hear them."
            )
        )
        self.off_notice.setVisible(False)

        c = card(self.tr("Speaker levels"), self.off_notice, grid_w, tools)
        self._sync_panel()
        return c

    # ----------------------------------------------------- daemon hooks

    def on_enabled_changed(self, enabled: bool) -> None:
        self._enabled = enabled
        was = self.enable_toggle.blockSignals(True)
        self.enable_toggle.setChecked(enabled)
        self.enable_toggle.blockSignals(was)
        self.off_notice.setVisible(bool(self._hrir_path) and not enabled)
        self._refresh_status_label()

    def on_hrir_changed(self, path: str) -> None:
        self._hrir_path = path
        self.path_edit.setText(path)
        self.clear_btn.setEnabled(bool(path))
        self.enable_toggle.setEnabled(bool(path))
        self._stage_card.setVisible(bool(path))
        self.setup_intro.setVisible(not bool(path))
        self.default_btn.setProperty("cta", not bool(path))
        self.default_btn.style().unpolish(self.default_btn)
        self.default_btn.style().polish(self.default_btn)
        if not path and self._enabled:
            self._enabled = False
            was = self.enable_toggle.blockSignals(True)
            self.enable_toggle.setChecked(False)
            self.enable_toggle.blockSignals(was)
        self._refresh_status_label()

    def on_device_variant_changed(self, variant: str) -> None:
        """Per-device level profiles: reload when the device changes so a
        wired and a wireless headset keep independent level trims."""
        if variant and variant != self._device_variant:
            self._device_variant = variant
            self._load_profile_for_device()
            self.push_profile_to_daemon()

    def on_service_connected(self) -> None:
        """The daemon (re)connected — replay the saved profile so the
        running chain matches the stage instead of starting flat."""
        self.push_profile_to_daemon()

    # --------------------------------------------------- profile handlers

    def _on_use_default(self) -> None:
        if not has_default():
            QMessageBox.warning(
                self,
                self.tr("Built-in profile missing"),
                self.tr(
                    "The bundled sound profile is missing — your install "
                    "may be incomplete. Load your own with Choose file."
                ),
            )
            return
        self._daemon.send_command(
            "set-surround-hrir", path=str(bundled_default_path())
        )

    def _on_browse(self) -> None:
        start_dir = os.path.expanduser("~/Downloads")
        if self._hrir_path:
            parent = os.path.dirname(self._hrir_path)
            if parent and os.path.isdir(parent):
                start_dir = parent
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Choose a sound profile (WAV)"), start_dir,
            "WAV files (*.wav);;All files (*)",
        )
        if path:
            self._daemon.send_command("set-surround-hrir", path=path)

    def _on_clear(self) -> None:
        self._daemon.send_command("set-surround-hrir", path=None)

    def _on_toggled(self, checked: bool) -> None:
        self._daemon.send_command("set-surround-enabled", enabled=bool(checked))

    # --------------------------------------------------- stage handlers

    def _on_selection_changed(self, key: str) -> None:
        self._sync_panel()

    def _on_gain_changing(self, key: str, db: float) -> None:
        # Live during drag: update the spinbox only, no daemon traffic.
        if key == self.stage.selected():
            self._sync_panel()

    def _on_gain_committed(self, key: str, db: float) -> None:
        # One daemon command + one save per adjustment. The undo snapshot
        # was already taken on drag_started (before the value moved), so
        # we do NOT snapshot here — doing so captured the post-change
        # state and made Undo a no-op for drags.
        self._daemon.send_command(
            "set-surround-channel-gain", channel=key, gain_db=round(db, 2)
        )
        self._save_profile()
        self._sync_panel()

    def _on_spin_changed(self, value: float) -> None:
        key = self.stage.selected()
        if not key:
            return
        ch = self.stage.channels().get(key)
        if ch and abs(ch.gain_db - value) > 1e-6:
            self._push_undo()
            self.stage.set_gain(key, value)
            self._daemon.send_command(
                "set-surround-channel-gain", channel=key,
                gain_db=round(clamp_db(value), 2),
            )
            self._save_profile()

    def _on_mute_toggled(self, checked: bool) -> None:
        key = self.stage.selected()
        if not key:
            return
        self._push_undo()
        self.stage.set_muted(key, checked)
        self._daemon.send_command(
            "set-surround-channel-mute", channel=key, muted=bool(checked)
        )
        self._save_profile()

    def _on_solo_toggled(self, checked: bool) -> None:
        key = self.stage.selected()
        self.stage.set_solo(key if checked else None)
        # Solo is a monitoring aid — tell the daemon which channel to
        # isolate (empty = none).
        self._daemon.send_command(
            "set-surround-solo", channel=key if checked else ""
        )
        self._refresh_solo_status()

    def _refresh_solo_status(self) -> None:
        solo = self.stage.solo()
        if solo:
            ch = self.stage.channels().get(solo)
            name = self.tr(ch.label) if ch else solo
            self.solo_status.setText(
                self.tr("Solo: {name} — everything else is silenced.").format(name=name)
            )
            self.solo_status.setVisible(True)
        else:
            self.solo_status.setVisible(False)

    def _on_test(self, key: str | None = None) -> None:
        # No argument → the toolbar button, which tests the selection.
        key = key or self.stage.selected()
        if not key:
            log.debug("surround: test ignored — no speaker selected")
            return
        if not self._enabled:
            # Nothing to hear yet — don't fire a beep out the default
            # sink; tell the user to turn surround on first.
            self.status_label.setText(
                self.tr("Turn on virtual surround to hear the test tone.")
            )
            return
        self.stage.flash_test(key)  # visual echo on the speaker
        self._daemon.send_command("surround-test-tone", channel=key)

    def _on_apply_preset(self) -> None:
        key = self.preset_combo.currentData()
        self._push_undo()
        apply_preset(self.stage.channels(), key)
        self.stage.update()
        self._push_all_gains()
        self._save_profile()
        self._sync_panel()

    def _on_reset_levels(self) -> None:
        self._push_undo()
        self.stage.set_channels(default_channels())
        self._clear_solo()
        self._push_all_gains()
        self._save_profile()
        self._sync_panel()

    def _on_undo(self) -> None:
        if not self._undo_stack:
            return
        snapshot = self._undo_stack.pop()
        self.stage.set_channels(deserialize(snapshot))
        self._clear_solo()
        self._push_all_gains()
        self._save_profile()
        self._sync_panel()
        self.undo_btn.setEnabled(bool(self._undo_stack))

    # --------------------------------------------------------- internals

    def _sync_panel(self) -> None:
        key = self.stage.selected()
        ch = self.stage.channels().get(key) if key else None
        has = ch is not None
        # LFE has no direction, but it IS a real channel with a level,
        # so its spinbox + mute stay enabled (it just can't be dragged).
        self.level_spin.setEnabled(has)
        self.mute_btn.setEnabled(has)
        self.solo_btn.setEnabled(has)
        self.test_btn.setEnabled(has)
        if not ch:
            self.sel_name.setText(self.tr("None"))
            return
        self.sel_name.setText(self.tr(ch.label))
        # Give the action buttons speaker context for screen readers.
        for btn, verb in (
            (self.mute_btn, self.tr("Mute")),
            (self.solo_btn, self.tr("Hear only")),
            (self.test_btn, self.tr("Test")),
        ):
            btn.setAccessibleName(f"{verb} {self.tr(ch.label)}")
        self.lfe_hint.setVisible(not ch.positional)
        was = self.level_spin.blockSignals(True)
        self.level_spin.setValue(ch.gain_db)
        self.level_spin.blockSignals(was)
        was = self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(ch.muted)
        self.mute_btn.blockSignals(was)
        was = self.solo_btn.blockSignals(True)
        self.solo_btn.setChecked(self.stage.solo() == key)
        self.solo_btn.blockSignals(was)

    def _push_all_gains(self) -> None:
        # ONE batched command for the whole channel set — gain AND mute
        # for every channel. Two reasons: (1) a preset/reset/undo that
        # un-mutes a channel must tell the daemon to un-mute it (sending
        # only muted=True left channels silently dead); (2) sending 16
        # separate commands respawned the surround chain up to 16 times.
        # The daemon applies all of these with a single respawn.
        channels = {
            key: {"gain_db": round(ch.gain_db, 2), "muted": bool(ch.muted)}
            for key, ch in self.stage.channels().items()
        }
        self._daemon.send_command("set-surround-gains", channels=channels)

    def _clear_solo(self) -> None:
        """Clear solo on both the widget and the daemon."""
        self.stage.set_solo(None)
        was = self.solo_btn.blockSignals(True)
        self.solo_btn.setChecked(False)
        self.solo_btn.blockSignals(was)
        self._daemon.send_command("set-surround-solo", channel="")
        self._refresh_solo_status()

    def push_profile_to_daemon(self) -> None:
        """Send the whole saved profile to the daemon. Called on connect
        and on device switch so the running chain matches what the stage
        shows — otherwise the daemon starts flat and the UI 'lies'."""
        self._daemon.send_command("set-surround-solo", channel="")
        self._push_all_gains()

    def _push_undo(self) -> None:
        self._undo_stack.append(serialize(self.stage.channels()))
        # Keep the stack bounded — this is casual undo, not history.
        del self._undo_stack[:-20]
        self.undo_btn.setEnabled(True)

    def _refresh_status_label(self) -> None:
        if not self._hrir_path:
            self.status_label.setText(self.tr("Choose a sound profile below first."))
            self.status_label.setStyleSheet("")
        elif self._enabled:
            self.status_label.setText(
                self.tr("On. Set an app's output to “SteelSurround” to hear it.")
            )
            self.status_label.setStyleSheet(
                f"color: {ACCENT}; font-size: 11px; font-weight: bold;"
            )
        else:
            self.status_label.setText(
                self.tr("Profile ready — flip the switch to turn it on.")
            )
            self.status_label.setStyleSheet("")

    # ----------------------------------------------------- persistence

    def _load_profile_for_device(self) -> None:
        profiles = self._settings.get(_PROFILES_KEY) or {}
        data = profiles.get(self._device_variant) if isinstance(profiles, dict) else None
        self.stage.set_channels(deserialize(data))
        self.stage.set_solo(None)
        self._refresh_solo_status()
        self._sync_panel()

    def _save_profile(self) -> None:
        from ..settings import save as save_settings
        profiles = self._settings.get(_PROFILES_KEY)
        if not isinstance(profiles, dict):
            profiles = {}
        profiles[self._device_variant] = serialize(self.stage.channels())
        self._settings[_PROFILES_KEY] = profiles
        try:
            save_settings(self._settings)
        except Exception as e:
            # A read-only config dir would otherwise silently lose the
            # user's levels between sessions.
            log.warning("Could not save surround profile: %s", e)

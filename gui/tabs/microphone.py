"""Microphone tab — wired to the daemon's mic-capture filter chain.

Three independently-toggleable features (Noise Gate, Noise Reduction,
AI Noise Cancellation), each with an Enabled toggle and a 0..100
strength slider. All three feed the same set-mic-* daemon commands;
the daemon reacts by spawning / re-spawning a single PipeWire
filter chain whose nodes reflect whichever combination is on.

Slider drags are debounced like the EQ tab's: while the user is
moving the slider we only update the visible label, then 250 ms
after the last change (or immediately on slider-released) we fire
one `set-mic-*` command. Without this, every pixel of slider travel
would respawn the LADSPA chain — same problem we hit on the EQ.

Daemon broadcasts mic-state-changed events whenever any feature
toggles or strength changes. We re-apply the snapshot to all three
toggles + sliders, blocking signals so the echo doesn't fire its
own command back at the daemon.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..settings import save as save_settings
from ..widgets import card, labelled_toggle

# Daemon command + MicState key names. The daemon's MicState fields
# are noise_gate / noise_reduction / ai_noise_cancellation; the
# matching commands are set-mic-noise-gate / set-mic-noise-reduction
# / set-mic-ai-nc.
_FEATURES = (
    ("noise_gate", "set-mic-noise-gate"),
    ("noise_reduction", "set-mic-noise-reduction"),
    ("ai_noise_cancellation", "set-mic-ai-nc"),
)

# Standard LADSPA install paths on Linux. We probe these in order;
# the LADSPA_PATH env var (if set) takes precedence. Both lib64 and
# lib variants are common — Fedora uses lib64, Debian uses lib.
_LADSPA_DIRS = (
    "/usr/lib64/ladspa",
    "/usr/lib/ladspa",
    "/usr/lib/x86_64-linux-gnu/ladspa",
    "/usr/local/lib/ladspa",
    "/usr/local/lib64/ladspa",
)

# Each feature's plugin file → install hint. Used by the
# availability check at startup to disable toggles whose plugin
# isn't installed.
#
# For ladspa-swh-plugins we can give a `dnf install` line because
# it's in main Fedora repos. For librnnoise_ladspa.so the source
# is werman/noise-suppression-for-voice on GitHub — not packaged
# in Fedora, so the hint points users at the project URL with a
# build-from-source pointer.
_INSTALL_DNF = "ladspa-swh-plugins"
_INSTALL_RNNOISE_HINT = (
    "Not in Fedora repos. Build from "
    "https://github.com/werman/noise-suppression-for-voice "
    "or grab a COPR that ships librnnoise_ladspa.so."
)
# Per-feature LADSPA plugin file names. Each entry lists the
# possible .so filenames that ship the plugin — Fedora's
# ladspa-swh-plugins splits each plugin into its own .so
# (gate_1410.so), while Debian/Ubuntu bundle them into one
# (swh_plugins.so). We accept either; the probe walks the list
# and reports installed if any of them resolve in LADSPA_PATH.
#
# `install_hint` is the short orange line shown inline on the
# card. `build_cmd` is the multi-line shell snippet shown in the
# "How to install" modal — None if no build path beyond dnf.
_BUILD_RNNOISE = """\
# Install build deps. werman's CMakeLists pulls JUCE which wants
# X11/Xrandr, gtk3, webkit2gtk, alsa, libcurl — even though we only
# need the LADSPA target. The full set keeps the configure step
# happy on a fresh Fedora install.
sudo dnf install -y \\
    cmake gcc-c++ ladspa-devel git rnnoise-devel \\
    libXrandr-devel libXinerama-devel libXcursor-devel libXi-devel \\
    libcurl-devel alsa-lib-devel \\
    webkit2gtk4.1-devel gtk3-devel mesa-libGL-devel

# Clone + build only the LADSPA target so we skip JUCE's VST/AU/LV2
# plumbing. The JUCE deps above are still needed because juceaide
# (a build helper used everywhere) pulls them in even when only
# the LADSPA target is selected.
git clone https://github.com/werman/noise-suppression-for-voice.git
cd noise-suppression-for-voice
cmake -B build -DCMAKE_BUILD_TYPE=Release \\
    -DBUILD_VST_PLUGIN=OFF -DBUILD_VST3_PLUGIN=OFF \\
    -DBUILD_AU_PLUGIN=OFF -DBUILD_LV2_PLUGIN=OFF
cmake --build build -j --target rnnoise_ladspa

# Install the built .so to the system LADSPA path
sudo install -Dm755 build/ladspa/librnnoise_ladspa.so \\
    /usr/lib64/ladspa/librnnoise_ladspa.so

# Restart steelvoicemix-gui so the LADSPA probe re-runs
systemctl --user restart steelvoicemix-gui"""

_PLUGIN_REQUIREMENTS: dict[str, tuple[tuple[str, ...], str, "str | None"]] = {
    "noise_gate": (
        ("gate_1410.so", "swh_plugins.so"),
        f"sudo dnf install {_INSTALL_DNF}",
        f"sudo dnf install {_INSTALL_DNF}",
    ),
    "noise_reduction": (
        ("librnnoise_ladspa.so",),
        _INSTALL_RNNOISE_HINT,
        _BUILD_RNNOISE,
    ),
    "ai_noise_cancellation": (
        ("librnnoise_ladspa.so",),
        _INSTALL_RNNOISE_HINT,
        _BUILD_RNNOISE,
    ),
}


def _ladspa_search_paths() -> tuple[str, ...]:
    """Honour LADSPA_PATH (colon-separated, like PATH) first; fall back
    to the well-known distro defaults so a user with no env override
    still gets a correct probe."""
    env = os.environ.get("LADSPA_PATH", "")
    extra = tuple(p for p in env.split(":") if p)
    return extra + _LADSPA_DIRS


def _plugin_available(filenames) -> bool:
    """True if any of the given LADSPA filenames is on disk."""
    if isinstance(filenames, str):
        filenames = (filenames,)
    for d in _ladspa_search_paths():
        for f in filenames:
            if (Path(d) / f).is_file():
                return True
    return False


class MicrophoneTab(QWidget):
    def __init__(self, daemon_client, settings: dict, parent=None):
        super().__init__(parent)
        self._daemon = daemon_client
        # Settings dict is shared with the rest of the GUI so the
        # one-time "default source promoted" marker persists across
        # launches alongside overlay/profile prefs.
        self._settings = settings

        # Local mirror of the daemon's MicState so we can issue
        # incremental updates (only the changed feature's command)
        # rather than redundantly re-sending the full state every
        # interaction.
        self._state: dict[str, dict] = {
            key: {"enabled": False, "strength": 50}
            for key, _cmd in _FEATURES
        }

        # Per-feature debounce timers. Each slider drag updates a
        # pending strength value; the timer fires 250 ms after the
        # last change to send a single command.
        self._pending_strength: dict[str, int] = {}
        self._commit_timers: dict[str, QTimer] = {}

        # Sidetone debounce timer — same pattern as the EQ tab. While
        # the user drags the slider we only update the visible label;
        # 250 ms after the last change we fire one set-sidetone HID
        # write. Without this, every pixel of travel triggers an
        # EEPROM save-state on the headset.
        self._sidetone_pending: int | None = None
        self._sidetone_commit_timer = QTimer(self)
        self._sidetone_commit_timer.setSingleShot(True)
        self._sidetone_commit_timer.setInterval(250)
        self._sidetone_commit_timer.timeout.connect(self._commit_sidetone)

        # Voice-test loopback. We use pw-loopback (PipeWire-native)
        # rather than `pactl load-module module-loopback` because the
        # PA compat shim has a race on creation — the sink-input is
        # already accepting audio before we can mute it, leading to
        # a painful burst at full volume on the first ~100 ms. The
        # PipeWire-native loopback stays silent until real audio
        # arrives. We track the spawned subprocess.Popen handle so
        # we can kill it on stop / app quit.
        self._voice_test_proc: "subprocess.Popen | None" = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # ALPHA banner — every feature on this tab is still being
        # validated on real hardware. Surface that prominently so
        # users go in with the right expectations.
        alpha_row = QHBoxLayout()
        alpha_pill = QLabel("ALPHA")
        alpha_pill.setStyleSheet(
            "background: #FF9800; color: white; "
            "font-size: 9px; font-weight: bold; "
            "padding: 2px 6px; border-radius: 8px;"
        )
        alpha_text = QLabel(
            "All microphone features on this tab are still being "
            "tested. Behaviour may change between releases; report "
            "issues on GitHub."
        )
        alpha_text.setWordWrap(True)
        alpha_text.setStyleSheet("font-size: 11px;")
        alpha_row.addWidget(alpha_pill, 0, Qt.AlignTop)
        alpha_row.addWidget(alpha_text, 1)

        intro = QLabel(
            "Apply capture-side processing to your headset's microphone. "
            "Apps record from the SteelMic source — selectable in your "
            "system audio settings or per-app input picker."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text);"
        )
        layout.addWidget(card("Microphone Processing", alpha_row, intro))

        self.gate_toggle, self.gate_slider, self.gate_value = self._add_feature_card(
            layout,
            "noise_gate",
            "Noise Gate",
            "Mutes the mic when input level falls below the threshold. "
            "Higher strength = more aggressive (cuts more background "
            "sound; may clip the start of soft speech). Provided by "
            "swh-plugins' gate_1410.",
        )
        self.nr_toggle, self.nr_slider, self.nr_value = self._add_feature_card(
            layout,
            "noise_reduction",
            "Noise Reduction",
            "Mild RNNoise denoiser — removes constant background hum, "
            "fan noise, keyboard clatter. Capped at 50% VAD threshold "
            "for a lighter touch than AI NC.",
        )
        self.ai_toggle, self.ai_slider, self.ai_value = self._add_feature_card(
            layout,
            "ai_noise_cancellation",
            "AI Noise Cancellation",
            "Aggressive RNNoise — handles non-stationary noise (typing, "
            "dog barks, road noise) but can clip quieter speech at high "
            "strength. If both NR and AI NC are on, only AI NC runs.",
        )

        # Sidetone card (moved here from Home — sidetone is a
        # microphone-side concern and grouping it with the gate / NR
        # controls makes the page tell one coherent story).
        sidetone_row = QHBoxLayout()
        sidetone_lbl = QLabel("Sidetone")
        sidetone_lbl.setFixedWidth(80)
        self.sidetone_slider = QSlider(Qt.Horizontal)
        self.sidetone_slider.setRange(0, 128)
        self.sidetone_slider.setValue(0)
        self.sidetone_slider.setMaximumWidth(420)
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
            "Hardware sidetone — how loudly the headset feeds your raw "
            "mic back into your ears. The Arctis Nova Pro Wireless has "
            "4 internal levels; the slider quantises into whichever "
            "range it lands in."
        )
        sidetone_help.setWordWrap(True)
        sidetone_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        # Voice-test card — a software loopback from the processed
        # SteelMic source back into the headset. Lets the user A/B
        # the gate / NR / AI-NC settings against their own voice
        # without firing up Discord. We use `pactl load-module
        # module-loopback`, capture its module id, and unload on
        # toggle-off. latency_msec=20 keeps the round-trip from
        # feeling laggy without taxing the scheduler.
        voice_btn_row = QHBoxLayout()
        self.voice_test_btn = QPushButton("🎧  Hear yourself (test mic)")
        self.voice_test_btn.setCheckable(True)
        self.voice_test_btn.setMaximumWidth(280)
        self.voice_test_btn.toggled.connect(self._on_voice_test_toggled)
        voice_btn_row.addWidget(self.voice_test_btn)
        voice_btn_row.addStretch(1)

        voice_help = QLabel(
            "Loops the processed SteelMic back through your headset "
            "so you can hear what the gate / NR / AI-NC actually do. "
            "Toggle off when done — the loopback also stops "
            "automatically when you close the app."
        )
        voice_help.setWordWrap(True)
        voice_help.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        layout.addWidget(
            card(
                "Listen + Sidetone",
                sidetone_row,
                sidetone_help,
                voice_btn_row,
                voice_help,
            )
        )

        notes = QLabel(
            "Requires the noise-suppression-for-voice and swh-plugins "
            "packages. If a feature fails to come up, check that the "
            "plugins are installed (Fedora: `dnf install "
            "noise-suppression-for-voice swh-plugins`)."
        )
        notes.setWordWrap(True)
        notes.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text); padding-top: 4px;"
        )
        layout.addWidget(notes)

        layout.addStretch(1)

    # ---------------------------------------------------- daemon-event hook

    def on_mic_state_changed(self, state: dict) -> None:
        """Re-apply a daemon snapshot of the full MicState. Signals
        are blocked while we mutate widgets so the echo doesn't loop
        back as another set-mic-* command."""
        for key, _cmd in _FEATURES:
            feat = state.get(key) or {}
            enabled = bool(feat.get("enabled", False))
            strength = int(feat.get("strength", 0))
            self._state[key]["enabled"] = enabled
            self._state[key]["strength"] = strength
            toggle, slider, value_lbl = self._widgets_for(key)
            was_blocked = toggle.blockSignals(True)
            try:
                toggle.setChecked(enabled)
            finally:
                toggle.blockSignals(was_blocked)
            was_blocked = slider.blockSignals(True)
            try:
                slider.setValue(strength)
            finally:
                slider.blockSignals(was_blocked)
            value_lbl.setText(str(strength))
            slider.setEnabled(enabled)

    # --------------------------------------------------------- internals

    def _widgets_for(self, key: str):
        return {
            "noise_gate": (self.gate_toggle, self.gate_slider, self.gate_value),
            "noise_reduction": (self.nr_toggle, self.nr_slider, self.nr_value),
            "ai_noise_cancellation": (self.ai_toggle, self.ai_slider, self.ai_value),
        }[key]

    def _command_for(self, key: str) -> str:
        return dict(_FEATURES)[key]

    def _add_feature_card(
        self,
        parent_layout: QVBoxLayout,
        key: str,
        title: str,
        description: str,
    ):
        toggle_row, toggle = labelled_toggle("Enabled")
        toggle.toggled.connect(lambda checked, k=key: self._on_toggled(k, checked))

        slider_row = QHBoxLayout()
        strength_lbl = QLabel("Strength")
        strength_lbl.setFixedWidth(80)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        # Cap width so the dial isn't 1500 px wide on a maximised
        # window — at full extent the user has to drag forever to
        # nudge by a few percent. 420 px feels right for a 0..100
        # slider: precise enough for fine tweaks, fast enough to
        # sweep the range in one motion.
        slider.setMaximumWidth(420)
        slider.setEnabled(False)  # Re-enabled when toggle flips on
        value_lbl = QLabel("50")
        value_lbl.setFixedWidth(36)
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(
            lambda v, k=key, lbl=value_lbl: self._on_slider_changed(k, v, lbl)
        )
        slider.sliderReleased.connect(
            lambda k=key, s=slider: self._on_slider_released(k, s)
        )
        slider_row.addWidget(strength_lbl)
        slider_row.addWidget(slider, 1)
        slider_row.addWidget(value_lbl)

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            "font-size: 10px; color: palette(placeholder-text);"
        )

        # LADSPA-plugin availability gate. Each feature needs a
        # specific plugin file installed; if it's missing, the
        # toggle silently fails (chain spawns but the LADSPA load
        # errors and the feature doesn't take effect). Probe at
        # build time and disable the toggle + slider with a clear
        # tooltip pointing at the providing package, plus a
        # red-tinted hint in the card body itself so the user
        # doesn't have to hover to understand why.
        plugin_filenames, install_hint, build_cmd = _PLUGIN_REQUIREMENTS.get(
            key, ((), "", None)
        )
        plugin_present = (
            not plugin_filenames or _plugin_available(plugin_filenames)
        )
        contents = [toggle_row, slider_row, desc]
        if not plugin_present:
            toggle.setEnabled(False)
            slider.setEnabled(False)
            display_name = plugin_filenames[0] if plugin_filenames else ""
            toggle.setToolTip(
                f"Missing LADSPA plugin: {display_name}. {install_hint}"
            )
            # Warning row: orange ⚠ message on the left, "Show
            # install steps" button on the right. The button opens
            # a modal with the full command sequence + a Copy
            # button — saves users from typing or screenshot-OCRing
            # commands they can't select directly.
            warn_row = QHBoxLayout()
            missing_lbl = QLabel(
                f"⚠ Missing LADSPA plugin <code>{display_name}</code>. "
                f"{install_hint}"
            )
            missing_lbl.setWordWrap(True)
            missing_lbl.setTextFormat(Qt.RichText)
            missing_lbl.setOpenExternalLinks(True)
            missing_lbl.setStyleSheet(
                "font-size: 10px; color: #FF9800; padding-top: 4px;"
            )
            warn_row.addWidget(missing_lbl, 1)
            if build_cmd:
                info_btn = QPushButton("ⓘ  Show install steps")
                info_btn.setStyleSheet(
                    "font-size: 10px; padding: 4px 10px; "
                    "border: 1px solid #FF9800; color: #FF9800;"
                )
                info_btn.clicked.connect(
                    lambda _checked, cmd=build_cmd, t=title:
                        self._show_install_modal(t, cmd)
                )
                warn_row.addWidget(info_btn, 0, Qt.AlignTop)
            contents.append(warn_row)

        parent_layout.addWidget(card(title, *contents))

        # Debounce timer for this feature's slider drags.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(250)
        timer.timeout.connect(lambda k=key: self._commit_pending_strength(k))
        self._commit_timers[key] = timer
        return toggle, slider, value_lbl

    # ---------------------------------------------------------- handlers

    def _on_toggled(self, key: str, checked: bool) -> None:
        self._state[key]["enabled"] = checked
        # Slider only useful when the feature is on — disable it
        # otherwise so the user has a clear visual signal.
        _, slider, _ = self._widgets_for(key)
        slider.setEnabled(checked)
        # Send the full feature state — daemon's command takes both.
        self._daemon.send_command(
            self._command_for(key),
            enabled=checked,
            strength=self._state[key]["strength"],
        )

    def _on_slider_changed(self, key: str, value: int, label: QLabel) -> None:
        label.setText(str(value))
        self._state[key]["strength"] = value
        self._pending_strength[key] = value
        self._commit_timers[key].start()

    def _on_slider_released(self, key: str, slider: QSlider) -> None:
        # Commit immediately on release so the user gets fast
        # feedback when they let go (no 250 ms wait).
        self._pending_strength[key] = slider.value()
        self._commit_timers[key].stop()
        self._commit_pending_strength(key)

    def _commit_pending_strength(self, key: str) -> None:
        strength = self._pending_strength.pop(key, None)
        if strength is None:
            return
        self._daemon.send_command(
            self._command_for(key),
            enabled=self._state[key]["enabled"],
            strength=int(strength),
        )

    # ------------------------------------------------------- sidetone

    def on_sidetone_changed(self, level: int) -> None:
        """Daemon broadcast: persisted sidetone level changed (status
        snapshot on connect, or another GUI client set it). Re-apply
        with signals blocked so the echo doesn't loop back as another
        set-sidetone command."""
        was_blocked = self.sidetone_slider.blockSignals(True)
        try:
            self.sidetone_slider.setValue(level)
        finally:
            self.sidetone_slider.blockSignals(was_blocked)
        self.sidetone_value.setText(str(level))

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

    # ------------------------------------------------------- voice test

    def _on_voice_test_toggled(self, checked: bool) -> None:
        """Toggle a pw-loopback subprocess from SteelMic to the
        default sink. PipeWire's native loopback stays silent until
        real audio arrives — no buffer-dump burst on creation,
        unlike `pactl load-module module-loopback` which had a 100 ms+
        race window where audio leaked at full volume."""
        if checked:
            if not shutil.which("pw-loopback"):
                QMessageBox.warning(
                    self,
                    "pw-loopback not found",
                    "Voice-test needs pw-loopback on PATH (part of the "
                    "pipewire-utils package). Install it and try again.",
                )
                self.voice_test_btn.setChecked(False)
                return
            # Spawn the loopback as a managed subprocess. Capture
            # side targets SteelMic (the processed mic source);
            # playback side hits the system default sink. Latency
            # 80 ms keeps the round-trip from feeling laggy.
            try:
                self._voice_test_proc = subprocess.Popen(
                    [
                        "pw-loopback",
                        "--capture-props=node.target=SteelMic "
                        "media.name=SteelVoiceMix-VoiceTest",
                        "--playback-props=media.name=SteelVoiceMix-VoiceTest",
                        "--latency", "80",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "Voice-test failed",
                    f"Could not start pw-loopback:\n{e}",
                )
                self.voice_test_btn.setChecked(False)
                self._voice_test_proc = None
                return
            self.voice_test_btn.setText("🛑  Stop voice test")
        else:
            self._teardown_voice_test()
            self.voice_test_btn.setText("🎧  Hear yourself (test mic)")

    # ---------------------------------------------------- install modal

    def _show_install_modal(self, feature_title: str, command_text: str) -> None:
        """Modal dialog with copyable install commands. Triggered by
        the ⓘ button next to a 'Missing LADSPA plugin' warning when
        we have a build-from-source recipe (e.g. for librnnoise_ladspa
        which Fedora doesn't package). The QPlainTextEdit makes the
        text selectable; the Copy button puts it on the clipboard so
        users don't have to hand-select."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Install {feature_title} dependencies")
        dlg.setMinimumWidth(560)

        layout = QVBoxLayout(dlg)

        intro = QLabel(
            "Run these commands in a terminal to build and install "
            "the missing LADSPA plugin. After it's done, restart "
            "steelvoicemix-gui (the last command does this) and the "
            "feature toggle will go enabled."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        text_edit = QPlainTextEdit()
        text_edit.setPlainText(command_text)
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet(
            "font-family: monospace; font-size: 11px; "
            "background: palette(base);"
        )
        text_edit.setMinimumHeight(220)
        layout.addWidget(text_edit, 1)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("📋  Copy to clipboard")
        copy_btn.clicked.connect(
            lambda: self._copy_to_clipboard_with_feedback(
                command_text, copy_btn
            )
        )
        btn_row.addWidget(copy_btn)
        btn_row.addStretch(1)
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(dlg.reject)
        btn_row.addWidget(close_box)
        layout.addLayout(btn_row)

        dlg.exec()

    def _copy_to_clipboard_with_feedback(
        self, text: str, btn: QPushButton
    ) -> None:
        """Copy `text` to the system clipboard and flash the button
        label so the user gets visual confirmation. Restores the
        original label after 1.5 s."""
        QApplication.clipboard().setText(text)
        original = btn.text()
        btn.setText("✓  Copied!")
        QTimer.singleShot(1500, lambda: btn.setText(original))

    def _teardown_voice_test(self) -> None:
        if self._voice_test_proc is None:
            return
        try:
            self._voice_test_proc.terminate()
            try:
                self._voice_test_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._voice_test_proc.kill()
                self._voice_test_proc.wait(timeout=1)
        except Exception:
            pass
        self._voice_test_proc = None

    # ------------------------------------------------------- default-source

    def on_mic_default_source_changed(self, active: bool) -> None:
        """Daemon broadcast: SteelMic is now (or no longer) the
        system default source. The first time it goes active in a
        user's life, surface a one-shot dialog explaining what just
        changed — without it, the swap is invisible and people get
        confused why their default mic suddenly says SteelMic. After
        the first time, persisted via settings.json so we don't
        nag on every subsequent toggle."""
        if not active:
            return
        if self._settings.get("mic_default_promoted_shown", False):
            return
        QMessageBox.information(
            self,
            "Default microphone changed",
            "Heads up: SteelMic is now your system's default "
            "microphone source. Apps that follow the system default "
            "(most do) will pick up the processed audio automatically. "
            "If you'd rather use a different mic, switch the default "
            "back from your system audio settings — disabling all "
            "microphone features here also restores the previous "
            "default.\n\nThis notice only shows once.",
        )
        self._settings["mic_default_promoted_shown"] = True
        save_settings(self._settings)

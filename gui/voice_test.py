"""Shared owner of the 'Hear yourself' loopback subprocess.

Two tabs surface this control — the Microphone tab and the
Equalizer tab (when the Mic channel is selected). Both need to
toggle the same `pw-loopback` process and reflect each other's
state. Keeping the subprocess + state in MixerGUI (instead of one
of the tabs) lets either toggle drive it cleanly:

    MicrophoneTab "Hear yourself"        ─┐
                                          ├──> VoiceTestService
    EqualizerTab Mic-channel "Hear yourself" ─┘

The service emits `state_changed(bool)` on every transition so
both buttons stay in sync without each tab knowing about the
other.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class VoiceTestService(QObject):
    state_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> tuple[bool, str]:
        """Spawn `pw-loopback` from SteelMic to the default sink.
        Returns (ok, error_message). On failure, error_message is
        a user-readable string; the caller is responsible for
        surfacing it (toast, dialog, etc.)."""
        if self.is_running:
            return True, ""
        if not shutil.which("pw-loopback"):
            return False, (
                "Voice-test needs pw-loopback on PATH (part of the "
                "pipewire-utils package). Install it and try again."
            )
        try:
            self._proc = subprocess.Popen(
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
            self._proc = None
            return False, f"Could not start pw-loopback:\n{e}"
        log.info("Voice-test started (pid %d)", self._proc.pid)
        self.state_changed.emit(True)
        return True, ""

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=1)
        except Exception:
            pass
        self._proc = None
        log.info("Voice-test stopped")
        self.state_changed.emit(False)

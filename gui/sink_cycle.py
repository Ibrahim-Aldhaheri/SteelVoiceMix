"""Default-sink cycler.

Used by the Sinks tab's 'Cycle default sink' button and by the
QShortcut wired in MixerGUI when the user has the cycle shortcut
enabled in Settings. Reads `pactl list sinks short` to find which
SteelVoiceMix virtual sinks are currently loaded, then advances
the system default to the next one in a fixed order:

    SteelGame → SteelChat → SteelMedia → SteelHDMI → SteelGame …

Skips sinks the daemon hasn't loaded (Media / HDMI gate on user
toggles). If the current default is something else entirely
(headset directly, EasyEffects, etc.) the cycle starts at
SteelGame.

Lives in its own module so the keyboard-shortcut path and the
button-click path share one implementation — no copy-pasting.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

_PREFERRED_ORDER = ("SteelGame", "SteelChat", "SteelMedia", "SteelHDMI")


def _list_sinks() -> list[str]:
    """All sink names currently loaded in PipeWire / PulseAudio."""
    if not shutil.which("pactl"):
        return []
    try:
        r = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out: list[str] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append(parts[1])
    return out


def _current_default() -> str:
    """Currently-default sink name, or an empty string on probe error."""
    if not shutil.which("pactl"):
        return ""
    try:
        r = subprocess.run(
            ["pactl", "info"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return ""
    if r.returncode != 0:
        return ""
    for line in r.stdout.splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":", 1)[1].strip()
    return ""


def cycle_default_sink() -> tuple[str, str]:
    """Advance the system default to the next loaded SteelVoiceMix
    sink in `_PREFERRED_ORDER`. Returns (previous, new) names so
    the caller can surface a toast / status update. On any failure
    returns ('', '') and logs a warning."""
    available = _list_sinks()
    candidates = [s for s in _PREFERRED_ORDER if s in available]
    if not candidates:
        log.warning("Cycle default sink: no SteelVoiceMix sinks loaded")
        return "", ""
    prev = _current_default()
    if prev in candidates:
        idx = (candidates.index(prev) + 1) % len(candidates)
    else:
        idx = 0
    new = candidates[idx]
    if new == prev:
        return prev, prev
    try:
        r = subprocess.run(
            ["pactl", "set-default-sink", new],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            log.warning(
                "Cycle default sink: pactl set-default-sink %s failed",
                new,
            )
            return prev, ""
    except Exception as e:
        log.warning("Cycle default sink: pactl error: %s", e)
        return prev, ""
    log.info("Cycled default sink: %r → %r", prev, new)
    return prev, new

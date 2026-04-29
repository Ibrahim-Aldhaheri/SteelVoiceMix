"""Bundled default HRIR for virtual surround.

The HRIR is a 14-channel HeSuVi-format WAV (`EAC_Default.wav`, ~165 KB)
derived from Equalizer APO Convolver's reference set. We ship it
directly in the package — `gui/data/hrir/EAC_Default.wav` — so the
"Use Default" button is instantaneous and works offline. Refreshing
the bundle is the maintainer's job (rerun
`scripts/fetch_asm_presets.py` won't touch this — for HRIR you
re-download manually if upstream changes).

Path resolution mirrors `gui.eq_presets.bundled_asm_dir`: relative to
this file so it works from a source checkout, an installed
`/usr/lib/python.../gui/` location, or a Flatpak sandbox.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_HRIR_FILENAME = "EAC_Default.wav"


def bundled_default_path() -> Path:
    """Absolute path to the bundled default HRIR WAV."""
    return Path(__file__).resolve().parent / "data" / "hrir" / DEFAULT_HRIR_FILENAME


def has_default() -> bool:
    """Quick existence check used by the Surround tab to decide whether
    the Use Default button has anything to point at. Returns False on
    a broken install where the data file is missing."""
    p = bundled_default_path()
    return p.is_file() and p.stat().st_size > 0

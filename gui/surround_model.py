"""Pure-logic model for the surround stage editor.

Kept free of Qt so it can be unit-tested directly, and so the honest
constraints live in one obvious place:

  * A channel's DIRECTION (azimuth) is fixed by the HRIR sound profile
    and is NOT editable here — see STATUS.md. Azimuth values below are
    the standard layout angles, used only to draw each speaker in its
    correct place.
  * A channel's LEVEL (gain in dB) IS editable and maps to a real gain
    node in the PipeWire chain. The editor's radial drag changes level,
    never direction.

Angle convention: degrees clockwise from straight ahead (the listener
faces 0°). -30° is front-left, +30° is front-right, ±90° is directly to
the side, ±150° is behind.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Level range. Below MUTE_DB the channel is treated as silent; above 0
# is a modest boost. Kept deliberately narrow — this is a trim, not a
# mixing desk, and large boosts on a binaural chain clip easily.
GAIN_MIN_DB = -24.0
GAIN_MAX_DB = 6.0
GAIN_DEFAULT_DB = 0.0
# A drag to the very outer edge snaps to "off" rather than a -24 dB
# whisper, so "drag it away to silence this channel" works intuitively.
MUTE_THRESHOLD_DB = GAIN_MIN_DB


@dataclass
class Channel:
    key: str            # daemon channel key: fl, fr, fc, lfe, rl, rr, sl, sr
    label: str          # human label (control panel)
    azimuth: float      # fixed direction, degrees clockwise from front
    short: str = ""     # short code drawn on the marker (FL, C, …)
    positional: bool = True   # LFE is non-directional (drawn near centre)
    gain_db: float = GAIN_DEFAULT_DB
    muted: bool = False


# The chain is always 8-channel (7.1). Azimuths are the standard ITU/
# HeSuVi layout angles. LFE has no direction.
def default_channels() -> dict[str, Channel]:
    return {
        "fl": Channel("fl", "Front left", -30.0, short="FL"),
        "fr": Channel("fr", "Front right", 30.0, short="FR"),
        "fc": Channel("fc", "Center", 0.0, short="C"),
        "lfe": Channel("lfe", "LFE (bass)", 0.0, short="LFE", positional=False),
        "sl": Channel("sl", "Side left", -90.0, short="SL"),
        "sr": Channel("sr", "Side right", 90.0, short="SR"),
        "rl": Channel("rl", "Rear left", -150.0, short="RL"),
        "rr": Channel("rr", "Rear right", 150.0, short="RR"),
    }


def clamp_db(db: float) -> float:
    """Clamp a level to the editable range. Non-finite -> default."""
    try:
        v = float(db)
    except (TypeError, ValueError):
        return GAIN_DEFAULT_DB
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return GAIN_DEFAULT_DB
    return max(GAIN_MIN_DB, min(GAIN_MAX_DB, v))


def db_to_fraction(db: float) -> float:
    """Map a level in dB to a radial fraction in [0.35, 1.0].

    Louder channels sit closer to the listener (smaller radius); quieter
    ones sit farther out. 0.35 keeps the loudest speakers off the head
    itself so labels stay readable.
    """
    db = clamp_db(db)
    t = (db - GAIN_MIN_DB) / (GAIN_MAX_DB - GAIN_MIN_DB)  # 0..1, louder=1
    inner, outer = 0.5, 1.0
    return outer - t * (outer - inner)


def fraction_to_db(fraction: float) -> float:
    """Inverse of db_to_fraction: a radial fraction back to a level."""
    inner, outer = 0.5, 1.0
    f = max(inner, min(outer, fraction))
    t = (outer - f) / (outer - inner)  # 0 at outer, 1 at inner
    return clamp_db(GAIN_MIN_DB + t * (GAIN_MAX_DB - GAIN_MIN_DB))


def db_to_linear(db: float) -> float:
    """Convert dB to a linear amplitude multiplier for the gain node."""
    return 10.0 ** (clamp_db(db) / 20.0)


# --- Presets ---------------------------------------------------------
#
# Presets only touch LEVEL (and which channels are muted) — never
# direction, because direction isn't ours to change. Each maps a
# channel key to a dB trim.

PRESETS: dict[str, dict[str, float]] = {
    # Everything at reference level.
    "flat": {k: 0.0 for k in default_channels()},
    # 5.1: silence the two side channels (a 5.1 source has none), leave
    # the rest flat. The sink is still 8-channel; this just trims.
    "5.1": {"sl": MUTE_THRESHOLD_DB, "sr": MUTE_THRESHOLD_DB},
    # 7.1 reference: all channels active and flat.
    "7.1": {k: 0.0 for k in default_channels()},
    # Bring the soundstage forward: lift fronts, trim rears/sides.
    "front-focus": {
        "fl": 2.0, "fr": 2.0, "fc": 2.0,
        "sl": -4.0, "sr": -4.0, "rl": -6.0, "rr": -6.0,
    },
    # Emphasise dialogue: lift centre, gently trim everything else.
    "dialogue": {
        "fc": 4.0, "fl": -2.0, "fr": -2.0,
        "sl": -3.0, "sr": -3.0, "rl": -3.0, "rr": -3.0,
    },
}

PRESET_LABELS = {
    "flat": "Flat (reference)",
    "5.1": "5.1 layout",
    "7.1": "7.1 layout",
    "front-focus": "Front focus",
    "dialogue": "Dialogue boost",
}


def apply_preset(channels: dict[str, Channel], preset_key: str) -> None:
    """Apply a preset in place. A channel absent from the preset dict is
    reset to 0 dB, so presets are absolute, not additive. Mute follows
    the mute threshold so the '5.1' preset actually silences the sides."""
    preset = PRESETS.get(preset_key)
    if preset is None:
        return
    for key, ch in channels.items():
        db = clamp_db(preset.get(key, 0.0))
        ch.gain_db = db
        ch.muted = db <= MUTE_THRESHOLD_DB


def serialize(channels: dict[str, Channel]) -> dict[str, dict]:
    """Persistable form: {key: {gain_db, muted}}. Positions/labels are
    static and re-derived, so only the editable state is stored."""
    return {
        k: {"gain_db": round(c.gain_db, 2), "muted": bool(c.muted)}
        for k, c in channels.items()
    }


def deserialize(data: dict | None) -> dict[str, Channel]:
    """Rebuild channels from persisted state, tolerating missing/legacy
    keys so an older settings.json (no surround profile at all) migrates
    cleanly to defaults."""
    channels = default_channels()
    if not isinstance(data, dict):
        return channels
    for key, ch in channels.items():
        entry = data.get(key)
        if isinstance(entry, dict):
            ch.gain_db = clamp_db(entry.get("gain_db", GAIN_DEFAULT_DB))
            ch.muted = bool(entry.get("muted", False))
    return channels

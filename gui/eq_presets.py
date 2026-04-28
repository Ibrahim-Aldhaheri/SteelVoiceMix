"""EQ presets — built-in tunings + user save/load/list.

Built-ins are hardcoded here rather than shipped as JSON files so
upgrades roll forward without leftover preset files in the user's
config dir to clean up. User presets live in
`$XDG_CONFIG_HOME/steelvoicemix/presets/<channel>/<safe-name>.json`,
one file per preset, each carrying the full 10-band shape.

Preset shape on disk:
    {
        "name": "Display Name (with spaces or whatever)",
        "channel": "game" | "chat" | "mic",
        "bands": [
            {"freq": 32.0, "q": 0.7, "gain": 6.0,
             "type": "lowshelf", "enabled": true},
            ... 10 entries ...
        ]
    }

The same shape is what `EqualizerTab` keeps in memory and what the
daemon's `set-eq-channel` command consumes — no transform on either
side, the JSON is the schema.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

NUM_BANDS = 10


def _band(freq: float, q: float, gain: float, band_type: str) -> dict:
    return {
        "freq": float(freq),
        "q": float(q),
        "gain": float(gain),
        "type": band_type,
        "enabled": True,
    }


def _flat() -> list[dict]:
    """Standard 10-band passthrough — same shape used as the daemon
    default. New presets that don't override every band start from this."""
    return [
        _band(32, 0.7, 0, "lowshelf"),
        _band(64, 1.0, 0, "peaking"),
        _band(125, 1.0, 0, "peaking"),
        _band(250, 1.0, 0, "peaking"),
        _band(500, 1.0, 0, "peaking"),
        _band(1000, 1.0, 0, "peaking"),
        _band(2000, 1.0, 0, "peaking"),
        _band(4000, 1.0, 0, "peaking"),
        _band(8000, 1.0, 0, "peaking"),
        _band(16000, 0.7, 0, "highshelf"),
    ]


def _override(base: list[dict], gains_by_index: dict[int, float]) -> list[dict]:
    """Return a copy of `base` with the listed indices' gains replaced.
    Lets each preset declaration stay readable — only the bands the
    preset actually moves get listed, every other band stays at 0 dB."""
    out = [dict(b) for b in base]
    for idx, gain in gains_by_index.items():
        out[idx]["gain"] = float(gain)
    return out


# --------------------------------------------------------- built-in presets
#
# Game presets target media + game audio. Chat presets target voice — they
# avoid bass lift (which makes voices muddy on a chat channel) and instead
# emphasise presence/intelligibility.

GAME_PRESETS: list[dict] = [
    {"name": "Flat", "bands": _flat()},
    {
        "name": "Bass Boost",
        "bands": _override(_flat(), {0: 6, 1: 4, 2: 2, 4: -1}),
    },
    {
        "name": "Footsteps (FPS)",
        # Cut sub-bass rumble, gentle scoop in lower mids, lift the
        # 2–4 kHz range where footsteps + reload clicks live.
        "bands": _override(_flat(), {0: -3, 3: -2, 6: 4, 7: 4, 8: 2}),
    },
    {
        "name": "Cinematic",
        # V-shape: bass + treble lift, mid scoop. Big-room movie sound.
        "bands": _override(
            _flat(), {0: 5, 1: 3, 4: -2, 5: -3, 6: -1, 8: 3, 9: 4}
        ),
    },
    {
        "name": "Loudness",
        # Equal-loudness contour compensation — gentle bass + treble lift
        # at low listening volumes. Mids untouched.
        "bands": _override(_flat(), {0: 4, 1: 2, 8: 2, 9: 3}),
    },
]

CHAT_PRESETS: list[dict] = [
    {"name": "Flat", "bands": _flat()},
    {
        "name": "Voice Clarity",
        # Roll off below 100 Hz, lift presence range. Standard broadcast
        # voice tuning.
        "bands": _override(_flat(), {0: -6, 1: -3, 5: 2, 6: 4, 7: 3}),
    },
    {
        "name": "Warm Voice",
        # Gentle bass body for richer male voices, mild presence lift.
        "bands": _override(_flat(), {1: 2, 2: 1, 6: 2, 7: 1}),
    },
    {
        "name": "Phone Call",
        # Telephone-band emulation — narrow, cut everything but 300–
        # 3000 Hz. For nostalgia / vibe; not for serious chat.
        "bands": _override(
            _flat(),
            {0: -10, 1: -8, 2: -3, 5: 0, 7: -3, 8: -8, 9: -10},
        ),
    },
]


BUILT_IN_PRESETS: dict[str, list[dict]] = {
    "game": GAME_PRESETS,
    "chat": CHAT_PRESETS,
}


# ------------------------------------------------------------- user presets


def user_preset_dir(channel: str | None = None) -> Path:
    """Return the directory that stores user-saved presets, optionally
    scoped to a channel sub-folder. Created on demand."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    root = Path(base) / "steelvoicemix" / "presets"
    if channel:
        root = root / channel
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(name: str) -> str:
    """Sanitise a preset name into something safe to put on disk.
    Permits letters, digits, dashes, underscores, spaces, and dots —
    everything else gets dropped."""
    cleaned = "".join(c for c in name if c.isalnum() or c in "-_ .").strip()
    # Collapse runs of whitespace, then trim trailing dots/spaces that
    # confuse some filesystems.
    cleaned = " ".join(cleaned.split()).rstrip(". ")
    return cleaned


def list_user_presets(channel: str) -> list[dict]:
    """Load all user-saved presets for a channel. Malformed files are
    skipped with a log warning rather than crashing the GUI."""
    d = user_preset_dir(channel)
    out: list[dict] = []
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Skipping unreadable preset %s: %s", path, e)
            continue
        bands = data.get("bands")
        if not isinstance(bands, list) or len(bands) != NUM_BANDS:
            log.warning("Skipping preset %s: wrong band count", path)
            continue
        out.append({"name": data.get("name", path.stem), "bands": bands})
    return out


def list_presets(channel: str) -> list[dict]:
    """All presets visible for `channel` — built-ins first, then user
    presets in alphabetical order. Returns a list of dicts with `name`
    and `bands` keys."""
    return list(BUILT_IN_PRESETS.get(channel, [])) + list_user_presets(channel)


def save_user_preset(name: str, channel: str, bands: list[dict]) -> str:
    """Persist a preset to disk under the user's config dir. Returns
    the actual filename (post-sanitisation) so the caller can warn the
    user if their input had to be cleaned. Raises ValueError for an
    empty/invalid name or wrong band count."""
    safe = _safe_filename(name)
    if not safe:
        raise ValueError("Preset name must contain at least one letter or digit.")
    if len(bands) != NUM_BANDS:
        raise ValueError(f"Preset must have exactly {NUM_BANDS} bands; got {len(bands)}.")
    d = user_preset_dir(channel)
    path = d / f"{safe}.json"
    payload = {"name": name, "channel": channel, "bands": bands}
    path.write_text(json.dumps(payload, indent=2))
    return safe


def delete_user_preset(name: str, channel: str) -> bool:
    """Delete a user-saved preset by display name. Returns True on
    success, False if the file didn't exist (e.g. for built-ins)."""
    safe = _safe_filename(name)
    if not safe:
        return False
    path = user_preset_dir(channel) / f"{safe}.json"
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        log.warning("Could not delete preset %s: %s", path, e)
        return False


def is_user_preset(name: str, channel: str) -> bool:
    """True iff the named preset is user-saved (not a built-in). The
    Delete button only enables for these."""
    builtin_names = {p["name"] for p in BUILT_IN_PRESETS.get(channel, [])}
    return name not in builtin_names


def find_preset(name: str, channel: str) -> dict | None:
    """Look up a preset by display name across built-ins + user list."""
    for preset in list_presets(channel):
        if preset["name"] == name:
            return preset
    return None

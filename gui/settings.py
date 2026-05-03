"""Persistent user settings for the SteelVoiceMix GUI.

Stored as JSON with a schema version so we can migrate cleanly when the
shape changes. A one-shot migration from the pre-v1 `settings.conf`
format lands new values into settings.json on first run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "steelvoicemix"
DISPLAY_NAME = "SteelVoiceMix"
def _derive_app_version() -> str:
    """Read the package version from RPM at runtime so the GUI's
    'About' dialog and User-Agent header match what's actually
    installed. Falls back to the hardcoded constant when the
    binary isn't an RPM (manual install, source checkout, etc)."""
    import subprocess
    try:
        r = subprocess.run(
            ["rpm", "-q", "--qf", "%{VERSION}", APP_NAME],
            capture_output=True, text=True, timeout=2,
        )
        v = r.stdout.strip()
        if r.returncode == 0 and v and not v.startswith("package"):
            return v
    except Exception:
        pass
    return _APP_VERSION_FALLBACK


# Bumped manually on each beta cut; runtime derives from RPM if
# available so this constant only matters in source-checkout /
# manual-install scenarios.
_APP_VERSION_FALLBACK = "0.4.2~beta8"
APP_VERSION = _derive_app_version()

CONFIG_DIR = Path.home() / ".config" / APP_NAME
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LEGACY_CONF = CONFIG_DIR / "settings.conf"

OVERLAY_POSITIONS = (
    "top-right",
    "top-left",
    "bottom-right",
    "bottom-left",
    "center",
)
OVERLAY_ORIENTATIONS = ("horizontal", "vertical")

SCHEMA_VERSION = 1

# Keys that participate in audio profiles — when the user saves a profile,
# we snapshot these from the current settings dict; when they load one, we
# write them back. Daemon-side state (media/HDMI sink toggles) is captured
# at save time as a separate dict in the profile entry, applied via socket
# commands at load time.
PROFILE_GUI_KEYS: tuple[str, ...] = (
    "overlay",
    "overlay_position",
    "overlay_orientation",
)

DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA_VERSION,
    "overlay": True,
    "autostart": True,
    # When true, the GUI starts hidden in the system tray instead
    # of opening its main window. Useful with autostart so the app
    # doesn't pop up at every login. Ignored if no system tray is
    # available (the close-to-tray path already handles that case).
    "start_minimized": False,
    "overlay_position": "top-right",
    "overlay_orientation": "horizontal",
    # name -> {"gui": {...PROFILE_GUI_KEYS subset...}, "sinks": {"media": bool, "hdmi": bool}}
    "profiles": {},
    # channel -> [preset name, ...]; capped at MAX_FAVOURITES_PER_CHANNEL
    # entries each via add_favourite(). Used by the EQ tab to pin
    # favourite presets to the top of the preset dropdown.
    "eq_favourites": {},
    # Notification preferences. The minimize-to-tray toast was the
    # specifically-flagged annoyance — closing the window with the X
    # button popped a toast every single time. Default off so new
    # users aren't pestered; users who want the reminder can re-enable.
    "notify_minimize_hint": False,
    # First-run marker for the surround default. Surround is on out
    # of the box — on first GUI launch we send the bundled HRIR path
    # to the daemon so its surround_enabled flag has something to
    # bind to. Once this flips True we never auto-send again, so a
    # user who later clears the path stays cleared.
    "surround_default_applied": False,
    # One-shot marker: True after we've shown the "we promoted
    # SteelMic to your default mic" notification once. The mic chain
    # silently swaps the default every time it spawns; this flag
    # gates the user-facing dialog so it only appears the very
    # first time, not every toggle.
    "mic_default_promoted_shown": False,
    # Appearance: 'auto' follows the system colour scheme; 'light'
    # / 'dark' override with our packaged palettes. See gui/theme.py.
    "theme_mode": "auto",
    # UI language. 'system' follows the OS locale; explicit codes
    # ('en', 'ar', etc.) override. Translation coverage is partial
    # — strings without a translation fall back to English, which
    # means a user picking 'ar' sees a mix until the .qm catches
    # up. Tracked in gui/translations/<code>.ts.
    "ui_language": "system",
    # Auto-switch the Game-channel EQ when a known game launches.
    # When True, the GameWatcher polls SteelGame's sink-inputs and:
    #   1. Looks up `game_eq_bindings[detected_name]` (manual override).
    #   2. Falls back to fuzzy-matching the bundled ASM preset library.
    #   3. Snapshots the current Game EQ, applies the matched preset.
    #   4. Restores the snapshot when the game's sink-input disappears.
    # Off by default — opt-in feature, audible only when on.
    "auto_game_eq_enabled": False,
    # Ordered list of manual game→preset overrides. First matching
    # entry wins, so the user controls priority by drag-reordering
    # rows in the EQ tab's binding table. Each entry is
    # {"game": <application.name>, "preset": <preset name>}.
    # Migrated automatically from the legacy dict shape on first
    # load by `gui/settings.py:load()`.
    "game_eq_bindings": [],
    # Persisted state of the Auto Game-EQ orchestrator. Without
    # these, suspending the PC right after closing a game (before
    # the watcher's 4-second exit grace fires) lost the snapshot
    # that was supposed to restore the user's pre-game EQ — the
    # daemon kept the game preset on disk and there was nothing to
    # override it with after resume / GUI restart.
    #
    #   auto_game_eq_active_preset: name of the preset currently
    #     applied via auto-switch, or "" if no auto preset is
    #     engaged. Set on _enter / _switch, cleared on _exit.
    #   auto_game_eq_snapshot_bands: list[band-dict], the user's
    #     pre-game Game-channel EQ. Saved at _enter, consumed at
    #     _exit. Empty list means "no snapshot stored".
    "auto_game_eq_active_preset": "",
    "auto_game_eq_snapshot_bands": [],
    # Default-sink cycle shortcut. When enabled, the configured
    # key combo cycles the system default sink between the loaded
    # SteelVoiceMix virtual sinks (Game / Chat / Media / HDMI).
    # Off by default — adds a global-ish keybinding, which users
    # should opt into. The combo is a Qt key-sequence string;
    # default Ctrl+Shift+S works in most desktop environments
    # without conflicting with common app shortcuts.
    "default_sink_cycle_enabled": False,
    "default_sink_cycle_combo": "Ctrl+Shift+S",
    # Sink names to skip when cycling (e.g. "SteelChat"). Stored
    # as a list of canonical sink names — the cycle helper compares
    # against `pactl list sinks short` output so the names must
    # match exactly.
    "default_sink_cycle_exclude": [],
}

# Star-tier capacity per channel. Five is enough to cover the main use
# cases (bass / vocal / footsteps / cinematic / flat) without the
# dropdown's pinned section eating too much vertical space.
MAX_FAVOURITES_PER_CHANNEL = 5


def socket_path() -> str:
    """Match the Rust daemon's socket location (XDG_RUNTIME_DIR preferred)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, f"{APP_NAME}.sock")
    return f"/tmp/{APP_NAME}-{os.getuid()}.sock"


def _migrate_legacy() -> dict[str, Any] | None:
    """Parse a pre-schema settings.conf if present. Returns None if absent."""
    if not LEGACY_CONF.exists():
        return None
    try:
        result: dict[str, Any] = {}
        bool_keys = {"overlay", "autostart"}
        for line in LEGACY_CONF.read_text().strip().splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key, val = k.strip(), v.strip()
            if key in bool_keys:
                result[key] = val.lower() in ("true", "1", "yes")
            else:
                result[key] = val
        return result
    except Exception:
        return None


def load() -> dict[str, Any]:
    """Load settings, falling back to defaults. Migrates legacy conf on first run."""
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text())
            if isinstance(loaded, dict):
                settings.update(loaded)
        except Exception:
            pass
    else:
        legacy = _migrate_legacy()
        if legacy:
            settings.update(legacy)
            save(settings)
            try:
                LEGACY_CONF.unlink()
            except OSError:
                pass
    settings["schema"] = SCHEMA_VERSION
    # Migrate legacy game_eq_bindings dict → ordered list shape.
    # Older versions persisted as `{game: preset}` (one preset per
    # game). New shape is a list of `{game, preset}` so the user
    # can reorder priority. Convert any dict found on disk to the
    # list form once; subsequent saves use the list shape.
    legacy = settings.get("game_eq_bindings")
    if isinstance(legacy, dict):
        settings["game_eq_bindings"] = [
            {"game": k, "preset": v} for k, v in sorted(legacy.items())
        ]
    return settings


def save(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    serializable = {**settings, "schema": SCHEMA_VERSION}
    SETTINGS_FILE.write_text(json.dumps(serializable, indent=2) + "\n")


def normalize_position(value: str) -> str:
    return value if value in OVERLAY_POSITIONS else "top-right"


def normalize_orientation(value: str) -> str:
    return value if value in OVERLAY_ORIENTATIONS else "horizontal"


# ----------------------------------------------------------------- profiles


def _profiles_dict(settings: dict[str, Any]) -> dict[str, Any]:
    """settings["profiles"] is the canonical store. Older settings.json
    files predate the field — return an empty dict in that case rather than
    failing."""
    p = settings.get("profiles")
    if not isinstance(p, dict):
        p = {}
        settings["profiles"] = p
    return p


def list_profiles(settings: dict[str, Any]) -> list[str]:
    return sorted(_profiles_dict(settings).keys())


def save_profile(
    settings: dict[str, Any],
    name: str,
    *,
    media_enabled: bool,
    hdmi_enabled: bool,
    eq_state: dict[str, list[dict]] | None = None,
    mic_state: dict | None = None,
) -> None:
    """Snapshot the current GUI keys + supplied daemon-side state.
    Overwrites any existing profile of the same name.

    `eq_state`: per-channel band lists (game/chat/media/hdmi/mic),
    each entry a 10-band list of {freq, q, gain, type, enabled}.
    Snapshotted as-is so a later load just sends one set-eq-channel
    per channel.
    `mic_state`: full MicState dict (noise_gate / NR / AI-NC /
    volume_stabilizer + volume_stabilizer_kind). Applied via per-
    feature daemon commands on load."""
    name = name.strip()
    if not name:
        raise ValueError("profile name must not be empty")
    profiles = _profiles_dict(settings)
    entry: dict[str, Any] = {
        "gui": {k: settings.get(k, DEFAULTS.get(k)) for k in PROFILE_GUI_KEYS},
        "sinks": {"media": bool(media_enabled), "hdmi": bool(hdmi_enabled)},
    }
    if eq_state:
        # Deep-copy via dict / list comprehensions so future mutations
        # to the live cache don't leak into the saved profile.
        entry["eq"] = {
            ch: [dict(b) for b in bands]
            for ch, bands in eq_state.items()
        }
    if mic_state:
        entry["mic"] = dict(mic_state)
    profiles[name] = entry
    save(settings)


def load_profile(settings: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Apply the named profile's GUI keys to `settings` (mutating) and
    return the profile dict so the caller can act on the daemon-side
    sink toggles. Returns None if the profile doesn't exist."""
    profile = _profiles_dict(settings).get(name)
    if not isinstance(profile, dict):
        return None
    gui = profile.get("gui")
    if isinstance(gui, dict):
        for k in PROFILE_GUI_KEYS:
            if k in gui:
                settings[k] = gui[k]
    save(settings)
    return profile


def delete_profile(settings: dict[str, Any], name: str) -> bool:
    profiles = _profiles_dict(settings)
    if name in profiles:
        del profiles[name]
        save(settings)
        return True
    return False


# ----------------------------------------------------------- EQ favourites


def _favourites_dict(settings: dict[str, Any]) -> dict[str, list[str]]:
    fav = settings.get("eq_favourites")
    if not isinstance(fav, dict):
        fav = {}
        settings["eq_favourites"] = fav
    return fav


def get_favourites(settings: dict[str, Any], channel: str) -> list[str]:
    """Ordered list of favourited preset names for `channel`. Order is
    preserved as added; the EQ tab uses it to render the pinned section
    of the dropdown."""
    raw = _favourites_dict(settings).get(channel, [])
    if not isinstance(raw, list):
        return []
    return [str(n) for n in raw]


def is_favourite(settings: dict[str, Any], channel: str, name: str) -> bool:
    return name in get_favourites(settings, channel)


def add_favourite(settings: dict[str, Any], channel: str, name: str) -> bool:
    """Mark `name` as a favourite on `channel`. Returns False if the
    channel is already at MAX_FAVOURITES_PER_CHANNEL — the caller can
    then prompt the user to unfavourite something first. No-op if the
    name was already in the list."""
    fav = _favourites_dict(settings)
    current = fav.get(channel, [])
    if not isinstance(current, list):
        current = []
    if name in current:
        return True
    if len(current) >= MAX_FAVOURITES_PER_CHANNEL:
        return False
    current.append(name)
    fav[channel] = current
    save(settings)
    return True


def remove_favourite(settings: dict[str, Any], channel: str, name: str) -> bool:
    fav = _favourites_dict(settings)
    current = fav.get(channel, [])
    if not isinstance(current, list) or name not in current:
        return False
    current.remove(name)
    fav[channel] = current
    save(settings)
    return True


def rename_favourite(
    settings: dict[str, Any], channel: str, old: str, new: str
) -> None:
    """Keep favourites in sync with a preset rename. No-op if the old
    name wasn't favourited."""
    fav = _favourites_dict(settings)
    current = fav.get(channel, [])
    if not isinstance(current, list) or old not in current:
        return
    current[current.index(old)] = new
    fav[channel] = current
    save(settings)


def reset_to_defaults_preserving_profiles(settings: dict[str, Any]) -> None:
    """Wipe every key in `settings` back to its DEFAULTS value EXCEPT
    `profiles` — the user's saved audio profiles are explicitly kept.
    Mutates `settings` in place and writes the new state to disk.

    Used by the Settings tab's 'Reset to defaults' button. Companion
    to the daemon's `reset-state` command, which handles its own
    persistent state separately."""
    profiles = _profiles_dict(settings)
    settings.clear()
    for k, v in DEFAULTS.items():
        # Deep-copy mutable defaults so the clear+restore cycle
        # doesn't accidentally have settings sharing references with
        # the module-level DEFAULTS dict.
        if isinstance(v, dict):
            settings[k] = {}
        elif isinstance(v, list):
            settings[k] = []
        else:
            settings[k] = v
    settings["profiles"] = profiles
    save(settings)

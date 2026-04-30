"""Auto game-EQ — detect what game is playing on SteelGame and apply
the matching ASM preset, then restore the user's EQ when the game
closes.

Flow:
  1. `GameWatcher` (QThread) polls `pactl list sink-inputs` every 2 s.
     For each sink-input on SteelGame it pulls `application.name` and
     `application.process.binary` and emits the active set.
  2. `GameProfileManager` listens to the watcher's signal. When a new
     game enters the set:
       - Look up `settings["game_eq_bindings"][name]` (manual override).
       - Fall back to fuzzy-matching `name` against the bundled ASM
         preset filenames (`gui/presets/asm/game/*.json`).
       - If we find a match: snapshot the current Game EQ bands and
         send a `set-eq-channel` to the daemon with the preset's bands.
  3. When the last matched game leaves the set, restore the snapshot
     via another `set-eq-channel`.

Failure modes are silent — fuzzy match below the threshold, daemon
disconnected, missing presets — all leave the user's current EQ
untouched. The toggle in Settings is the single source of truth for
whether any of this runs.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .eq_presets import bundled_asm_dir, list_presets

log = logging.getLogger(__name__)

# Matching threshold for fuzzy game-name → preset-name. 0.6 = roughly
# "60 % overlap by SequenceMatcher" — generous enough to match
# "Apex" → "Apex Legends" but tight enough to reject unrelated
# names. Tune up if false positives bite.
_FUZZY_THRESHOLD = 0.62

# Poll cadence. 2 s feels live without being a noticeable load — pactl
# list sink-inputs is cheap (~5 ms typical).
_POLL_INTERVAL_MS = 2000

# Sink whose audio we'd ideally apply the Game EQ to. We *prefer*
# clients on SteelGame, but the watcher does NOT filter to it —
# many users keep their default sink as the headset and never
# manually route games into SteelGame. Filtering would silently
# drop those cases. We still note SteelGame membership in the
# emitted set so the orchestrator can warn when a matched game
# isn't routed where the EQ would actually take effect.
_GAME_SINK = "SteelGame"


def _normalise(name: str) -> str:
    """Lowercase + strip non-alphanumerics so 'Apex Legends' and
    'apex_legends.exe' compare equal-ish under SequenceMatcher."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _bundled_asm_index() -> dict[str, Path]:
    """Map normalised filename → file path for every bundled Game-
    channel ASM preset. Built once on demand; small enough (~400
    entries) that recomputing per scan would still be cheap, but
    caching keeps the watcher thread snappy."""
    out: dict[str, Path] = {}
    d = bundled_asm_dir("game")
    if not d.is_dir():
        return out
    for path in d.glob("*.json"):
        out[_normalise(path.stem)] = path
    return out


def match_asm_preset(game_name: str) -> str | None:
    """Return the display name of the ASM Game preset that best
    matches `game_name`, or None if none clears the threshold. The
    returned name is the one the EQ tab shows in its dropdown
    (i.e. with the `[ASM] ` prefix), so callers can pass it straight
    to the existing apply path."""
    if not game_name:
        return None
    target = _normalise(game_name)
    if not target:
        return None
    index = _bundled_asm_index()
    keys = list(index.keys())
    matches = difflib.get_close_matches(target, keys, n=1, cutoff=_FUZZY_THRESHOLD)
    if not matches:
        return None
    return f"[ASM] {index[matches[0]].stem}"


def find_preset_bands(preset_name: str) -> list[dict] | None:
    """Look up a preset by display name across the three sources
    (built-in / bundled ASM / user). Returns the bands list, or None
    if the name is unknown."""
    for entry in list_presets("game"):
        if entry.get("name") == preset_name:
            return list(entry.get("bands") or [])
    return None


# --------------------------------------------------------------- watcher


class GameWatcher(QThread):
    """Background thread that publishes the set of active candidate-
    game clients seen on PipeWire. Emits `games_changed(dict)` with
    the latest snapshot whenever it changes — keys are
    `application.name` strings, values are booleans for whether the
    client is on SteelGame. The orchestrator decides what to do."""

    games_changed = Signal(dict)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._stop = False
        self._last: tuple[tuple[str, bool], ...] = ()

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 — Qt slot signature
        if not shutil.which("pactl"):
            log.warning("pactl not on PATH; auto game-EQ disabled")
            return
        while not self._stop:
            scanned = self._scan()
            # Stable canonical key for change detection: dedupe + sort.
            canon = tuple(sorted(set(scanned)))
            if canon != self._last:
                self._last = canon
                # Convert to dict for the slot — duplicates collapse,
                # any "on_game" win wins (sorted True > False on bool).
                snapshot: dict[str, bool] = {}
                for name, on_game in scanned:
                    snapshot[name] = snapshot.get(name, False) or on_game
                self.games_changed.emit(snapshot)
            # msleep yields cooperatively so stop() flips promptly
            # rather than after a full 2-second wait.
            for _ in range(_POLL_INTERVAL_MS // 50):
                if self._stop:
                    return
                self.msleep(50)

    # Apps that aren't games but report sink-inputs we don't want to
    # match against. Skip anything whose binary or name lives in
    # this allow-list-of-skips. Browsers are auto-routed to Media
    # via the daemon already; the rest are noisy false positives
    # (system sounds, voice clients, etc).
    _IGNORED_APPS = frozenset({
        "Spotify", "Discord", "WEBRTC VoiceEngine", "Web Content",
        "Steam", "Steam Voice Settings",
    })
    _IGNORED_BINARIES = frozenset({
        "firefox", "firefox-bin", "chromium", "chromium-browser",
        "chrome", "google-chrome", "brave", "brave-browser",
        "spotify", "discord", "vesktop", "telegram-desktop",
        "obs", "OBS", "easyeffects", "pavucontrol", "qpwgraph",
    })

    @staticmethod
    def _scan() -> list[tuple[str, bool]]:
        """Read sink-inputs and return `(application.name, on_steel_game)`
        tuples for every active client that looks like it could be a
        game. We do NOT filter out clients that aren't on SteelGame —
        the user may not have routed games there, and a silent miss
        is the worst possible UX. The orchestrator decides whether
        to apply the EQ based on the on_steel_game flag.

        pactl's verbose listing groups one block per sink-input with
        a `Sink: <id>` header and an indented `Properties:` section.
        We identify SteelGame membership two ways — via `Sink: <id>`
        cross-referenced against `pactl list sinks short`, and via
        the `node.target` property if set. Either match wins."""
        try:
            sinks = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True, text=True, timeout=3,
            )
            r = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=4,
            )
        except Exception as e:
            log.debug("pactl probe failed: %s", e)
            return []
        if r.returncode != 0:
            return []

        # Build sink-id → name map from the short listing.
        # Format: "<id>\t<name>\t<module>\t<format>\t<state>"
        id_to_name: dict[str, str] = {}
        if sinks.returncode == 0:
            for line in sinks.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0].isdigit():
                    id_to_name[parts[0]] = parts[1]

        out: list[tuple[str, bool]] = []
        sink_id: str | None = None
        node_target: str | None = None
        app_name: str | None = None
        app_binary: str | None = None

        ignored_apps = GameWatcher._IGNORED_APPS
        ignored_bins = GameWatcher._IGNORED_BINARIES

        def flush() -> None:
            if not app_name:
                return
            if app_name in ignored_apps:
                return
            if app_binary and app_binary in ignored_bins:
                return
            on_game = False
            if sink_id and id_to_name.get(sink_id) == _GAME_SINK:
                on_game = True
            if node_target == _GAME_SINK:
                on_game = True
            out.append((app_name, on_game))

        for raw in r.stdout.splitlines():
            stripped = raw.strip()
            if stripped.startswith("Sink Input #"):
                flush()
                sink_id = None
                node_target = None
                app_name = None
                app_binary = None
                continue
            if stripped.startswith("Sink:"):
                rest = stripped[5:].strip()
                if rest.isdigit():
                    sink_id = rest
                continue
            if "=" in stripped:
                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip().strip('"')
                if key == "node.target":
                    node_target = value
                elif key == "application.name" and value:
                    app_name = value
                elif key == "application.process.binary" and value:
                    app_binary = value
        flush()
        return out


# ----------------------------------------------------- profile manager


class GameProfileManager(QObject):
    """Orchestrator that turns watcher events into EQ swaps. Keeps a
    snapshot of the user's pre-game Game-channel bands so the close-
    of-game restore returns to exactly that state — not 'flat', not
    a different preset the user happened to have selected before."""

    # Signal: (game_name, preset_name, on_steel_game). preset_name
    # is None when nothing matched. on_steel_game is False when the
    # detected game isn't routed to SteelGame — the EQ won't take
    # effect until the user moves the stream. UI listens to this
    # to render the live "Currently detected" status line.
    detected_changed = Signal(str, object, bool)

    def __init__(
        self,
        daemon_client,
        settings: dict,
        eq_state: dict[str, list[dict]],
        parent=None,
    ):
        """`eq_state` is a reference to the EqualizerTab's
        `_bands_by_channel`, used as a live mirror of the daemon's
        per-channel EQ. We read from it at game-start time to take
        the snapshot; we never mutate it (the daemon's broadcast
        events do that)."""
        super().__init__(parent)
        self._daemon = daemon_client
        self._settings = settings
        self._eq_state = eq_state
        self._snapshot_bands: list[dict] | None = None
        self._active_preset: str | None = None
        self._current_games: dict[str, bool] = {}
        self._last_seen: dict[str, bool] = {}

    def latest_seen(self) -> dict[str, bool]:
        """Snapshot of the most recent watcher tick. Used by the
        Settings UI to populate the manual-binding dropdown with
        currently-active app names so the user picks the exact
        string PipeWire reports rather than typing it."""
        return dict(self._last_seen)

    def on_games_changed(self, games: dict) -> None:
        """Watcher tick: react to the dict of {app.name: on_steel_game}.
        Always cache the latest seen list (UI uses it). EQ swaps
        only run when the toggle is on."""
        self._last_seen = dict(games)
        # Emit a "detected" event for UI feedback regardless of
        # whether the toggle is on — the user wants to see what's
        # being seen even before they enable the auto-switch.
        if games:
            top = sorted(games.keys())[0]
            preset = self._resolve_preset(games)
            on_game = games.get(top, False)
            self.detected_changed.emit(top, preset, on_game)
        else:
            self.detected_changed.emit("", None, False)

        if not self._settings.get("auto_game_eq_enabled", False):
            self._current_games = dict(games)
            return
        was_empty = not self._current_games
        is_empty = not games
        self._current_games = dict(games)

        if was_empty and not is_empty:
            self._enter(games)
        elif is_empty and not was_empty:
            self._exit()
        elif not is_empty:
            self._switch(games)

    # ---------------------------------------------------- transitions

    def _resolve_preset(self, games) -> str | None:
        """Return the preset name to apply for any of `games`, or
        None if nothing maps. Manual bindings (ordered list, first
        match wins) take precedence over ASM fuzzy matches.

        Resolution order:
          1. Walk the ordered binding list. For each entry whose
             `game` matches an active game name, return its preset.
          2. Fall back to fuzzy-matching every active game against
             the bundled ASM library; return the first hit."""
        bindings = self._settings.get("game_eq_bindings") or []
        active = set(games)
        if isinstance(bindings, list):
            for entry in bindings:
                if not isinstance(entry, dict):
                    continue
                if entry.get("game") in active:
                    return entry.get("preset") or None
        else:
            # Legacy dict shape — load() migrates on next save, but
            # be defensive for in-memory dicts that pre-date this.
            for name in sorted(active):
                override = bindings.get(name)
                if override:
                    return override
        for name in sorted(active):
            asm = match_asm_preset(name)
            if asm:
                return asm
        return None

    def _enter(self, games) -> None:
        preset_name = self._resolve_preset(games)
        if not preset_name:
            log.info("Auto game-EQ: no preset match for %s", games)
            return
        bands = find_preset_bands(preset_name)
        if not bands:
            log.warning("Auto game-EQ: preset %r vanished from disk", preset_name)
            return
        # Snapshot the user's current Game EQ before we overwrite it.
        snapshot = self._eq_state.get("game")
        if isinstance(snapshot, list):
            self._snapshot_bands = deepcopy(snapshot)
        else:
            self._snapshot_bands = None
        self._active_preset = preset_name
        log.info("Auto game-EQ: applying %r for %s", preset_name, games)
        self._daemon.send_command(
            "set-eq-channel", channel="game", bands=bands,
        )

    def _switch(self, games) -> None:
        new_preset = self._resolve_preset(games)
        if new_preset == self._active_preset or new_preset is None:
            return
        bands = find_preset_bands(new_preset)
        if not bands:
            return
        log.info(
            "Auto game-EQ: switching from %r to %r (games=%s)",
            self._active_preset, new_preset, games,
        )
        self._active_preset = new_preset
        self._daemon.send_command(
            "set-eq-channel", channel="game", bands=bands,
        )

    def _exit(self) -> None:
        if self._snapshot_bands is None:
            self._active_preset = None
            return
        log.info("Auto game-EQ: restoring user's pre-game Game EQ")
        self._daemon.send_command(
            "set-eq-channel", channel="game",
            bands=self._snapshot_bands,
        )
        self._snapshot_bands = None
        self._active_preset = None

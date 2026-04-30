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

# Name of the sink we filter on. Anything not playing here is ignored
# (a YouTube tab on Media doesn't trigger a game profile, for
# instance).
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
    """Background thread that publishes the set of active games on
    SteelGame. Emits `games_changed(set[str])` with the union of
    `application.name` values whenever the set changes — both on
    addition and removal."""

    games_changed = Signal(set)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._stop = False
        self._last: frozenset[str] = frozenset()

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 — Qt slot signature
        if not shutil.which("pactl"):
            log.warning("pactl not on PATH; auto game-EQ disabled")
            return
        while not self._stop:
            current = frozenset(self._scan())
            if current != self._last:
                self._last = current
                self.games_changed.emit(set(current))
            # msleep yields cooperatively so stop() flips promptly
            # rather than after a full 2-second wait.
            for _ in range(_POLL_INTERVAL_MS // 50):
                if self._stop:
                    return
                self.msleep(50)

    @staticmethod
    def _scan() -> list[str]:
        """Read sink-inputs and return every `application.name`
        whose target sink is SteelGame. pactl's verbose listing
        groups one block per sink-input, with a `Sink: NNN` header
        line and an indented `Properties:` section underneath. We
        identify membership in SteelGame two ways — via `Sink: <id>`
        cross-referenced against `pactl list sinks short`, and via
        the `node.target` property if it's set. Either match wins."""
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

        out: list[str] = []
        sink_id: str | None = None
        node_target: str | None = None
        app_name: str | None = None

        def flush() -> None:
            on_game = False
            if sink_id and id_to_name.get(sink_id) == _GAME_SINK:
                on_game = True
            if node_target == _GAME_SINK:
                on_game = True
            if on_game and app_name:
                out.append(app_name)

        for raw in r.stdout.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if stripped.startswith("Sink Input #"):
                flush()
                sink_id = None
                node_target = None
                app_name = None
                continue
            # Sink id header line — top-level (single tab indent).
            if stripped.startswith("Sink:"):
                rest = stripped[5:].strip()
                if rest.isdigit():
                    sink_id = rest
                continue
            # Properties section: `\t\tkey = "value"`.
            if "=" in stripped:
                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip().strip('"')
                if key == "node.target":
                    node_target = value
                elif key == "application.name" and value:
                    app_name = value
        flush()
        return out


# ----------------------------------------------------- profile manager


class GameProfileManager(QObject):
    """Orchestrator that turns watcher events into EQ swaps. Keeps a
    snapshot of the user's pre-game Game-channel bands so the close-
    of-game restore returns to exactly that state — not 'flat', not
    a different preset the user happened to have selected before."""

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
        self._current_games: set[str] = set()

    def on_games_changed(self, games: set) -> None:
        """Watcher tick: react to changes in the set of running
        games on SteelGame. Three transitions matter:
          - empty → non-empty: apply the matched preset (if any).
          - non-empty → empty: restore the user's pre-game EQ.
          - swap: re-match against the new top game (rare)."""
        if not self._settings.get("auto_game_eq_enabled", False):
            # Toggle off — don't touch EQ even if games come and go.
            self._current_games = set(games)
            return
        was_empty = not self._current_games
        is_empty = not games
        self._current_games = set(games)

        if was_empty and not is_empty:
            self._enter(games)
        elif is_empty and not was_empty:
            self._exit()
        elif not is_empty:
            # Same scenario as enter, but we already have a snapshot.
            # Re-evaluate the match in case the new top game maps to
            # a different preset.
            self._switch(games)

    # ---------------------------------------------------- transitions

    def _resolve_preset(self, games: set) -> str | None:
        """Return the preset name to apply for any of `games`, or
        None if nothing maps. Manual bindings win over fuzzy ASM
        matches; among the games in the set, the first one that
        resolves is chosen (ordering is irrelevant since usually
        only one game is on SteelGame at a time)."""
        bindings: dict = self._settings.get("game_eq_bindings") or {}
        for name in sorted(games):  # deterministic
            override = bindings.get(name)
            if override:
                return override
            asm = match_asm_preset(name)
            if asm:
                return asm
        return None

    def _enter(self, games: set) -> None:
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

    def _switch(self, games: set) -> None:
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

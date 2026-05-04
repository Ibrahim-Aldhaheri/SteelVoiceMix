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
import shlex
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .eq_presets import bundled_asm_dir, list_presets
from .settings import save as save_settings

log = logging.getLogger(__name__)

# Matching threshold for fuzzy game-name → preset-name. 0.6 = roughly
# "60 % overlap by SequenceMatcher" — generous enough to match
# "Apex" → "Apex Legends" but tight enough to reject unrelated
# names. Tune up if false positives bite.
_FUZZY_THRESHOLD = 0.62

# Poll cadence. 2 s feels live without being a noticeable load — pactl
# list sink-inputs is cheap (~5 ms typical).
_POLL_INTERVAL_MS = 2000

# How many consecutive empty-games ticks we tolerate before firing the
# exit (and thus a chain respawn back to the user's snapshot). Games
# briefly drop their sink-input during loading screens, cutscenes,
# 3D-positional updates, and audio-focus changes; without this grace
# period each transient blip cycles enter→exit→enter and the EQ chain
# respawns every cycle, audibly glitching the sound.
_EXIT_GRACE_TICKS = 2

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
        # Our own EQ test-audio playback. pw-cat fuzzy-matches
        # 'Podcast' at SequenceMatcher ratio ~0.67 (shared p/c/a/t
        # characters), which auto-applied the Podcast preset every
        # time the user pressed Play on a noise/sweep clip. Listing
        # the app name as well as the binary defends against
        # PipeWire variants that surface the friendlier name.
        "pw-cat", "PipeWire pw-cat",
        # Browsers — `application.process.binary` is often empty or
        # mangled when the browser is Flatpak / Snap / sandboxed
        # (e.g. "chromium-bwrap"), so the binary-list check from
        # _IGNORED_BINARIES misses. Listing the friendly application
        # .name strings catches them before the fuzzy-match step
        # has a chance to false-positive on a game preset that
        # happens to share characters (e.g. "Chromium" → fuzzy
        # match on a Horizon preset reported in the wild).
        "Chromium", "Chromium Browser",
        "Firefox", "Mozilla Firefox",
        "Chrome", "Google Chrome",
        "Brave", "Brave Browser", "Brave-browser",
        "Vivaldi", "Opera", "Microsoft Edge",
        "Tor Browser", "LibreWolf", "Zen Browser",
        # Other common audio-producing non-game apps the watcher
        # sees on SteelGame when the user hasn't routed selectively.
        "VLC media player", "VLC", "mpv", "MPV",
        "OBS", "OBS Studio",
        "Telegram Desktop", "Vesktop", "WhatsApp",
        "ALSA plug-in [pulseaudio]", "ALSA plug-in",
    })
    _IGNORED_BINARIES = frozenset({
        "firefox", "firefox-bin", "chromium", "chromium-browser",
        "chrome", "google-chrome", "brave", "brave-browser",
        "spotify", "discord", "vesktop", "telegram-desktop",
        "obs", "OBS", "easyeffects", "pavucontrol", "qpwgraph",
        # Audio test / playback tools — they're not games even when
        # they happen to fuzzy-match an ASM preset name.
        "pw-cat", "pw-play", "paplay", "aplay", "ffplay", "mpv",
        "vlc", "mplayer",
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
            sink_name = id_to_name.get(sink_id) if sink_id else None
            on_game = False
            # Multiple ways to recognise SteelGame routing:
            #  1. Sink id resolves to "SteelGame".
            #  2. node.target property is "SteelGame".
            #  3. Sink id resolves to a name CONTAINING "SteelGame"
            #     (covers EQ/surround chain intermediates like
            #     SteelGameEQ that some Pipewire builds expose as
            #     virtual sinks rather than effect nodes).
            if sink_name == _GAME_SINK:
                on_game = True
            elif sink_name and _GAME_SINK in sink_name:
                on_game = True
            if node_target == _GAME_SINK:
                on_game = True
            # Per-scan log fires every 2 s × per sink-input on
            # SteelGame — easily 90 lines/min with one game running.
            # DEBUG-only so the journal stays readable; bring it back
            # via STEELVOICEMIX_DEBUG=1.
            log.debug(
                "Game-watcher: app=%r binary=%r sink_id=%s sink_name=%r "
                "node_target=%r → on_game=%s",
                app_name, app_binary, sink_id, sink_name,
                node_target, on_game,
            )
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

    # Signal: applied preset name, or empty string when no auto
    # preset is currently engaged (snapshot restored). The EQ tab
    # listens to lock the Game-channel controls and surface the
    # active preset in a banner.
    applied_changed = Signal(str)

    # Signal: bands list that the EQ tab should immediately load
    # into its visible state. Emitted alongside applied_changed so
    # the UI updates instantly without waiting for the daemon's
    # eq-bands-changed broadcast roundtrip — preset name AND the
    # actual band values land at the same time.
    bands_to_load = Signal(list)

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
        # Rehydrate any persisted snapshot from a prior session — used
        # when the GUI is restarted (or the system suspended) before
        # the watcher's exit grace fired and consumed it. Without
        # this, a "close game → suspend immediately" pattern left the
        # daemon's persisted Game EQ stuck on the auto-applied game
        # preset with nothing to restore from on resume.
        persisted_snapshot = settings.get("auto_game_eq_snapshot_bands") or []
        persisted_active = settings.get("auto_game_eq_active_preset") or ""
        self._snapshot_bands: list[dict] | None = (
            list(persisted_snapshot) if persisted_snapshot else None
        )
        self._active_preset: str | None = persisted_active or None
        self._current_games: dict[str, bool] = {}
        self._last_seen: dict[str, bool] = {}
        # Counts consecutive watcher ticks where no candidate-game
        # client was seen. Used to gate _exit so a transient empty
        # tick (loading screen, cutscene, audio refocus) doesn't
        # respawn the EQ chain unnecessarily.
        self._consecutive_empty_ticks: int = 0
        if self._snapshot_bands is not None:
            log.info(
                "Auto game-EQ: rehydrated stale session — preset=%r, "
                "snapshot bands=%d. Will restore on first empty watcher "
                "tick.",
                self._active_preset,
                len(self._snapshot_bands),
            )

    def latest_seen(self) -> dict[str, bool]:
        """Snapshot of the most recent watcher tick. Used by the
        Settings UI to populate the manual-binding dropdown with
        currently-active app names so the user picks the exact
        string PipeWire reports rather than typing it."""
        return dict(self._last_seen)

    def on_games_changed(self, games: dict) -> None:
        """Watcher tick: react to {app.name: on_steel_game}. Always
        cache the latest seen list (UI uses it). EQ swaps only run
        when the toggle is on, and we *always* reconcile against
        desired state — not just on edge transitions — so flipping
        the toggle on while a game is already running still loads
        the matching preset (the previous edge-only logic missed
        that case)."""
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

        self._current_games = dict(games)
        self._reconcile()

    def reconcile(self) -> None:
        """Public re-entry point. Called when the toggle flips so
        the manager re-evaluates state immediately (the watcher's
        next tick is up to 2 s away)."""
        self._reconcile()

    def _reconcile(self) -> None:
        """Bring auto-applied preset into agreement with desired
        state, computed from `_current_games` + the toggle."""
        auto_on = self._settings.get("auto_game_eq_enabled", False)
        games = self._current_games
        # Track consecutive empty-games ticks so we can grace-period
        # the exit. A single empty tick (game's sink-input disappeared
        # for half a second during a cutscene) shouldn't trigger a
        # chain respawn — that's the audible "jamming" the user
        # reported when game audio shifts position. Reset to 0 on
        # any tick where we DID see games.
        if games:
            self._consecutive_empty_ticks = 0
        else:
            self._consecutive_empty_ticks += 1
        # Per-tick reconcile log — DEBUG only so the journal isn't
        # spammed every 2 s. The actual transitions (_enter / _switch
        # / _exit) still log at INFO so users see preset changes.
        log.debug(
            "Auto game-EQ reconcile: auto_on=%s games=%s active_preset=%r "
            "empty_ticks=%d",
            auto_on, list(games.keys()), self._active_preset,
            self._consecutive_empty_ticks,
        )
        if not auto_on:
            if self._active_preset is not None:
                self._exit()
            return
        if games:
            target = self._resolve_preset(games)
            if target is None:
                # Couldn't match anything — leave any active preset
                # in place (we don't want to thrash) but log.
                return
            if self._active_preset is None:
                self._enter(games)
            elif target != self._active_preset:
                self._switch(games)
        else:
            # Grace period: only declare the games gone after
            # _EXIT_GRACE_TICKS consecutive empty ticks. Until then,
            # the active preset stays put — sink-input blips during
            # loading screens / cutscenes don't cycle enter→exit→enter
            # and the EQ chain doesn't respawn on every blip.
            if (
                self._active_preset is not None
                and self._consecutive_empty_ticks >= _EXIT_GRACE_TICKS
            ):
                self._exit()

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
        log.info("Auto game-EQ enter: games=%s → preset=%r", games, preset_name)
        if not preset_name:
            return
        bands = find_preset_bands(preset_name)
        if not bands:
            log.warning(
                "Auto game-EQ: preset %r matched but find_preset_bands "
                "returned no bands — preset list source mismatch?",
                preset_name,
            )
            return
        # Snapshot the user's current Game EQ before we overwrite it.
        snapshot = self._eq_state.get("game")
        if isinstance(snapshot, list):
            self._snapshot_bands = deepcopy(snapshot)
        else:
            self._snapshot_bands = None
        # Capture the pre-game preset name so the exit notification
        # can say what we're restoring back to. The orchestrator only
        # sees an opaque snapshot of bands; the EQ tab tracks the
        # named preset and we read it through the same settings dict
        # that the EQ tab persists to.
        self._pre_active_preset = self._settings.get(
            "auto_game_eq_pre_preset", ""
        ) or self._active_preset_at_enter()
        self._settings["auto_game_eq_pre_preset"] = self._pre_active_preset
        self._active_preset = preset_name
        self._persist_runtime_state()
        log.info(
            "Auto game-EQ: sending set-eq-channel game with %d bands "
            "(first band: %s)",
            len(bands), bands[0] if bands else None,
        )
        self._daemon.send_command(
            "set-eq-channel", channel="game", bands=bands,
        )
        self.bands_to_load.emit(bands)
        self.applied_changed.emit(preset_name)
        # Notification: "Game name → New preset". Picks the first
        # detected game name as the user-facing label — the watcher
        # can see multiples but typically one is the actually-running
        # game and the others are launchers (Steam, etc.).
        game_label = next(iter(games), "") if games else ""
        self._notify(
            f"{game_label} → {preset_name}" if game_label else preset_name
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
        self._persist_runtime_state()
        self._daemon.send_command(
            "set-eq-channel", channel="game", bands=bands,
        )
        self.bands_to_load.emit(bands)
        self.applied_changed.emit(new_preset)
        game_label = next(iter(games), "") if games else ""
        self._notify(
            f"{game_label} → {new_preset}" if game_label else new_preset
        )

    def _exit(self) -> None:
        if self._snapshot_bands is None:
            self._active_preset = None
            self._settings["auto_game_eq_pre_preset"] = ""
            self._persist_runtime_state()
            self.applied_changed.emit("")
            return
        log.info("Auto game-EQ: restoring user's pre-game Game EQ")
        snapshot = self._snapshot_bands
        self._daemon.send_command(
            "set-eq-channel", channel="game",
            bands=snapshot,
        )
        self.bands_to_load.emit(snapshot)
        # Notification: name what we're restoring to so the user
        # gets confirmation that their pre-game tuning is back, not
        # just "EQ changed".
        pre = self._settings.get("auto_game_eq_pre_preset", "") or "default"
        self._notify(f"Restored: {pre}")
        self._snapshot_bands = None
        self._active_preset = None
        self._settings["auto_game_eq_pre_preset"] = ""
        self._persist_runtime_state()
        self.applied_changed.emit("")

    def _active_preset_at_enter(self) -> str:
        """Try to read the user's currently-selected named preset from
        settings.json — the EQ tab persists the active per-channel
        preset under `eq_active_preset_by_channel` (best-effort key
        name; fallback to empty string)."""
        active = self._settings.get("eq_active_preset_by_channel") or {}
        if isinstance(active, dict):
            return str(active.get("game", ""))
        return ""

    def _notify(self, body: str) -> None:
        """Emit a desktop notification via notify-send when the user
        has Auto Game-EQ notifications enabled. Same transport the
        Rust daemon uses for connect/disconnect — surfaces in the
        system notification centre, not a transient Qt toast."""
        if not self._settings.get("notify_auto_game_eq", True):
            return
        if not shutil.which("notify-send"):
            return
        try:
            subprocess.Popen(
                [
                    "notify-send",
                    "--app-name=SteelVoiceMix",
                    "--icon=steelvoicemix",
                    "--expire-time=4000",
                    "Auto EQ",
                    body,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.debug("notify-send failed: %s", e)

    def _persist_runtime_state(self) -> None:
        """Mirror the in-memory snapshot/active-preset to settings.json
        so a GUI restart (or a suspend that lets the watcher's exit
        grace expire after an unexpected GUI restart) can pick up
        where we left off and still restore the user's pre-game EQ.

        Cheap — settings.save is a small JSON write — and only fires
        on the rare _enter / _switch / _exit transitions, never per
        watcher tick."""
        self._settings["auto_game_eq_active_preset"] = self._active_preset or ""
        self._settings["auto_game_eq_snapshot_bands"] = (
            list(self._snapshot_bands) if self._snapshot_bands else []
        )
        try:
            save_settings(self._settings)
        except Exception as e:  # don't crash the auto-EQ flow on a settings I/O blip
            log.warning("Auto game-EQ: persist runtime state failed: %s", e)

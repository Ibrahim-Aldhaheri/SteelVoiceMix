#!/usr/bin/env python3
"""CLI entry-point for SteelVoiceMix actions that the GUI exposes
but a user might want to bind to a system-wide keyboard shortcut.

Right now the only subcommand is `sink cycle` — advances the
system default audio sink between the loaded SteelVoiceMix sinks
(Game / Chat / Media / HDMI), honouring the per-user exclude
list stored in `~/.config/steelvoicemix/settings.json`.

Bind in your DE's keyboard settings:

    KDE   → System Settings → Shortcuts → Custom Shortcuts → New
            → command: `steelvoicemix-cli sink cycle`
    GNOME → Settings → Keyboard → Custom Shortcuts → +
            → command: `steelvoicemix-cli sink cycle`

Exit codes:
  0 — sink advanced (or already on the only candidate)
  1 — no eligible SteelVoiceMix sinks loaded
  2 — usage error
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_settings() -> dict:
    """Read settings.json without importing PySide6 — keeps the
    CLI dependency-light so it runs without a display, in scripts,
    on headless servers etc."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = Path(base) / "steelvoicemix" / "settings.json"
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cmd_sink_cycle() -> int:
    # Importing gui.sink_cycle pulls PySide6 transitively (the gui
    # package's __init__ doesn't, but Settings does). To stay
    # GUI-free, inline the cycle logic here.
    import shutil
    import subprocess

    if not shutil.which("pactl"):
        print("error: pactl not found on PATH", file=sys.stderr)
        return 1

    settings = _load_settings()
    excluded = set(settings.get("default_sink_cycle_exclude") or [])
    preferred = ("SteelGame", "SteelChat", "SteelMedia", "SteelHDMI")

    # List loaded sinks.
    try:
        r = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        print(f"error: pactl list failed: {e}", file=sys.stderr)
        return 1
    available = [
        line.split("\t")[1]
        for line in r.stdout.splitlines()
        if "\t" in line and len(line.split("\t")) >= 2
    ]
    candidates = [
        s for s in preferred
        if s in available and s not in excluded
    ]
    if not candidates:
        print(
            "error: no eligible SteelVoiceMix sinks loaded "
            f"(available={available!r}, excluded={sorted(excluded)!r})",
            file=sys.stderr,
        )
        return 1

    # Find current default.
    try:
        r = subprocess.run(
            ["pactl", "info"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        print(f"error: pactl info failed: {e}", file=sys.stderr)
        return 1
    prev = ""
    for line in r.stdout.splitlines():
        if line.startswith("Default Sink:"):
            prev = line.split(":", 1)[1].strip()
            break

    if prev in candidates:
        idx = (candidates.index(prev) + 1) % len(candidates)
    else:
        idx = 0
    new = candidates[idx]
    if new == prev:
        print(f"already on {new}")
        return 0

    try:
        r = subprocess.run(
            ["pactl", "set-default-sink", new],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        print(f"error: pactl set-default-sink failed: {e}", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print(
            f"error: pactl set-default-sink {new} failed: {r.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    print(f"{prev or '?'} → {new}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "sink" and argv[2] == "cycle":
        return _cmd_sink_cycle()
    print(
        "usage: steelvoicemix-cli sink cycle\n\n"
        "Advances the system default audio sink between the loaded\n"
        "SteelVoiceMix virtual sinks. Bind to a global keyboard\n"
        "shortcut via your DE's keyboard settings.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

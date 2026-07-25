#!/usr/bin/env python3
"""Validate the bundled preset tree and the runtime-alias map.

This is the CI trust boundary for third-party content. Everything under
`gui/presets/asm/` is pulled from `loteran/Arctis-Sound-Manager` by
`scripts/fetch_asm_presets.py`, a repo we don't control. Checking that
the JSON merely *parses* (what the workflow used to do) lets a
compromised or simply broken upstream ship band values that are absurd,
non-finite, or the wrong shape entirely, and lets a runaway refresh
dump thousands of files into the PR.

So we check three things:

  1. **Shape** — every preset is an object with a non-empty string
     `name` and exactly NUM_BANDS bands, each band carrying finite
     numeric `freq`/`q`/`gain` and a known filter `type`.
  2. **Bounds** — values sit inside the same ranges the Rust daemon
     clamps to (src/audio.rs). The daemon is the authority and will
     clamp regardless; failing here instead means hostile values are
     visible in the PR diff as an error rather than silently squashed
     at runtime.
  3. **Volume** — a single refresh can't add more than MAX_PRESETS
     files or exceed MAX_TOTAL_BYTES, so a flood or a decompression
     bomb fails CI instead of landing in the shipped RPM.

Also verifies every runtime-alias *value* names a preset that actually
exists — the class of bug that silently broke auto Game-EQ for
Baldur's Gate 3 (alias pointed at the filename stem, not the display
name).

Exit 0 when everything is good, 1 with a per-problem report otherwise.
Run from anywhere; paths resolve relative to the repo root.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = ROOT / "gui" / "presets" / "asm"
ALIASES_PATH = ROOT / "gui" / "presets" / "runtime_aliases.json"

CHANNELS = ("game", "chat", "mic")
NUM_BANDS = 10

# Mirror of the daemon's clamps in src/audio.rs. Kept a little wider
# than the UI's ±12 dB so a legitimately hot ASM preset isn't rejected,
# but tight enough that garbage stands out.
FREQ_RANGE = (10.0, 24000.0)
Q_RANGE = (0.05, 40.0)
GAIN_RANGE = (-30.0, 30.0)

# Filter types we know how to render (see _SONAR_TYPE_MAP and
# filter_chain.rs band_label).
VALID_TYPES = frozenset({
    "peaking", "lowpass", "highpass", "lowshelf",
    "highshelf", "bandpass", "notch", "allpass",
})

# Volume ceilings for a single refresh.
MAX_PRESETS = 2000
MAX_TOTAL_BYTES = 32 * 1024 * 1024

# Control and bidirectional-override characters. A hostile display name
# using RTL overrides can make a PR diff read as something other than
# what it is.
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f‎‏‪-‮⁦-⁩]")


def _check_band(band: object, idx: int) -> list[str]:
    """Return a list of problems with a single band entry."""
    if not isinstance(band, dict):
        return [f"band {idx}: not an object"]
    problems: list[str] = []
    for key, (lo, hi) in (
        ("freq", FREQ_RANGE),
        ("q", Q_RANGE),
        ("gain", GAIN_RANGE),
    ):
        raw = band.get(key)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            problems.append(f"band {idx}: {key}={raw!r} is not numeric")
            continue
        val = float(raw)
        if not math.isfinite(val):
            problems.append(f"band {idx}: {key} is not finite ({val!r})")
        elif not (lo <= val <= hi):
            problems.append(
                f"band {idx}: {key}={val} outside [{lo}, {hi}]"
            )
    btype = band.get("type")
    if btype not in VALID_TYPES:
        problems.append(f"band {idx}: unknown type {btype!r}")
    return problems


def _check_preset(path: Path) -> list[str]:
    """Return a list of problems with one preset file."""
    try:
        # parse_constant fires on NaN/Infinity/-Infinity, which Python's
        # json accepts by default but which are not valid JSON and blow
        # up a biquad downstream.
        data = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda c: (_ for _ in ()).throw(
                ValueError(f"non-JSON constant {c!r}")
            ),
        )
    except (OSError, ValueError) as e:
        return [f"unreadable/invalid JSON: {e}"]

    if not isinstance(data, dict):
        return ["top level is not an object"]

    problems: list[str] = []
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"missing/empty 'name' (got {name!r})")
    elif _BAD_CHARS.search(name):
        problems.append(
            "'name' contains control or bidi-override characters"
        )

    bands = data.get("bands")
    if not isinstance(bands, list):
        problems.append(f"'bands' is not a list (got {type(bands).__name__})")
    elif len(bands) != NUM_BANDS:
        problems.append(f"expected {NUM_BANDS} bands, got {len(bands)}")
    else:
        for i, band in enumerate(bands):
            problems.extend(_check_band(band, i))
    return problems


def _check_aliases(known_names: dict[str, set[str]]) -> list[str]:
    """Every alias value must name a preset that exists in its channel."""
    if not ALIASES_PATH.exists():
        return []
    try:
        data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{ALIASES_PATH.name}: invalid JSON: {e}"]
    problems: list[str] = []
    for channel in CHANNELS:
        table = data.get(channel)
        if not isinstance(table, dict):
            continue
        valid = known_names.get(channel, set())
        if not valid:
            continue
        for key, value in sorted(table.items()):
            if not isinstance(value, str):
                problems.append(
                    f"{channel}: alias {key!r} value is not a string"
                )
            elif value not in valid:
                problems.append(
                    f"{channel}: alias {key!r} -> {value!r} names no "
                    f"existing preset (display-name mismatch?)"
                )
    return problems


def main() -> int:
    if not PRESETS_DIR.is_dir():
        print(f"No preset directory at {PRESETS_DIR} — nothing to validate.")
        return 0

    failures: list[str] = []
    total_files = 0
    total_bytes = 0
    known_names: dict[str, set[str]] = {}

    for channel in CHANNELS:
        d = PRESETS_DIR / channel
        if not d.is_dir():
            continue
        names: set[str] = set()
        for path in sorted(d.glob("*.json")):
            total_files += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
            problems = _check_preset(path)
            if problems:
                rel = path.relative_to(ROOT)
                failures.extend(f"{rel}: {p}" for p in problems)
            else:
                names.add(json.loads(path.read_text(encoding="utf-8"))["name"])
        known_names[channel] = names
        print(f"  {channel}: {len(names)} valid presets")

    failures.extend(_check_aliases(known_names))

    if total_files > MAX_PRESETS:
        failures.append(
            f"preset count {total_files} exceeds cap {MAX_PRESETS} — "
            f"refusing a refresh this large"
        )
    if total_bytes > MAX_TOTAL_BYTES:
        failures.append(
            f"preset tree is {total_bytes} bytes, over the "
            f"{MAX_TOTAL_BYTES}-byte cap"
        )

    print(f"  total: {total_files} files, {total_bytes} bytes")

    if failures:
        print(f"\n{len(failures)} problem(s) found:\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("All presets and aliases valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

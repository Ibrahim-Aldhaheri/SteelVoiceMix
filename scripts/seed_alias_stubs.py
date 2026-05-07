#!/usr/bin/env python3
"""Seed runtime_aliases.json with default stubs for any bundled ASM
preset that doesn't yet have an alias entry.

Why this exists: when ASM ships a new game preset, our auto Game-EQ
matcher can usually fuzzy-match the runtime application.name to the
new preset's filename. But fuzzy match is a fallback — explicit
aliases are faster and immune to false positives. This script writes
a baseline alias for every new preset:

    "<Display Name>": "<Display Name>"

— mapping the preset's display name to itself. The maintainer can
refine each entry afterward, e.g. adding `.exe` variants:

    "ApexLegends.exe": "Apex Legends"

Existing manual aliases (e.g. `r6.exe` → `Rainbow Six Siege`) are
NEVER overwritten. The script is idempotent — running it twice
produces the same file the second time.

Designed to run inside a GitHub Action immediately after
`scripts/fetch_asm_presets.py`. See `.github/workflows/refresh-asm-presets.yml`.

Usage:
  python3 scripts/seed_alias_stubs.py [--dry-run]

Exit 0 always; the script never fails the build. Output to stdout
includes a summary the workflow can grep for the "anything changed?"
decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALIASES_PATH = ROOT / "gui" / "presets" / "runtime_aliases.json"
PRESETS_BUNDLE = ROOT / "gui" / "presets" / "asm"

# Channels we seed. ASM ships game / chat / mic; only `game` is
# auto-matched today, but seeding the others now is cheap and ready
# for when the chat/mic auto-match paths land.
CHANNELS = ("game", "chat", "mic")


def load_aliases() -> dict:
    if not ALIASES_PATH.exists():
        return {"version": 1, "comment": "auto-seeded; refine manually."}
    with open(ALIASES_PATH) as f:
        return json.load(f)


def list_preset_displays(channel: str) -> list[str]:
    """Display names for all bundled presets in a channel."""
    d = PRESETS_BUNDLE / channel
    if not d.is_dir():
        return []
    out: list[str] = []
    for path in sorted(d.glob("*.json")):
        try:
            with open(path) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        name = payload.get("name")
        if isinstance(name, str) and name:
            out.append(name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change but don't write the file",
    )
    args = parser.parse_args()

    data = load_aliases()
    total_added = 0
    per_channel: dict[str, int] = {}

    for channel in CHANNELS:
        existing = data.get(channel)
        if not isinstance(existing, dict):
            existing = {}
            data[channel] = existing
        # Build a case-insensitive set of keys already present so we
        # don't overwrite a manual alias whose key happens to differ
        # from the preset name only by case (e.g. user wrote
        # `apex legends` and the new preset is `Apex Legends`).
        existing_lower = {k.lower() for k in existing.keys()}
        added_here = 0
        for display in list_preset_displays(channel):
            # Default stub: alias the preset's display name to itself.
            # Manual aliases (e.g. `EldenRing.exe → Elden Ring`) are
            # NOT overwritten thanks to the two skip-checks below.
            stub_value = display
            if display.lower() in existing_lower:
                continue
            if stub_value in existing.values():
                # Some other key already maps to this preset — the
                # manual entry is preferred over a generic stub.
                continue
            existing[display] = stub_value
            added_here += 1
            total_added += 1
        per_channel[channel] = added_here
        if added_here:
            print(f"  + {channel}: {added_here} stub aliases")

    if total_added == 0:
        print("No new aliases needed.")
        return 0

    if args.dry_run:
        print(f"Would add {total_added} stubs (dry-run; not writing).")
        return 0

    # Sort each channel's keys alphabetically for stable diffs in PRs.
    # Manual edits get re-sorted on next run too — accept that minor
    # churn in exchange for predictable diff output.
    for channel in CHANNELS:
        if isinstance(data.get(channel), dict):
            data[channel] = dict(sorted(data[channel].items()))

    ALIASES_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {total_added} new stubs to {ALIASES_PATH.relative_to(ROOT)}")
    print("Per-channel:", per_channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())

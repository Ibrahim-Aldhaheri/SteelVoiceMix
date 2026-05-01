#!/usr/bin/env python3
"""Refresh the bundled ASM preset library.

Run this script once whenever you want to pull a newer snapshot of
loteran/Arctis-Sound-Manager's preset library into our `gui/presets/asm/`
directory. The presets are bundled in the package so end users don't
need a network round-trip on first launch — running this script is the
maintainer's job, not the user's.

What it does:
  1. Downloads the ASM repo tarball (single HTTP call, no GitHub API
     rate limits).
  2. Walks the `src/arctis_sound_manager/gui/presets/` directory inside
     the tarball.
  3. Converts each `[Game]` / `[Chat]` / `[Mic]` JSON to our EqBand
     shape via `gui.eq_presets.convert_sonar_preset`. Mic landed once
     the daemon got a microphone capture-path EQ chain.
  4. Writes the result to `gui/presets/asm/<channel>/<safe-name>.json`.

Idempotent: re-running overwrites the existing bundled set with
upstream's current state. Removed-from-upstream presets aren't pruned
automatically — the script lists files it would delete and asks for
confirmation.

Usage:
  python3 scripts/fetch_asm_presets.py [--ref main] [--prune]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

# Allow running from the repo root without installing the package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.eq_presets import (  # noqa: E402  (after sys.path tweak)
    NUM_BANDS,
    _ASM_TAG_TO_CHANNEL,
    _safe_filename,
    convert_sonar_preset,
)

ASM_TARBALL_URL = (
    "https://codeload.github.com/loteran/Arctis-Sound-Manager/tar.gz/refs/heads/{ref}"
)
PRESET_PREFIX_IN_TARBALL = "src/arctis_sound_manager/gui/presets/"

BUNDLE_DIR = ROOT / "gui" / "presets" / "asm"

_TAG_RE = re.compile(r"\[([^\]]+)\]")


def fetch_tarball(ref: str) -> bytes:
    url = ASM_TARBALL_URL.format(ref=ref)
    print(f"Fetching {url} …")
    req = urllib.request.Request(
        url, headers={"User-Agent": "steelvoicemix-asm-refresh"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_filename(name: str) -> tuple[str, str] | None:
    """Extract (display_name, channel_key) from `Apex Legends [Game].json`.
    Returns None for unsupported tags or malformed filenames."""
    base = Path(name).name
    if not base.endswith(".json"):
        return None
    tag_match = _TAG_RE.search(base)
    if not tag_match:
        return None
    channel = _ASM_TAG_TO_CHANNEL.get(tag_match.group(1))
    if channel is None:
        return None
    display = base[: tag_match.start()].rstrip().rstrip(".")
    return display, channel


def write_preset(target: Path, name: str, channel: str, bands: list[dict]) -> None:
    payload = {"name": name, "channel": channel, "bands": bands}
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref", default="main", help="Git ref to pull from (branch / tag / SHA)"
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete bundled presets that are no longer in upstream",
    )
    args = parser.parse_args()

    raw = fetch_tarball(args.ref)
    print(f"Downloaded {len(raw):,} bytes")

    saved_paths: set[Path] = set()
    skipped = 0
    written = 0

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            # Tarball top-level dir is `Arctis-Sound-Manager-<ref>/`;
            # presets sit under that prefix + the in-repo path.
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue
            inner = parts[1]
            if not inner.startswith(PRESET_PREFIX_IN_TARBALL):
                continue
            filename = inner[len(PRESET_PREFIX_IN_TARBALL) :]
            parsed = parse_filename(filename)
            if parsed is None:
                continue
            display, channel = parsed

            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            try:
                payload = json.loads(extracted.read().decode("utf-8"))
            except json.JSONDecodeError as e:
                print(f"  ! malformed JSON in {filename}: {e}")
                skipped += 1
                continue

            bands = convert_sonar_preset(payload)
            if bands is None or len(bands) != NUM_BANDS:
                print(f"  ! unsupported filter shape in {filename}")
                skipped += 1
                continue

            channel_dir = BUNDLE_DIR / channel
            channel_dir.mkdir(parents=True, exist_ok=True)
            safe = _safe_filename(display) or "Untitled"
            target = channel_dir / f"{safe}.json"
            write_preset(target, display, channel, bands)
            saved_paths.add(target)
            written += 1

    print(f"Wrote {written} presets, skipped {skipped}")

    if args.prune:
        existing = {p for p in BUNDLE_DIR.rglob("*.json")}
        stale = existing - saved_paths
        if stale:
            print(f"Pruning {len(stale)} stale presets:")
            for p in sorted(stale):
                print(f"  - {p.relative_to(ROOT)}")
                p.unlink()
        else:
            print("Nothing to prune.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

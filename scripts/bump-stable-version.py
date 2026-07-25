#!/usr/bin/env python3
"""Bump the stable-channel version.

The dev channel has scripts/bump-dev-version.sh; this is its stable
counterpart. It updates the three places a stable release version
appears:

  1. steelvoicemix.spec  — the `Version:` field, plus a new %changelog
     entry (RPM requires one per release, and stable-release.yml uses
     the top entry as the GitHub Release notes).
  2. dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml — a <release>
     entry, which is what GNOME Software / KDE Discover show.

Cargo.toml and the GUI's version fallback deliberately track the *dev*
version (see bump-dev-version.sh) — the crate version isn't
user-facing and the GUI derives its real version from `rpm -q` at
runtime, so this script leaves them alone.

Pushing a stable version bump to main is what triggers
.github/workflows/stable-release.yml, so treat running this as
"cutting a release".

Usage:
  scripts/bump-stable-version.py 0.4.4
  scripts/bump-stable-version.py 0.4.4 --notes-file notes.md
  scripts/bump-stable-version.py --check      # print current, no change

Notes default to the commit subjects since the previous stable tag,
which is a starting point, not a substitute for editing the entry by
hand before release.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "steelvoicemix.spec"
METAINFO = ROOT / "dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MAINTAINER = "Ibrahim Aldhaheri <ibrahim@abokhalil.dev>"


def current_version() -> str:
    for line in SPEC.read_text().splitlines():
        if line.startswith("Version:"):
            return line.split()[1]
    raise SystemExit(f"Could not read Version: from {SPEC}")


def generate_notes(prev_version: str) -> str:
    """Commit subjects since the previous stable tag, as changelog
    bullets. Dependency and chore commits are dropped — they're noise
    in user-facing release notes."""
    tag = f"v{prev_version}"
    try:
        out = subprocess.check_output(
            ["git", "log", "--pretty=format:%s", f"{tag}..HEAD"],
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return "- See the commit history for changes in this release.\n"
    bullets: list[str] = []
    for subject in out.splitlines():
        if re.match(r"^(deps|chore|ci)(\(|:)", subject):
            continue
        subject = subject.strip()
        if subject:
            bullets.append(f"- {subject}\n")
    if not bullets:
        return "- Maintenance release.\n"
    return "".join(bullets)


def bump_spec(new: str, notes: str) -> None:
    s = SPEC.read_text()
    s = re.sub(r"^Version:\s+\S+", f"Version:        {new}", s, count=1,
               flags=re.MULTILINE)
    # RPM changelog dates are always C-locale English day/month names.
    today = datetime.date.today().strftime("%a %b %d %Y")
    entry = f"%changelog\n* {today} {MAINTAINER} - {new}-1\n{notes}"
    if "%changelog\n" not in s:
        raise SystemExit("No %changelog section found in the spec")
    s = s.replace("%changelog\n", entry, 1)
    SPEC.write_text(s)


def bump_metainfo(new: str, notes: str) -> None:
    s = METAINFO.read_text()
    if f'<release version="{new}"' in s:
        print(f"  metainfo already has a {new} entry — leaving it alone")
        return
    today = datetime.date.today().isoformat()
    # Keep the description short; the spec changelog is the long form.
    summary = " ".join(
        line.lstrip("- ").strip()
        for line in notes.splitlines()[:3]
        if line.strip()
    )
    summary = summary or f"Release {new}."
    # Escape for XML text content.
    for bad, good in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        summary = summary.replace(bad, good)
    entry = (
        "  <releases>\n"
        f'    <release version="{new}" date="{today}" type="stable">\n'
        "      <description>\n"
        f"        <p>\n          {summary}\n        </p>\n"
        "      </description>\n"
        "    </release>\n"
    )
    if "  <releases>\n" not in s:
        raise SystemExit("No <releases> block found in the metainfo file")
    s = s.replace("  <releases>\n", entry, 1)
    METAINFO.write_text(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", nargs="?", help="New stable version, e.g. 0.4.4")
    ap.add_argument("--check", action="store_true",
                    help="Print the current version and exit")
    ap.add_argument("--notes-file", type=Path,
                    help="File containing the changelog body (one '- ' bullet "
                         "per line). Defaults to commit subjects since the "
                         "previous stable tag.")
    args = ap.parse_args()

    cur = current_version()
    if args.check:
        print(cur)
        return 0
    if not args.version:
        ap.error("a version is required (stable versions are never guessed)")
    new = args.version
    if not VERSION_RE.match(new):
        raise SystemExit(
            f"Refusing unrecognised stable version {new!r}; "
            f"expected MAJOR.MINOR.PATCH (no ~betaN on the stable channel)"
        )
    if new == cur:
        print(f"Already at {cur} — nothing to do.")
        return 0

    if args.notes_file:
        notes = args.notes_file.read_text()
    else:
        notes = generate_notes(cur)
    if not notes.endswith("\n"):
        notes += "\n"

    bump_spec(new, notes)
    bump_metainfo(new, notes)

    print(f"Bumped stable: {cur} → {new}")
    print(f"  {SPEC.name}")
    print(f"  {METAINFO.name}")
    print()
    print("Review the generated changelog before releasing — pushing this "
          "to main triggers the stable release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
